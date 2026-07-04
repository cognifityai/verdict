"""Tests for the judge — using FakeProvider so no real API calls happen.

Validates JSON parsing tolerance and rubric handling.
"""

from __future__ import annotations

import json

from verdict.schema import Verdict
from verdict_eval.judge import DEFAULT_RUBRIC, Judge, JudgeEnsemble
from verdict_eval.providers import CompletionResponse, FakeProvider


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


def test_judge_tolerates_code_fence_wrapper():
    payload = "```json\n" + _fake_json_for({d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions}) + "\n```"
    judge = Judge(provider=FakeProvider(payload), model="fake-judge")
    j = judge.judge(query="hi", response="hello")
    assert j.pass_count == len(DEFAULT_RUBRIC.dimensions)


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
