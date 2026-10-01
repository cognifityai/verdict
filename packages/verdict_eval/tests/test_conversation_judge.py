"""Real storage → plan → provider port → durable conversation result."""

import json
import re

import pytest
from verdict.conversations import validate_conversation
from verdict.storage.memory import InMemoryStorage
from verdict_eval.conversation_judge import execute_evaluation, preview_evaluation
from verdict_eval.providers import FakeProvider


def _row(identifier="a" * 32, *, messages=None):
    return validate_conversation({
        "id": identifier, "tenant_id": "alpha", "source_scope": "b" * 16,
        "messages": messages or [
            {"role": "user", "content": "First synthetic question."},
            {"role": "assistant", "content": "First synthetic answer."},
            {"role": "user", "content": "Second synthetic question."},
            {"role": "assistant", "content": "Second synthetic answer."},
        ],
        "event_at": "2026-09-01T12:00:00Z", "end_status": "complete", "input_issues": [],
    })


def _config(target="conversation"):
    return {"provider": "openai", "model": "synthetic-model", "maxCalls": 3,
            "rubric": {"name": "quality", "version": "1", "target": target,
                       "dimensions": [{"name": "helpful", "description": "Addresses the request."}]}}


def test_preview_excludes_ungradable_row_and_judges_valid_one():
    store = InMemoryStorage()
    store.save_conversation(_row(messages=[{"role": "user", "content": "No reply."}]))
    store.save_conversation(_row("c" * 32))
    preview = preview_evaluation(store, tenant_id="alpha", config=_config())
    assert preview["eligibleTargets"] == 1
    assert preview["notEvaluableReasons"] == {"no_completed_reply": 1}
    assert preview["plannedCalls"] == 1
    assert preview["plannedTargets"][0]["conversationId"] == "c" * 32


def test_approved_reply_plan_sends_only_prefix_and_records_partial_coverage():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    calls = []
    def answer(request):
        calls.append(request)
        return json.dumps({"dimensions": {"helpful": {"verdict": "PASS", "reason": "Synthetic answer.", "findings": []}}})
    provider = FakeProvider(answer)
    provider.name = "openai"
    config = _config("response")
    config["maxCalls"] = 1
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert result["completed"] == 1
    assert len(calls) == 1
    assert "Second synthetic question." not in calls[0].messages[1]["content"]
    assert "First synthetic answer." in calls[0].messages[1]["content"]
    saved = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert [a["target_position"] for a in saved] == [1]


def test_stale_approval_has_zero_provider_egress():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    changed = _row(messages=[{"role": "user", "content": "Changed question."}, {"role": "assistant", "content": "Changed answer."}])
    store.save_conversation(changed)
    calls = []
    provider = FakeProvider(lambda req: calls.append(req) or "{}")
    provider.name = "openai"
    with pytest.raises(ValueError, match="plan"):
        execute_evaluation(store, tenant_id="alpha", config={
            **config, "plannedTargets": preview["plannedTargets"],
            "planFingerprint": preview["planFingerprint"],
        }, confirm_external_egress=True, provider=provider)
    assert calls == []


