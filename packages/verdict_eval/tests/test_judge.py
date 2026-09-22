"""Tests for the judge — using FakeProvider so no real API calls happen.

Validates JSON parsing tolerance and rubric handling.
"""

from __future__ import annotations

import json

import pytest
from verdict.schema import Verdict
from verdict_eval.judge import (
    DEFAULT_RUBRIC,
    Judge,
    JudgeEnsemble,
    Rubric,
    RubricDimension,
)
from verdict_eval.providers import FakeProvider


def test_judge_marks_provider_call_and_restores_existing_workload() -> None:
    from verdict.client import clear_context, get_context_workload, set_context

    payload = json.dumps({
        dimension.name: {"reasoning": "ok", "verdict": "PASS"}
        for dimension in DEFAULT_RUBRIC.dimensions
    })

    class WorkloadObservingProvider(FakeProvider):
        observed_workload: str | None = None

        def complete(self, request):
            self.observed_workload = get_context_workload()
            return super().complete(request)

    provider = WorkloadObservingProvider(payload)
    set_context(workload="agent")
    try:
        Judge(provider=provider, model="judge-a").judge(
            query="question", response="answer"
        )
        assert provider.observed_workload == "judge"
        assert get_context_workload() == "agent"
    finally:
        clear_context()


def test_trace_judgment_identity_is_captured_before_provider_mutates() -> None:
    class MutatingProvider(FakeProvider):
        name = "before-call"
        supports_temperature = False

        def complete(self, request):
            self.name = "after-call"
            self.supports_temperature = True
            return super().complete(request)

    provider = MutatingProvider("{}")
    judge = Judge(provider=provider, model="judge-a")
    expected = judge.evaluator_identity()
    result = judge.judge(query="question", response="answer")
    assert result.evaluator_provider == "before-call"
    assert result.evaluator_fingerprint == expected["evaluator_fingerprint"]
    assert judge.evaluator_identity()["evaluator_fingerprint"] != result.evaluator_fingerprint


@pytest.mark.parametrize(
    ("mode", "template"),
    [("unsupported", "calls: {calls}"), ("counts_v1", None)],
)
def test_direct_score_rejects_invalid_tool_mode_before_egress(mode, template):
    class CountingProvider(FakeProvider):
        calls = 0

        def complete(self, request):
            self.calls += 1
            return super().complete(request)

    provider = CountingProvider("{}")
    judge = Judge(provider=provider, model="judge-a",
                  tool_evidence_mode=mode, tool_evidence_template=template)
    with pytest.raises(ValueError):
        judge.score(query="question", response="answer", tool_evidence="counts")
    assert provider.calls == 0


def _fake_json_for(verdicts: dict[str, str]) -> str:
    return json.dumps({k: {"reasoning": "r", "verdict": v} for k, v in verdicts.items()})


def test_judge_parses_clean_json_and_assigns_per_dimension_verdicts():
    payload = _fake_json_for(
        {
            "groundedness": "PASS",
            "relevance": "PASS",
            "completeness": "FAIL",
            "safety": "PASS",
            "instruction_following": "PASS",
        }
    )
    judge = Judge(provider=FakeProvider(payload), model="fake-judge")
    j = judge.judge(query="hi", response="hello", trace_id="t1")
    assert j.trace_id == "t1"
    by_name = {d.name: d for d in j.dimensions}
    assert by_name["groundedness"].verdict == Verdict.PASS
    assert by_name["completeness"].verdict == Verdict.FAIL
    assert j.pass_count == 4
    assert j.fail_count == 1
    assert j.evaluator_identity_complete
    assert j.evaluator_provider == "fake"
    assert j.expected_dimensions == [d.name for d in DEFAULT_RUBRIC.dimensions]


def test_evaluator_fingerprint_changes_for_behavior_relevant_configuration():
    base = Judge(provider=FakeProvider("{}"), model="judge-a")
    changed_temperature = Judge(
        provider=FakeProvider("{}"), model="judge-a", temperature=0.1
    )
    changed_rubric_text = Judge(
        provider=FakeProvider("{}"),
        model="judge-a",
        rubric=Rubric(
            name=DEFAULT_RUBRIC.name,
            version=DEFAULT_RUBRIC.version,
            dimensions=(RubricDimension("relevance", "A changed definition."),),
        ),
    )

    fingerprints = {
        judge.evaluator_identity()["evaluator_fingerprint"]
        for judge in (base, changed_temperature, changed_rubric_text)
    }

    assert len(fingerprints) == 3


def test_judge_tolerates_code_fence_wrapper():
    payload = "```json\n" + _fake_json_for({d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions}) + "\n```"
    judge = Judge(provider=FakeProvider(payload), model="fake-judge")
    j = judge.judge(query="hi", response="hello")
    assert j.pass_count == len(DEFAULT_RUBRIC.dimensions)


