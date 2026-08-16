"""Tests for context-dependent dimension skipping in the Judge.

An internal validation run surfaced that groundedness ("supported by retrieved context")
was being scored as FAIL on a chat transcript that has no retrieved context —
the judge returns FAIL for unverifiable claims rather than UNCLEAR, so the
UNCLEAR-exclusion didn't help. Fix: a context-aware caller skips groundedness
entirely when no context is supplied. Probe suites (which use groundedness as
a world-knowledge correctness check) leave the flag off and are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

from verdict_eval.judge import DEFAULT_RUBRIC, Judge


@dataclass
class _StubResponse:
    text: str


class _CapturingProvider:
    """Records the prompt it was asked to complete, returns a fixed JSON verdict
    for whatever dimensions appear in the rubric (PASS for all)."""

    name = "stub"

    def __init__(self) -> None:
        self.last_prompt = ""

    def complete(self, req):
        # The user prompt is the second message
        self.last_prompt = req.messages[-1]["content"]
        # Return PASS for every dimension named in DEFAULT_RUBRIC so parsing
        # succeeds regardless of which subset was requested.
        import json
        verdicts = {d.name: {"reasoning": "ok", "verdict": "PASS"}
                    for d in DEFAULT_RUBRIC.dimensions}
        return _StubResponse(text=json.dumps(verdicts))


def test_groundedness_skipped_without_context_when_flag_set() -> None:
    prov = _CapturingProvider()
    judge = Judge(provider=prov, model="m",
                  skip_context_dependent_when_missing=True)
    judgment = judge.judge(query="What's the plan?", response="Here is the plan.")
    names = {d.name for d in judgment.dimensions}
    assert "groundedness" not in names, "groundedness must be dropped with no context"
    # The other four dimensions remain
    assert "relevance" in names and "safety" in names
    assert len(judgment.dimensions) == len(DEFAULT_RUBRIC.dimensions) - 1
    # And the prompt sent to the judge must not mention groundedness
    assert "groundedness" not in prov.last_prompt


def test_groundedness_kept_with_context() -> None:
    prov = _CapturingProvider()
    judge = Judge(provider=prov, model="m",
                  skip_context_dependent_when_missing=True)
    judgment = judge.judge(query="Q", response="R", context="Some retrieved context.")
    names = {d.name for d in judgment.dimensions}
    assert "groundedness" in names, "with context, groundedness is evaluated"
    assert len(judgment.dimensions) == len(DEFAULT_RUBRIC.dimensions)


def test_groundedness_kept_when_flag_off_even_without_context() -> None:
    """Default behavior (flag off) preserves groundedness — this is what probe
    suites rely on (groundedness = world-knowledge correctness)."""
    prov = _CapturingProvider()
    judge = Judge(provider=prov, model="m")  # flag defaults to False
    judgment = judge.judge(query="Q", response="R")
    names = {d.name for d in judgment.dimensions}
    assert "groundedness" in names
    assert len(judgment.dimensions) == len(DEFAULT_RUBRIC.dimensions)