def test_duplicate_judge_keys_are_error_not_success():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    provider = FakeProvider('{"dimensions":{"helpful":{"verdict":"PASS","verdict":"FAIL","reason":"bad"}}}')
    provider.name = "openai"
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert result["errors"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["status"] == "error"
    assert saved["dimensions"] == {}


def test_judge_output_secret_is_redacted_before_storage():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    provider = FakeProvider(json.dumps({"dimensions": {"helpful": {
        "verdict": "PASS", "reason": "Email doctor@example.org to confirm.",
        "findings": [],
    }}}))
    provider.name = "openai"
    outcome = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert outcome["completed"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["status"] == "completed"
    assert "doctor@example.org" not in json.dumps(saved)


def test_invented_finding_quote_is_a_retryable_judge_error():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    provider = FakeProvider(json.dumps({"dimensions": {"helpful": {
        "verdict": "FAIL", "reason": "Missing detail.",
        "findings": [{"issue": "fabricated", "message_position": 1,
                      "quote": "These words were never said", "reason": "Unsupported."}],
    }}}))
    provider.name = "openai"
    outcome = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert outcome["errors"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["status"] == "error"
    assert saved["findings"] == []


@pytest.mark.parametrize("extra", [{"dimension": "safety"}, {"unexpected": "value"}])
def test_judge_cannot_override_or_extend_finding_attribution(extra):
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    config["rubric"]["dimensions"].append({
        "name": "safety", "description": "Avoids unsafe advice.",
    })
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    provider = FakeProvider(json.dumps({"dimensions": {
        "helpful": {"verdict": "FAIL", "reason": "Missing detail.", "findings": [
            {"issue": "omission", "reason": "Missing detail.", **extra},
        ]},
        "safety": {"verdict": "PASS", "reason": "Safe.", "findings": []},
    }}))
    provider.name = "openai"
    outcome = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert outcome["errors"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["status"] == "error"
    assert saved["findings"] == []


# ---------------------------------------------------------------------------
# Reply shapes the shared decoder accepts (synthetic fixtures)
# ---------------------------------------------------------------------------

from test_judge_output import GPT_BARE_OBJECT, HAIKU_FENCED_LIST, HAIKU_FENCED_OBJECT  # noqa: E402


class _ResponseProvider:
    """Returns a full CompletionResponse so finish reasons reach the judge."""

    name = "openai"

    def __init__(self, text, finish_reason=None):
        self.text, self.finish_reason, self.requests = text, finish_reason, []

    def complete(self, request):
        from verdict_eval.providers import CompletionResponse

        self.requests.append(request)
        text = self.text(request) if callable(self.text) else self.text
        return CompletionResponse(text=text, finish_reason=self.finish_reason)


def _approved(store, config, provider):
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    return preview, result


HAIKU_STYLE_FENCED_LIST = """```json
{
  "dimensions": [
    {
      "name": "helpful",
      "type": "binary",
      "verdict": "FAIL",
      "reason": "The reply does not address the second question.",
      "findings": [
        {
          "issue": "unanswered",
          "message_position": 1,
          "quote": "First synthetic answer.",
          "reason": "Only the first question was answered."
        }
      ]
    }
  ]
}
```"""


def test_fenced_list_shaped_reply_is_graded_and_stored():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    provider = _ResponseProvider(HAIKU_STYLE_FENCED_LIST, "end_turn")

    preview, result = _approved(store, _config(), provider)

    assert result["completed"] == 1 and result["errors"] == 0
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["status"] == "completed"
    assert saved["dimensions"] == {"helpful": {
        "state": "fail", "score": None, "reason": "The reply does not address the second question.",
    }}
    assert saved["findings"][0]["quote"] == "First synthetic answer."
    assert saved["findings"][0]["dimension"] == "helpful"


@pytest.mark.parametrize("reply", [HAIKU_FENCED_OBJECT, GPT_BARE_OBJECT])
def test_fenced_and_bare_object_replies_are_graded(reply):
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    config["rubric"]["dimensions"] = [{"name": "task_resolved", "description": "Resolved."}]

    preview, result = _approved(store, config, _ResponseProvider(reply, "end_turn"))

    assert result["completed"] == 1 and result["errors"] == 0
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["dimensions"]["task_resolved"]["state"] == "pass"


def test_named_list_reply_decodes_but_its_unnamed_finding_fields_are_rejected():
    """The old prompt never named the finding fields, so Haiku invented them.

    The reply now decodes (a23 could not read it at all), but a finding with
    ``exact_quote`` and a prose position is not evidence Verdict can bind to a
    message, so the whole assessment stays a retryable error. The current
    prompt shows the exact finding shape; see the fixtures above.
    """
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    config["rubric"]["dimensions"] = [{"name": "task_resolved", "description": "Resolved."}]

    preview, result = _approved(store, config, _ResponseProvider(HAIKU_FENCED_LIST, "end_turn"))

    assert result["errors"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["error"] == "invalid_judge_output"


# A reply with the correct verdict, four exactly cited quotes, and prose
# ``issue`` labels that the stored identifier rule used to reject outright.
GPT5_MINI_FINDINGS_REPLY = (
    '{"dimensions": {"task_resolved": {"verdict": "PASS", "reason": "Agent identified the '
    'expired card, collected the renewal fee, reactivated the card, and the member confirmed '
    'borrowing works.", "findings": [{"issue": "Card expiry identified", "message_position": 4, '
    '"quote": "I can see that your library card expired last month. That is why checkout fails.", '
    '"reason": "Shows the agent diagnosed the cause."}, {"issue": "Fee collected", '
    '"message_position": 5, "quote": "[tool result] Renewal fee of 12.0 USD collected for card '
    'C0000.", "reason": "Confirms the fee was taken as part of the fix."}, {"issue": "Card '
    'renewed", "message_position": 6, "quote": "[tool result] {\\"message\\": \\"Card renewed\\", '
    '\\"card\\": {\\"card_id\\": \\"C0000\\", \\"status\\": \\"Active\\"}}", '
    '"reason": "Shows the system action that reactivated the card."}, {"issue": "Member '
    'confirmation of resolution", "message_position": 7, "quote": "Signed back in and I can '
    'borrow again.", "reason": "The member reports the problem is gone."}]}}}'
)


def _support_row():
    replies = json.loads(GPT5_MINI_FINDINGS_REPLY)["dimensions"]["task_resolved"]["findings"]
    quotes = {finding["message_position"]: finding["quote"] for finding in replies}
    messages = []
    for position in range(8):
        role = "user" if position % 2 == 0 else "assistant"
        if position == 7:
            role = "user"
        if position == 4:
            role = "assistant"
        content = quotes.get(position, f"Synthetic turn {position}.")
        messages.append({"role": role, "content": content})
    return _row("d" * 32, messages=messages)


def test_reply_with_prose_issue_labels_is_graded_with_its_findings():
    store = InMemoryStorage()
    row = _support_row()
    store.save_conversation(row)
    config = _config()
    config["rubric"]["dimensions"] = [{"name": "task_resolved", "description": "Resolved."}]

    preview, result = _approved(store, config, _ResponseProvider(GPT5_MINI_FINDINGS_REPLY, "stop"))

    assert result["completed"] == 1 and result["errors"] == 0, result
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["dimensions"]["task_resolved"]["state"] == "pass"
    assert [finding["issue"] for finding in saved["findings"]] == [
        "card_expiry_identified", "fee_collected", "card_renewed",
        "member_confirmation_of_resolution",
    ]
    assert [finding["message_position"] for finding in saved["findings"]] == [4, 5, 6, 7]


@pytest.mark.parametrize(("label", "stored"), [
    ("Payment processed", "payment_processed"),
    ("  Order placement -- appropriately declined!  ", "order_placement_--_appropriately_declined"),
    ("unresolved_request", "unresolved_request"),
    ("Tool sequence / correctness", "tool_sequence_/_correctness"),
    ("", "helpful"),
    ("***", "helpful"),
    ("x" * 300, "x" * 128),
])
def test_finding_issue_labels_are_normalized_to_identifiers(label, stored):
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    reply = json.dumps({"dimensions": {"helpful": {"verdict": "PASS", "reason": "ok", "findings": [
        {"issue": label, "message_position": 1, "quote": "First synthetic answer.", "reason": "r"},
    ]}}})

    preview, result = _approved(store, _config(), _ResponseProvider(reply, "stop"))

    assert result["completed"] == 1, result
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["findings"][0]["issue"] == stored


def test_non_text_issue_label_is_rejected():
    store = InMemoryStorage()
    store.save_conversation(_row())
    reply = json.dumps({"dimensions": {"helpful": {"verdict": "PASS", "reason": "ok", "findings": [
        {"issue": 7, "message_position": 1, "quote": "First synthetic answer.", "reason": "r"},
    ]}}})

    _, result = _approved(store, _config(), _ResponseProvider(reply, "stop"))

    assert result["errors"] == 1


def test_real_quote_cited_at_the_wrong_message_index_is_still_rejected():
    """Seen live: a correct verdict with exact quotes at off-by-one positions."""
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    reply = json.dumps({"dimensions": {"helpful": {"verdict": "PASS", "reason": "ok", "findings": [
        {"issue": "answered", "message_position": 0, "quote": "First synthetic answer.",
         "reason": "The answer is at position 1, not 0."},
    ]}}})

    _, result = _approved(store, _config(), _ResponseProvider(reply, "end_turn"))

    assert result["errors"] == 1 and result["completed"] == 0


def test_prose_wrapped_fenced_object_reply_is_graded():
    store = InMemoryStorage()
    store.save_conversation(_row())
    reply = ('Here is the evaluation:\n```json\n{"dimensions": {"helpful": {"verdict": "PASS", '
             '"reason": "Both answered.", "findings": []}}}\n```\nLet me know.')

    _, result = _approved(store, _config(), _ResponseProvider(reply, "stop"))

    assert result["completed"] == 1


@pytest.mark.parametrize("finish_reason", ["max_tokens", "length", "MAX_TOKENS"])
def test_reply_cut_off_at_the_output_ceiling_is_a_retryable_error(finish_reason):
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    complete_reply = json.dumps({"dimensions": {"helpful": {"verdict": "PASS", "reason": "ok"}}})

    preview, result = _approved(store, _config(), _ResponseProvider(complete_reply, finish_reason))

    assert result["errors"] == 1 and result["completed"] == 0
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["error"] == "invalid_judge_output"


def test_system_prompt_shows_the_exact_reply_shape_for_this_rubric():
    store = InMemoryStorage()
    store.save_conversation(_row())
    config = _config()
    config["rubric"]["dimensions"].append({
        "name": "tone", "description": "Polite.", "type": "number", "min": 0, "max": 10,
        "passThreshold": 7,
    })
    provider = _ResponseProvider(json.dumps({"dimensions": {
        "helpful": {"verdict": "PASS", "reason": "ok"},
        "tone": {"score": 8, "reason": "polite"},
    }}))

    _, result = _approved(store, config, provider)

    assert result["completed"] == 1
    [request] = provider.requests
    system = request.messages[0]["content"]
    assert ('{"dimensions": {"helpful": {"verdict": "PASS", "reason": "<one short sentence>", '
            '"findings": []}, "tone": {"score": 5.0, "reason": "<one short sentence>", '
            '"findings": []}}}') in system
    assert "no code fence" in system
    assert request.messages[1]["role"] == "user"


def test_numeric_dimension_in_list_form_derives_state_from_threshold():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = _config()
    config["rubric"]["dimensions"] = [{
        "name": "tone", "description": "Polite.", "type": "number", "min": 0, "max": 10,
        "passThreshold": 7,
    }]
    reply = json.dumps({"dimensions": [{"name": "tone", "type": "number", "score": 4,
                                        "reasoning": "curt", "findings": []}]})

    preview, result = _approved(store, config, _ResponseProvider(reply))

    assert result["completed"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["dimensions"] == {"tone": {"state": "fail", "score": 4, "reason": "curt"}}


def test_stray_score_on_a_binary_dimension_is_dropped():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    reply = json.dumps({"dimensions": {"helpful": {"verdict": "PASS", "reason": "ok", "score": 1}}})

    preview, result = _approved(store, _config(), _ResponseProvider(reply))

    assert result["completed"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["dimensions"]["helpful"]["score"] is None


@pytest.mark.parametrize("reply", [
    '{"dimensions": {"helpful": {"verdict": "PASS", "reason": "ok"}, "extra": {"verdict": "PASS", "reason": "x"}}}',
    '{"dimensions": {}}',
    '{"dimensions": [{"name": "helpful", "verdict": "PASS"}, {"name": "helpful", "verdict": "FAIL"}]}',
    '{"dimensions": {"helpful": {"verdict": "MAYBE", "reason": "x"}}}',
    '{"dimensions": {"helpful": {"verdict": "PASS", "reason": "x", "findings": [{"issue": "a", "dimension": "other"}]}}}',
    '{"dimensions": {"helpful": "PASS"}}',
    'I decline to grade this transcript.',
])
def test_replies_that_do_not_answer_the_rubric_are_errors(reply):
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)

    preview, result = _approved(store, _config(), _ResponseProvider(reply))

    assert result["errors"] == 1 and result["completed"] == 0
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["error"] == "invalid_judge_output" and saved["dimensions"] == {}


def test_evaluator_identity_names_the_prompt_and_output_contract(monkeypatch):
    from verdict_eval import conversation_judge, judge_output

    store = InMemoryStorage()
    store.save_conversation(_row())
    before = preview_evaluation(store, tenant_id="alpha", config=_config())
    assert before["evaluator"]["prompt_version"] == (
        f"{conversation_judge.PROMPT_VERSION}/{judge_output.OUTPUT_CONTRACT_VERSION}"
        f"/{conversation_judge.PROMPT_FINGERPRINT}"
    )
    assert re.fullmatch(r"conversation_rubric_v2/judge_output_v2/[0-9a-f]{12}",
                        before["evaluator"]["prompt_version"])

    monkeypatch.setattr(judge_output, "OUTPUT_CONTRACT_VERSION", "judge_output_test")
    after = preview_evaluation(store, tenant_id="alpha", config=_config())

    assert after["evaluatorFingerprint"] != before["evaluatorFingerprint"]
    assert "/judge_output_test/" in after["evaluator"]["prompt_version"]

    monkeypatch.setattr(conversation_judge, "PROMPT_FINGERPRINT", "abcdefabcdef")
    edited = preview_evaluation(store, tenant_id="alpha", config=_config())

    assert edited["evaluatorFingerprint"] not in {before["evaluatorFingerprint"], after["evaluatorFingerprint"]}


def test_prompt_fingerprint_is_a_digest_of_the_template_text():
    import hashlib

    from verdict_eval import conversation_judge

    expected = hashlib.sha256(conversation_judge._PROMPT_TEMPLATE.encode("utf-8")).hexdigest()[:12]
    assert conversation_judge.PROMPT_FINGERPRINT == expected
    assert "{example}" in conversation_judge._PROMPT_TEMPLATE
