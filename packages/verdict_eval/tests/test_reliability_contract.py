"""Synthetic contract checks; only the provider is substituted."""

import json
import random

import pytest
from verdict.schema import Verdict
from verdict_eval.judge import Judge, Rubric, RubricDimension
from verdict_eval.judge_output import JudgeOutputError
from verdict_eval.providers import FakeProvider

RUBRIC = Rubric("synthetic", "1", (RubricDimension("delivery", "Include two examples."),))


@pytest.mark.parametrize("payload", [
    "not json", "{}", '{"delivery":{"verdict":"MAYBE"}}',
    '{"delivery":{"verdict":"PASS"},"extra":{"verdict":"FAIL"}}',
    '{"delivery":{"verdict":"PASS","reason":"yes","reasoning":"no"}}',
    '{"delivery":{"verdict":"PASS"}} {"delivery":{"verdict":"FAIL"}}',
])
def test_technical_reply_failure_is_error_not_semantic_uncertainty(payload):
    with pytest.raises(JudgeOutputError):
        Judge(FakeProvider(payload), "synthetic-judge", RUBRIC).score(
            query="Give two examples", response="One example.",
        )


@pytest.mark.parametrize("label,expected", [("UNCLEAR", Verdict.UNCLEAR), ("FAIL", Verdict.FAIL)])
def test_valid_uncertainty_and_demonstrated_omission_remain_distinct(label, expected):
    scores = Judge(FakeProvider(json.dumps({"delivery": {
        "verdict": label, "reasoning": "Synthetic assessment",
    }})), "synthetic-judge", RUBRIC).score(query="Give two examples", response="One example.")
    assert scores[0].verdict is expected


def test_randomized_invalid_verdict_values_never_become_completed_unclear():
    rng = random.Random(431)
    invalid = [None, True, False, 0, 1, [], {}, "MAYBE", "", "error"]
    for _ in range(100):
        value = rng.choice(invalid)
        payload = json.dumps({"delivery": {"verdict": value, "reason": "Synthetic"}})
        with pytest.raises(JudgeOutputError):
            Judge(FakeProvider(payload), "synthetic-judge", RUBRIC).score(query="Question", response="Answer")
