"""Real storage → plan → provider port → durable conversation result."""

import json

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
