"""ProbeRunner — execute a probe suite against a target model + judge.

The runner is intentionally minimal:

  1. For each probe, call the target model with the probe's prompt.
  2. If the probe has a `follow_up`, call the target model again with the
     full conversation history including the follow_up.
  3. For each `ProbeExpectation`, ask the judge to score the response on
     that dimension. The expectation's `judge_notes` are passed to the
     judge so it knows what to look for.
  4. A probe `overall_passed` iff every expectation passed.

Storage / persistence is left to the caller — see `scripts/run_probes.py`
for an example that writes ProbeRun results to JSON for diffing across
scheduled runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from verdict_eval.judge import DEFAULT_RUBRIC, Judge, Rubric
from verdict_eval.probes.schema import (
    Probe,
    ProbeExpectation,
    ProbeResult,
    ProbeRun,
    ProbeSuite,
)
from verdict_eval.providers import CompletionRequest, LLMProvider


def _norm_verdict(v: object) -> str:
    """Normalize a Verdict enum or raw string to an uppercase string.

    The Verdict enum stores lowercase values (Verdict.PASS == "pass"), so
    comparing it directly to "PASS" always returns False. Normalizing both
    sides through this helper fixes that.
    """
    return str(getattr(v, "value", v)).upper()


@dataclass
class ProbeRunner:
    """Run probes against a target model, scored by a judge.

    Parameters
    ----------
    target_provider, target_model:
        The model being probed (the one whose drift we care about).
    judge_provider, judge_model:
        The model doing the scoring. Use a different family from the
        target to mitigate same-family judge bias.
    rubric:
        Used to ground the dimension names. Defaults to DEFAULT_RUBRIC.
    sleep_between_calls:
        Pacing between target API calls; gentle on rate limits.
    """
    target_provider: LLMProvider
    target_model: str
    judge_provider: LLMProvider
    judge_model: str
    rubric: Rubric = DEFAULT_RUBRIC
    sleep_between_calls: float = 0.2

    def __post_init__(self) -> None:
        self._judge = Judge(
            provider=self.judge_provider,
            model=self.judge_model,
            rubric=self.rubric,
            temperature=0.0,
            max_tokens=1024,
        )

    # ------------------------------------------------------------------ #
    # Probe execution
    # ------------------------------------------------------------------ #

    def _call_target(self, messages: list[dict]) -> tuple[str, float]:
        t0 = time.time()
        resp = self.target_provider.complete(CompletionRequest(
            model=self.target_model,
            messages=messages,
            temperature=0.0,
            max_tokens=1024,
        ))
        return (resp.text or "", (time.time() - t0) * 1000.0)

    def _evaluate_expectation(
        self, exp: ProbeExpectation, *, prompt: str, response: str,
    ) -> dict:
        """Ask the judge whether `response` meets `exp` on `exp.dimension`.

        We narrow the judge's rubric to just the one dimension being probed,
        and inject `exp.judge_notes` into the query so the judge knows
        precisely what counts as PASS.
        """
        narrowed_query = (
            f"PROBE EXPECTATION on dimension '{exp.dimension}': "
            f"{exp.judge_notes}\n\nORIGINAL PROMPT:\n{prompt}"
        )
        # We run the full judge and pick out only the dimension we care about
        try:
            judgment = self._judge.judge(
                query=narrowed_query,
                response=response,
            )
        except Exception as e:
            return {
                "name": exp.dimension,
                "expected": exp.verdict,
                "observed": "ERROR",
                "passed": False,
                "judge_reasoning": f"judge call failed: {e}",
            }
        observed: str | None = None
        reasoning: str = ""
        for d in judgment.dimensions:
            if d.name == exp.dimension:
                # Normalize to an uppercase string — the Verdict enum stores
                # lowercase values, so comparing the raw enum to "PASS" always
                # failed (the same bug that zeroed out the internal validation judge run).
                observed = _norm_verdict(d.verdict)
                reasoning = d.reasoning
                break
        if observed is None:
            # Judge rubric didn't include the expected dimension
            return {
                "name": exp.dimension,
                "expected": _norm_verdict(exp.verdict),
                "observed": "MISSING",
                "passed": False,
                "judge_reasoning": (
                    f"Judge rubric did not contain dimension '{exp.dimension}'."
                ),
            }
        return {
            "name": exp.dimension,
            "expected": _norm_verdict(exp.verdict),
            "observed": observed,
            "passed": observed == _norm_verdict(exp.verdict),
            "judge_reasoning": reasoning,
        }

    def run_one(self, probe: Probe) -> ProbeResult:
        """Run a single probe and return a ProbeResult."""
        messages = [{"role": "user", "content": probe.prompt}]
        try:
            response_text, latency_ms = self._call_target(messages)
        except Exception as e:
            return ProbeResult(
                probe_id=probe.id,
                category=probe.category,
                response_text="",
                judge_model=self.judge_model,
                target_model=self.target_model,
                latency_ms=0.0,
                error=f"target call failed: {e}",
            )

        follow_up_text: str | None = None
        if probe.follow_up:
            time.sleep(self.sleep_between_calls)
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": probe.follow_up})
            try:
                follow_up_text, _ = self._call_target(messages)
            except Exception as e:
                follow_up_text = f"(follow-up call failed: {e})"

        # Use the follow-up response for evaluation if present; that's the
        # final stance the model takes, which is what we want to score on
        # sycophancy probes specifically.
        eval_response = follow_up_text if follow_up_text else response_text

        dimensions: list[dict] = []
        for exp in probe.expectations:
            time.sleep(self.sleep_between_calls)
            dimensions.append(self._evaluate_expectation(
                exp, prompt=probe.prompt, response=eval_response,
            ))

        all_passed = bool(dimensions) and all(d["passed"] for d in dimensions)

        return ProbeResult(
            probe_id=probe.id,
            category=probe.category,
            response_text=response_text,
            follow_up_response_text=follow_up_text,
            dimensions=dimensions,
            overall_passed=all_passed,
            judge_model=self.judge_model,
            target_model=self.target_model,
            latency_ms=latency_ms,
        )

    def run_suite(self, suite: ProbeSuite) -> ProbeRun:
        """Run every probe in `suite`, return the aggregated ProbeRun."""
        run = ProbeRun.new(
            suite=suite,
            target_model=self.target_model,
            judge_model=self.judge_model,
        )
        for probe in suite.probes:
            result = self.run_one(probe)
            run.results.append(result)
        run.finished_at = datetime.now(timezone.utc)
        return run