def test_judge_preserves_verdict_when_fenced_reasoning_contains_markdown_fence():
    expected = {
        dimension.name: {
            "reasoning": "The response included ```python\nprint('ok')\n``` correctly.",
            "verdict": "FAIL" if dimension.name == "completeness" else "PASS",
        }
        for dimension in DEFAULT_RUBRIC.dimensions
    }
    payload = f"```json\n{json.dumps(expected)}\n```"

    judgment = Judge(provider=FakeProvider(payload), model="fake-judge").judge(
        query="hi", response="hello"
    )

    by_name = {dimension.name: dimension for dimension in judgment.dimensions}
    assert by_name["completeness"].verdict == Verdict.FAIL
    assert by_name["completeness"].reasoning == expected["completeness"]["reasoning"]
    assert judgment.pass_count == len(DEFAULT_RUBRIC.dimensions) - 1
    assert judgment.fail_count == 1


def test_judge_preserves_json_delimiters_and_escapes_inside_reasoning():
    reasoning = 'Compared {"nested": "value"}, a \\path, and a "quoted" phrase.'
    payload = json.dumps({
        "groundedness": {"reasoning": reasoning, "verdict": "FAIL"},
    })

    judgment = Judge(provider=FakeProvider(payload), model="fake-judge").judge(
        query="hi", response="hello"
    )

    groundedness = next(
        dimension
        for dimension in judgment.dimensions
        if dimension.name == "groundedness"
    )
    assert groundedness.verdict == Verdict.FAIL
    assert groundedness.reasoning == reasoning


def test_judge_ignores_unrelated_json_object_before_verdict_object():
    verdicts = _fake_json_for({d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions})
    payload = f'preface metadata: {{"attempt": 1}}\nverdict: {verdicts}'

    judgment = Judge(provider=FakeProvider(payload), model="fake-judge").judge(
        query="hi", response="hello"
    )

    assert judgment.pass_count == len(DEFAULT_RUBRIC.dimensions)


def test_judge_keeps_malformed_json_unclear():
    payload = '```json\n{"groundedness": {"reasoning": "broken", "verdict": "FAIL"}'

    judgment = Judge(provider=FakeProvider(payload), model="fake-judge").judge(
        query="hi", response="hello"
    )

    assert all(dimension.verdict == Verdict.UNCLEAR for dimension in judgment.dimensions)


def test_judge_tolerates_garbage_before_json():
    payload = "Sure, here's my evaluation:\n" + _fake_json_for(
        {d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions}
    )
    judge = Judge(provider=FakeProvider(payload), model="fake-judge")
    j = judge.judge(query="hi", response="hello")
    assert j.pass_count == len(DEFAULT_RUBRIC.dimensions)


def test_judge_records_unclear_when_dimension_missing():
    payload = json.dumps({"groundedness": {"reasoning": "r", "verdict": "PASS"}})
    judge = Judge(provider=FakeProvider(payload), model="fake-judge")
    j = judge.judge(query="hi", response="hello")
    by_name = {d.name: d.verdict for d in j.dimensions}
    assert by_name["groundedness"] == Verdict.PASS
    assert by_name["relevance"] == Verdict.UNCLEAR
    assert by_name["completeness"] == Verdict.UNCLEAR


def test_ensemble_majority_vote():
    pass_payload = _fake_json_for({d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions})
    fail_payload = _fake_json_for({d.name: "FAIL" for d in DEFAULT_RUBRIC.dimensions})
    judges = [
        Judge(provider=FakeProvider(pass_payload), model="judge-a"),
        Judge(provider=FakeProvider(pass_payload), model="judge-b"),
        Judge(provider=FakeProvider(fail_payload), model="judge-c"),
    ]
    j = JudgeEnsemble(judges).judge(query="q", response="r")
    # 2 PASS vs 1 FAIL → PASS wins
    assert all(d.verdict == Verdict.PASS for d in j.dimensions)
    # All judges credited
    assert set(j.judge_models) == {"judge-a", "judge-b", "judge-c"}


def test_ensemble_tie_becomes_unclear():
    pass_payload = _fake_json_for({d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions})
    fail_payload = _fake_json_for({d.name: "FAIL" for d in DEFAULT_RUBRIC.dimensions})
    judges = [
        Judge(provider=FakeProvider(pass_payload), model="judge-a"),
        Judge(provider=FakeProvider(fail_payload), model="judge-b"),
    ]
    j = JudgeEnsemble(judges).judge(query="q", response="r")
    assert all(d.verdict == Verdict.UNCLEAR for d in j.dimensions)


def test_ensemble_rejects_same_named_dimensions_with_different_context_contracts():
    first = Rubric(
        name="same",
        version="1",
        dimensions=(RubricDimension("quality", "definition", requires_context=False),),
    )
    second = Rubric(
        name="same",
        version="1",
        dimensions=(RubricDimension("quality", "definition", requires_context=True),),
    )

    with pytest.raises(ValueError, match="same rubric"):
        JudgeEnsemble([
            Judge(FakeProvider("{}"), "a", rubric=first),
            Judge(FakeProvider("{}"), "b", rubric=second),
        ])
