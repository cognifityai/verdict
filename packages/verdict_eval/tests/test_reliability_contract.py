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


def _literal_template_reply(name: str, response: str) -> str:
    """Fill the audit prompt's own example with values, changing no structure."""
    from verdict_eval.historical_audit import _PROMPT

    example = _PROMPT[_PROMPT.index('{"turns"'):_PROMPT.index("\nA recovered finding")]
    for placeholder, value in (
        ("PASS|FAIL|UNCLEAR", "FAIL"), ("unresolved|unclear", "unresolved"),
        ("<dimension>", name), ("<only dimensions declaring holistic>", name),
        ("<exact text from R1>", response),
    ):
        example = example.replace(placeholder, value)
    for placeholder in ("<short reason>", "<specific violation>", "<this finding's recovery only>",
                        "<whole-selection judgment>"):
        example = example.replace(placeholder, "Synthetic reason.")
    return example


def _single_turn_evidence(response: str) -> dict:
    return {
        "turnPairs": [{"reference": "T1", "requestReference": "Q1", "responseReference": "R1",
                       "originalEvidenceReferences": ["Q1", "R1"], "laterEvidenceReferences": []}],
        "documents": {"Q1": {"text": "How many sides does a hexagon have?"}, "R1": {"text": response}},
        "chronologyCertain": True, "coverage": "complete",
    }


@pytest.mark.parametrize("fence", [False, True])
def test_prompt_example_is_itself_a_valid_audit_reply(fence):
    from verdict_eval.historical_audit import parse

    response = "A hexagon has five sides."
    policy = {"geometry": {"aggregation": "holistic", "evidenceAuthority": "public_knowledge",
                           "requiredEvidence": ["request", "response"]}}
    reply = _literal_template_reply("geometry", response)
    if fence:
        reply = "Here is the audit.\n```json\n" + reply + "\n```"
    result = parse(reply, _single_turn_evidence(response), policy)
    assert result["dimensions"][0]["verdict"] == "fail"
    assert [finding["id"] for finding in result["historicalFindings"]] == ["F1"]


def test_draft_and_corrected_audit_objects_are_two_answers():
    from verdict_eval.historical_audit import parse

    response = "A hexagon has five sides."
    policy = {"geometry": {"aggregation": "holistic", "evidenceAuthority": "public_knowledge",
                           "requiredEvidence": ["request", "response"]}}
    reply = _literal_template_reply("geometry", response)
    with pytest.raises(JudgeOutputError, match="multiple audit answers"):
        parse(reply + "\nCorrection:\n" + reply, _single_turn_evidence(response), policy)


def test_malformed_audit_object_is_invalid_json_not_its_inner_objects():
    from verdict_eval.judge_output import decode_judge_object

    response = "A hexagon has five sides."
    reply = _literal_template_reply("geometry", response)
    # One closing brace short: only nested assessments remain decodable.
    malformed = reply.replace("}}}],\n", "}}],\n", 1)
    assert malformed != reply
    with pytest.raises(JudgeOutputError, match="invalid audit JSON"):
        decode_judge_object(malformed)
    # A broken draft followed by a valid answer is not one clean answer.
    with pytest.raises(JudgeOutputError, match="invalid audit JSON"):
        decode_judge_object(malformed + "\n" + reply)
    assert decode_judge_object(reply)["turns"]
