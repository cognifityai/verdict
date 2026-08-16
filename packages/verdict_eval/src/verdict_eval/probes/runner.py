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

from verdict.metrics import verdict_label
from verdict.redaction import redact

from verdict_eval.judge import DEFAULT_RUBRIC, Judge, JudgeEnsemble, Rubric
from verdict_eval.probes.schema import (
    PROBE_JUDGE_METHOD_VERSION,
    PROBE_METRIC_SCHEMA_VERSION,
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
    return verdict_label(v)


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
    judge: Judge | JudgeEnsemble | None = None

    def __post_init__(self) -> None:
        self._judge = self.judge or Judge(
            provider=self.judge_provider,
            model=self.judge_model,
            rubric=self.rubric,
            temperature=0.0,
            max_tokens=1024,
        )
        # A supplied judge is a complete evaluator configuration. Its rubric is
        # authoritative just like its provider/model/temperature; replacing it
        # with the runner default can silently skip custom dimensions.
        if isinstance(self.judge, Judge):
            self.rubric = self.judge.rubric
        elif isinstance(self.judge, JudgeEnsemble):
            self.rubric = self.judge.judges[0].rubric
        identity = self._judge.evaluator_identity()
        identity_models = identity.get("judge_models", [])
        self._artifact_judge_model = (
            "+".join(str(model) for model in identity_models)
            if identity_models
            else self.judge_model
        )
        self._dimension_judges: dict[str, object] = {}

    @staticmethod
    def _narrow_judge(active_judge: object, rubric: Rubric) -> object:
        if isinstance(active_judge, Judge):
            return Judge(
                provider=active_judge.provider,
                model=active_judge.model,
                rubric=rubric,
                temperature=active_judge.temperature,
                max_tokens=active_judge.max_tokens,
                skip_context_dependent_when_missing=(
                    active_judge.skip_context_dependent_when_missing
                ),
            )
        if isinstance(active_judge, JudgeEnsemble):
            return JudgeEnsemble([
                ProbeRunner._narrow_judge(judge, rubric)
                for judge in active_judge.judges
            ])
        return active_judge

    @staticmethod
    def _provenance(active_judge: object) -> dict[str, str | None]:
        identity = (
            active_judge.evaluator_identity()
            if hasattr(active_judge, "evaluator_identity")
            else {}
        )
        return {
            "judgeMethodVersion": PROBE_JUDGE_METHOD_VERSION,
            "evaluatorFingerprint": identity.get("evaluator_fingerprint"),
        }

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
        rubric_dimension = next(
            (
                dimension
                for dimension in self.rubric.dimensions
                if dimension.name == exp.dimension
            ),
            None,
        )
        if rubric_dimension is None:
            return {
                "name": exp.dimension,
                "expected": _norm_verdict(exp.verdict),
                "observed": "MISSING",
                "passed": False,
                "judge_reasoning": (
                    f"Judge rubric did not contain dimension '{exp.dimension}'."
                ),
                "judgeMethodVersion": PROBE_JUDGE_METHOD_VERSION,
                "evaluatorFingerprint": None,
            }

        # Production Judge instances receive a one-dimension rubric so the
        # prompt, parser, expected dimensions, and evaluator fingerprint all
        # describe exactly the expectation under test. Tests/custom callers may
        # replace _judge with a compatible stub, which is used as supplied.
        active_judge = self._judge
        if isinstance(active_judge, (Judge, JudgeEnsemble)):
            active_judge = self._dimension_judges.get(exp.dimension)
            if active_judge is None:
                active_judge = self._narrow_judge(
                    self._judge,
                    Rubric(
                        name=self.rubric.name,
                        version=self.rubric.version,
                        dimensions=(rubric_dimension,),
                    ),
                )
                self._dimension_judges[exp.dimension] = active_judge
        provenance = self._provenance(active_judge)
        try:
            judgment = active_judge.judge(
                query=narrowed_query,
                response=response,
            )
        except Exception as e:
            return {
                "name": exp.dimension,
                "expected": exp.verdict,
                "observed": "ERROR",
                "passed": False,
                "judge_reasoning": redact(f"judge call failed: {e}") or "judge call failed",
                **provenance,
            }
        observed: str | None = None
        reasoning: str = ""
        for d in judgment.dimensions:
            if d.name == exp.dimension:
                # Normalize to an uppercase string — the Verdict enum stores
                # lowercase values, so comparing the raw enum to "PASS" always
                # failed (the same bug that zeroed out the internal validation judge run).
                observed = _norm_verdict(d.verdict)
                reasoning = redact(d.reasoning) or ""
                break
        if observed is None:
            return {
                "name": exp.dimension,
                "expected": _norm_verdict(exp.verdict),
                "observed": "MISSING",
                "passed": False,
                "judge_reasoning": (
                    f"Judge rubric did not contain dimension '{exp.dimension}'."
                ),
                **provenance,
            }
        return {
            "name": exp.dimension,
            "expected": _norm_verdict(exp.verdict),
            "observed": observed,
            "passed": observed == _norm_verdict(exp.verdict),
            "judge_reasoning": reasoning,
            **provenance,
        }

    def run_one(self, probe: Probe) -> ProbeResult:
        """Run a single probe and return a ProbeResult."""
        messages = [{"role": "user", "content": probe.prompt}]
        try:
            response_text, latency_ms = self._call_target(messages)
        except Exception as e:
            safe_error = redact(f"target call failed: {e}") or "target call failed"
            return ProbeResult(
                probe_id=probe.id,
                category=probe.category,
                response_text="",
                judge_model=self._artifact_judge_model,
                target_model=self.target_model,
                latency_ms=0.0,
                dimensions=self._error_dimensions(probe, safe_error),
                error=safe_error,
                weight=probe.weight,
                metric_schema_version=PROBE_METRIC_SCHEMA_VERSION,
                judge_method_version=PROBE_JUDGE_METHOD_VERSION,
            )

        follow_up_text: str | None = None
        if probe.follow_up:
            time.sleep(self.sleep_between_calls)
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": probe.follow_up})
            try:
                follow_up_text, _ = self._call_target(messages)
            except Exception as e:
                safe_error = (
                    redact(f"follow-up call failed: {e}")
                    or "follow-up call failed"
                )
                return ProbeResult(
                    probe_id=probe.id,
                    category=probe.category,
                    response_text=redact(response_text) or "",
                    dimensions=self._error_dimensions(probe, safe_error),
                    overall_passed=False,
                    judge_model=self._artifact_judge_model,
                    target_model=self.target_model,
                    latency_ms=latency_ms,
                    error=safe_error,
                    weight=probe.weight,
                    metric_schema_version=PROBE_METRIC_SCHEMA_VERSION,
                    judge_method_version=PROBE_JUDGE_METHOD_VERSION,
                )

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
            response_text=redact(response_text) or "",
            follow_up_response_text=redact(follow_up_text),
            dimensions=dimensions,
            overall_passed=all_passed,
            judge_model=self._artifact_judge_model,
            target_model=self.target_model,
            latency_ms=latency_ms,
            weight=probe.weight,
            metric_schema_version=PROBE_METRIC_SCHEMA_VERSION,
            judge_method_version=PROBE_JUDGE_METHOD_VERSION,
        )

    @staticmethod
    def _error_dimensions(probe: Probe, message: str) -> list[dict]:
        """Represent infrastructure errors in every declared denominator."""
        return [
            {
                "name": expectation.dimension,
                "expected": _norm_verdict(expectation.verdict),
                "observed": "ERROR",
                "passed": False,
                "judge_reasoning": message,
                "judgeMethodVersion": PROBE_JUDGE_METHOD_VERSION,
                "evaluatorFingerprint": None,
            }
            for expectation in probe.expectations
        ]

    def run_suite(self, suite: ProbeSuite) -> ProbeRun:
        """Run every probe in `suite`, return the aggregated ProbeRun."""
        run = ProbeRun.new(
            suite=suite,
            target_model=self.target_model,
            judge_model=self._artifact_judge_model,
        )
        for probe in suite.probes:
            result = self.run_one(probe)
            run.results.append(result)
        run.finished_at = datetime.now(timezone.utc)
        return run
