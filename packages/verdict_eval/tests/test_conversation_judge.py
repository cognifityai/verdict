"""Real storage → plan → provider port → durable conversation result."""

import json
from pathlib import Path

import pytest
from verdict.conversation_monitoring import preview_conversation_comparison
from verdict.conversations import conversation_from_voice, validate_conversation
from verdict.storage.memory import InMemoryStorage
from verdict.telemetry.model import ImportContext
from verdict_eval.conversation_judge import execute_evaluation, preview_evaluation
from verdict_eval.providers import FakeProvider

_ELEMENT_RUBRIC = Path(__file__).resolve().parents[3] / "examples/telemetry/element-rubric.example.json"


def _element_response():
    def item(name):
        return {"phase": "general", "element": name, "applicable": True, "adequacy": "adequate",
                "description": "Fictional evidence.", "quote": None, "message_position": None}
    return {"route": "standard", "enabled_phases": [], "categories": {
        "coverage": {"confidence": 0.9, "elements": [item("question"), item("followup")]},
        "errors": {"confidence": 0.9, "elements": [item("accuracy")]},
        "timeline": {"confidence": 0.9, "elements": [item("sequence")]},
    }}


def test_structured_judge_runs_through_existing_plan_and_storage():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = {"provider": "openai", "model": "fictional-model", "maxCalls": 1,
              "maxOutputTokens": 8192, "rubric": json.loads(_ELEMENT_RUBRIC.read_text())}
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    calls = []
    provider = FakeProvider(lambda request: calls.append(request) or json.dumps(_element_response()))
    provider.name = "openai"
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert result["completed"] == 1
    assert len(calls) == 1
    assert "Do not calculate category scores" in calls[0].messages[0]["content"]
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["structured"]["computed"]["overall"] == 100
    assert saved["dimensions"]["safety_gate"]["state"] == "pass"


def test_structured_judge_cannot_return_invented_final_score():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    config = {"provider": "openai", "model": "fictional-model", "maxCalls": 1,
              "rubric": json.loads(_ELEMENT_RUBRIC.read_text())}
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    output = {**_element_response(), "overall": 100}
    provider = FakeProvider(json.dumps(output))
    provider.name = "openai"
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert result["errors"] == 1
    [saved] = store.list_conversation_assessments("alpha", row["id"], preview["evaluatorFingerprint"])
    assert saved["status"] == "error"
    assert saved["dimensions"] == {}


def test_missing_source_phases_are_previewed_as_alternate_only():
    store = InMemoryStorage()
    row = _row()
    store.save_conversation(row)
    rubric = json.loads(_ELEMENT_RUBRIC.read_text())
    rubric["catalog"]["coverage"][0]["phase"] = "intake"
    config = {"provider": "openai", "model": "fictional-model", "maxCalls": 1,
              "rubric": rubric}
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    assert preview["eligibleTargets"] == 1
    assert preview["alternateOnlyTargets"] == 1
    alternate = {"route": "alternate", "alternate": {
        "scores": {"recognition": 5, "response": 4},
        "score_reasons": {"recognition": "Detected.", "response": "Timely."},
        "adequacy": "adequate", "rationale": "Synthetic alternate condition.",
        "context": "fictional urgent case", "critical_flags": [],
    }}
    provider = FakeProvider(json.dumps(alternate))
    provider.name = "openai"
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert result["completed"] == 1


def test_synthetic_voice_to_structured_judge_to_binary_monitor():
    store = InMemoryStorage()
    context = ImportContext(adapter="file", source_scope="synthetic", tenant_id="alpha")
    for identifier, ended, question in [
        ("before", "2026-09-01T12:00:00Z", "Before synthetic question."),
        ("after", "2026-09-03T12:00:00Z", "After synthetic question."),
    ]:
        row = conversation_from_voice({
            "conversation_id": identifier, "end_status": "complete", "ended_at": ended,
            "turns": [{"speaker": "caller", "text": question},
                      {"speaker": "agent", "text": "Synthetic answer."}],
        }, context)
        assert row is not None
        store.save_conversation(row)
    config = {"provider": "openai", "model": "fictional-model", "maxCalls": 2,
              "rubric": json.loads(_ELEMENT_RUBRIC.read_text())}
    preview = preview_evaluation(store, tenant_id="alpha", config=config)
    assert preview["plannedCalls"] == 2

    def fake_judge(request):
        output = _element_response()
        if "After synthetic question." in request.messages[1]["content"]:
            for element in output["categories"]["coverage"]["elements"]:
                element["adequacy"] = "critical"
        return json.dumps(output)

    provider = FakeProvider(fake_judge)
    provider.name = "openai"
    result = execute_evaluation(store, tenant_id="alpha", config={
        **config, "plannedTargets": preview["plannedTargets"],
        "planFingerprint": preview["planFingerprint"],
    }, confirm_external_egress=True, provider=provider)
    assert result["completed"] == 2
    history = preview_conversation_comparison(store, tenant_id="alpha", payload={
        "analysisUnit": "conversation", "referenceStart": "2026-09-01T00:00:00Z",
        "referenceEnd": "2026-09-03T00:00:00Z", "currentStart": "2026-09-03T00:00:00Z",
        "currentEnd": "2026-09-05T00:00:00Z",
        "evaluatorFingerprint": preview["evaluatorFingerprint"], "dimension": "safety_gate",
    })
    assert history["reference"]["passRate"] == 1
    assert history["current"]["passRate"] == 0


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
