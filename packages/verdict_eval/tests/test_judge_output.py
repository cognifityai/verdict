"""The shared judge-reply decoder: every wrapper a model adds, every way a reply can lie."""

from __future__ import annotations

import json
import random

import pytest
from verdict_eval.judge_output import (
    MAX_OUTPUT_BYTES,
    JudgeOutputError,
    JudgeOutputUnusable,
    decode_judge_dimensions,
    dimension_fields,
    is_truncated,
)

# Reply shapes judges produce for a one-dimension rubric (``task_resolved``)
# over a synthetic support chat about a library card that stopped working.
# Each fixture is wholly synthetic; it exercises one decoder contract.

# A fenced object whose ``dimensions`` is a list of named results that echo
# the rubric ``type`` and add finding fields the prompt never named
# (``exact_quote``, a prose ``message_position``).
HAIKU_FENCED_LIST = """```json
{
  "dimensions": [
    {
      "name": "task_resolved",
      "type": "binary",
      "verdict": "PASS",
      "reason": "The agent found that the library card had expired, took the renewal fee, reactivated the card, and the member confirmed that borrowing works again.",
      "findings": [
        {
          "issue": "Correct resolution path",
          "message_position": "Final messages",
          "exact_quote": "Your card is active again. Please sign out of the catalog and sign back in.",
          "reason": "The member followed the instruction and confirmed the result: 'Signed back in and I can borrow again.'"
        },
        {
          "issue": "Tool sequence correctness",
          "message_position": "Tool calls throughout",
          "exact_quote": "[tool call] lookup_member, get_card_status, get_fees, collect_fee, renew_card",
          "reason": "Member lookup, card status, fee lookup, payment, and renewal happened in a sensible order."
        }
      ]
    }
  ]
}
```"""

# A fenced object in the shape and field names the prompt shows.
HAIKU_FENCED_OBJECT = """```json
{
  "dimensions": {
    "task_resolved": {
      "verdict": "PASS",
      "reason": "The agent identified the expired library card, collected the renewal fee, reactivated the card, and had the member sign back in to confirm borrowing works.",
      "findings": []
    }
  }
}
```"""

# A bare object with no fence or prose.
GPT_BARE_OBJECT = (
    '{"dimensions": {"task_resolved": {"verdict": "PASS", "reason": "The agent found the '
    'expired card, collected the fee, renewed the card, and confirmed the fix with the '
    'member.", "findings": []}}}'
)

# The object in a fence with prose around it.
GPT_FENCED_OBJECT = """Here is the evaluation:

```json
{"dimensions": {"task_resolved": {"verdict": "PASS", "reason": "The agent completed the renewal.", "findings": []}}}
```"""


def test_fenced_named_list_reply_decodes_with_its_verdict_and_raw_findings() -> None:
    decoded = decode_judge_dimensions(HAIKU_FENCED_LIST, ["task_resolved"])

    assert list(decoded) == ["task_resolved"]
    fields = dimension_fields(decoded["task_resolved"])
    assert fields.verdict == "PASS"
    assert fields.reason.startswith("The agent found that the library card")
    assert [finding["issue"] for finding in fields.findings] == [
        "Correct resolution path", "Tool sequence correctness",
    ]
    assert "name" not in decoded["task_resolved"]


@pytest.mark.parametrize("reply", [HAIKU_FENCED_OBJECT, GPT_BARE_OBJECT])
def test_fenced_and_bare_object_replies_decode(reply) -> None:
    decoded = decode_judge_dimensions(reply, ["task_resolved"])

    assert dimension_fields(decoded["task_resolved"]).verdict == "PASS"
    assert dimension_fields(decoded["task_resolved"]).findings == []


def test_fenced_object_with_prose_decodes() -> None:
    decoded = decode_judge_dimensions(GPT_FENCED_OBJECT, ["task_resolved"])

    assert dimension_fields(decoded["task_resolved"]).verdict == "PASS"


def _render(results: dict[str, dict], shape: str, wrapper: str) -> str:
    if shape == "flat":
        payload = results
    elif shape == "wrapped_object":
        payload = {"dimensions": results}
    elif shape == "wrapped_list":
        payload = {"dimensions": [{"name": name, **item} for name, item in results.items()]}
    elif shape == "bare_list":
        payload = [{"name": name, **item} for name, item in results.items()]
    else:  # pragma: no cover - guarded by parametrize
        raise AssertionError(shape)
    text = json.dumps(payload, ensure_ascii=False)
    if wrapper == "fence":
        return f"```json\n{text}\n```"
    if wrapper == "bare_fence":
        return f"```\n{text}\n```"
    if wrapper == "prose":
        return f"Sure, here is my evaluation:\n{text}\nI hope this helps."
    if wrapper == "preface_json":
        return f'metadata: {{"attempt": 1}} and [1, 2]\n{text}'
    return text


@pytest.mark.parametrize("shape", ["flat", "wrapped_object", "wrapped_list", "bare_list"])
@pytest.mark.parametrize("wrapper", ["none", "fence", "bare_fence", "prose", "preface_json"])
def test_every_shape_and_wrapper_decodes_to_the_same_fields(shape, wrapper) -> None:
    results = {
        "relevance": {"verdict": "PASS", "reasoning": "on topic", "findings": []},
        "safety": {"verdict": "FAIL", "reason": "leaks {braces} and ```fences```", "type": "binary"},
    }

    decoded = decode_judge_dimensions(_render(results, shape, wrapper), ["relevance", "safety"])

    assert set(decoded) == {"relevance", "safety"}
    assert dimension_fields(decoded["relevance"]).verdict == "PASS"
    assert dimension_fields(decoded["relevance"]).reason == "on topic"
    assert dimension_fields(decoded["safety"]).verdict == "FAIL"
    assert dimension_fields(decoded["safety"]).reason == "leaks {braces} and ```fences```"


def test_random_wrappers_never_change_the_decoded_fields() -> None:
    rng = random.Random(20260929)
    atoms = ['"', "{", "}", "[", "]", "```", "\\", "é", "\n", "json", ":", ","]
    for _ in range(300):
        names = [f"dim_{index}" for index in range(rng.randint(1, 4))]
        results = {}
        for name in names:
            reason = "".join(rng.choice([*atoms, "word "]) for _ in range(rng.randint(0, 12)))
            results[name] = {
                "verdict": rng.choice(["PASS", "FAIL", "UNCLEAR"]),
                rng.choice(["reason", "reasoning"]): reason,
                "score": rng.choice([None, 0, 2.5, -1]),
            }
        shape = rng.choice(["flat", "wrapped_object", "wrapped_list", "bare_list"])
        wrapper = rng.choice(["none", "fence", "bare_fence", "prose", "preface_json"])

        decoded = decode_judge_dimensions(_render(results, shape, wrapper), names)

        assert set(decoded) == set(names), (shape, wrapper)
        for name in names:
            fields = dimension_fields(decoded[name])
            expected = results[name]
            assert fields.verdict == expected["verdict"]
            assert fields.reason == expected.get("reason", expected.get("reasoning", "")).strip()
            assert fields.score == expected["score"]


def test_wrapper_with_dimensions_key_is_the_answer_even_when_names_differ() -> None:
    decoded = decode_judge_dimensions('{"dimensions": {"other": {"verdict": "PASS"}}}', ["helpful"])

    assert decoded == {"other": {"verdict": "PASS"}}


def test_flat_object_without_an_expected_name_is_skipped_until_one_matches() -> None:
    text = '{"note": "first"} {"helpful": {"verdict": "FAIL", "reason": "r"}}'

    decoded = decode_judge_dimensions(text, ["helpful"])

    assert dimension_fields(decoded["helpful"]).verdict == "FAIL"


@pytest.mark.parametrize("text", [
    '{"helpful": {"verdict": "PASS"}, "helpful": {"verdict": "FAIL"}}',
    '{"helpful": {"verdict": "PASS", "verdict": "FAIL", "reason": "r"}}',
    '{"dimensions": {"helpful": {"verdict": "PASS"}}, "dimensions": {"helpful": {"verdict": "FAIL"}}}',
    'preface {"a": 1, "a": 2} then {"helpful": {"verdict": "PASS"}}',
])
def test_duplicate_keys_reject_the_whole_reply(text) -> None:
    with pytest.raises(JudgeOutputError, match="duplicate"):
        decode_judge_dimensions(text, ["helpful"])


@pytest.mark.parametrize("text", [
    '{"dimensions": [{"name": "helpful", "verdict": "PASS"}, {"name": "helpful", "verdict": "FAIL"}]}',
    '[{"name": "helpful", "verdict": "PASS"}, {"name": "helpful", "verdict": "FAIL"}]',
])
def test_duplicate_dimension_names_in_a_list_reject_the_reply(text) -> None:
    with pytest.raises(JudgeOutputError, match="duplicate"):
        decode_judge_dimensions(text, ["helpful"])


@pytest.mark.parametrize("text", [
    '{"dimensions": [{"verdict": "PASS"}]}',
    '{"dimensions": [{"name": 3, "verdict": "PASS"}]}',
    '{"dimensions": ["helpful"]}',
    '{"dimensions": "helpful"}',
    '{"dimensions": null}',
])
def test_malformed_dimension_lists_reject_the_reply(text) -> None:
    with pytest.raises(JudgeOutputError):
        decode_judge_dimensions(text, ["helpful"])


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_numbers_reject_the_reply(literal) -> None:
    with pytest.raises(JudgeOutputError, match="non-finite"):
        decode_judge_dimensions('{"helpful": {"score": ' + literal + '}}', ["helpful"])


@pytest.mark.parametrize("text", [
    "", "no json here", "{", '{"helpful": {"verdict": "PASS"',
    '```json\n{"helpful": {"verdict": "PASS"}\n```',
    '{"other": {"verdict": "PASS"}}', "[1, 2, 3]", '["helpful"]', "null",
])
def test_replies_without_a_usable_result_raise(text) -> None:
    with pytest.raises(JudgeOutputError):
        decode_judge_dimensions(text, ["helpful"])


def test_non_text_reply_raises() -> None:
    with pytest.raises(JudgeOutputError):
        decode_judge_dimensions(None, ["helpful"])


def test_oversize_reply_is_unusable_not_malformed() -> None:
    text = '{"helpful": {"verdict": "PASS", "reason": "' + "x" * MAX_OUTPUT_BYTES + '"}}'

    with pytest.raises(JudgeOutputUnusable):
        decode_judge_dimensions(text, ["helpful"])
    assert issubclass(JudgeOutputUnusable, JudgeOutputError)


def test_candidate_scan_is_bounded() -> None:
    text = "{ " * 65 + '{"helpful": {"verdict": "PASS"}}'

    with pytest.raises(JudgeOutputError, match="candidate"):
        decode_judge_dimensions(text, ["helpful"])


def test_dimension_fields_normalize_verdict_reason_and_findings() -> None:
    fields = dimension_fields({
        "verdict": " pass ", "reasoning": " because ", "type": "binary", "extra": 1,
    })

    assert fields.verdict == "PASS"
    assert fields.reason == "because"
    assert fields.findings == []
    assert fields.score is None


def test_dimension_fields_prefers_reason_over_reasoning_and_accepts_scalars() -> None:
    assert dimension_fields({"reason": "a", "reasoning": "b"}).reason == "a"
    assert dimension_fields({"verdict": True}).verdict == "TRUE"
    assert dimension_fields({"reason": 4}).reason == "4"
    assert dimension_fields({"findings": None}).findings == []
    assert dimension_fields({}).verdict is None


@pytest.mark.parametrize("item", [
    "PASS", None, ["PASS"], {"verdict": ["PASS"]}, {"verdict": {"x": 1}},
    {"reason": ["a"]}, {"reason": {"a": 1}}, {"findings": "none"}, {"findings": {}},
])
def test_dimension_fields_reject_structural_mistakes(item) -> None:
    with pytest.raises(JudgeOutputError):
        dimension_fields(item)


@pytest.mark.parametrize(("finish_reason", "expected"), [
    ("length", True), ("max_tokens", True), ("MAX_TOKENS", True),
    ("FinishReason.MAX_TOKENS", True), (" Length ", True),
    ("stop", False), ("end_turn", False), ("STOP", False), ("FinishReason.STOP", False),
    ("refusal", False), ("", False), (None, False), ("tool_use", False),
])
def test_is_truncated_recognizes_every_provider_spelling(finish_reason, expected) -> None:
    assert is_truncated(finish_reason) is expected
