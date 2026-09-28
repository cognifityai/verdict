import json

import pytest
from verdict.sessions import validate_rubric
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import import_into_storage
from verdict.telemetry.sources.voice import map_voice_conversation
from verdict_eval.providers import FakeProvider
from verdict_eval.session_evaluation import assess_snapshot, execute_evaluation, preview_evaluation


@pytest.fixture
def snapshot(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "test.db"))
    import_into_storage(
        map_voice_conversation(
            {
                "id": "synthetic",
                "event_at": "2026-09-01T00:00:00Z",
                "end_status": "complete",
                "turns": [
                    {"role": "user", "content": "Synthetic parcel was expected yesterday."},
                    {"role": "assistant", "content": "What is the order number?"},
                    {"role": "user", "content": "ABC123."},
                ],
            },
            ImportContext("voice", "stable-feed", "alpha"),
        ),
        storage,
    )
    yield storage, storage.list_sessions("alpha")[0]
    storage.close()


def rubric(**changes):
    return validate_rubric(
        {
            "name": "custom_quality",
            "version": "1",
            "target": "conversation",
            "dimensions": [
                {
                    "name": "repetition",
                    "description": "Avoid unnecessary repeated questions.",
                    "type": "binary",
                }
            ],
            **changes,
        }
    )


@pytest.mark.parametrize("canary", ["4111111111111111", "123-45-6789", "192.0.2.10",
                                   "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwx"])
def test_imported_assessment_rejects_sensitive_identifiers(snapshot, canary):
    storage, row = snapshot
    result = assess_snapshot(row, rubric(), provider=FakeProvider(), model="external",
                             imported_findings={"dimensions": {"repetition": {
                                 "verdict": "PASS", "reason": "Synthetic reference."}}})
    with pytest.raises(ValueError, match="invalid bounded identifier"):
        storage.save_session_assessment({**result, "session_id": canary, "id": None})
    assert storage.list_session_assessments("alpha") == []


def test_real_storage_evaluation_plan_and_replay(snapshot):
    storage, row = snapshot
    provider = FakeProvider(
        json.dumps(
            {
                "dimensions": {
                    "repetition": {
                        "verdict": "FAIL",
                        "reason": "Synthetic failure.",
                        "findings": [
                            {
                                "message_id": "message-1",
                                "quote": "What is the order",
                                "reason": "Synthetic evidence.",
                                "issue": "repeated_question",
                            }
                        ],
                    }
                }
            }
        )
    )
    provider.name = "openai"
    config = {
        "unit": "conversation",
        "provider": "openai",
        "model": "unfamiliar-local-model",
        "rubric": rubric(),
        "maxCalls": 10,
    }
    preview = preview_evaluation(storage, tenant_id="alpha", config=config)
    config.update(
        plannedSessions=preview["plannedSessions"], planFingerprint=preview["planFingerprint"]
    )
    result = execute_evaluation(
        storage, tenant_id="alpha", config=config, confirm_external_egress=True, provider=provider
    )
    assert result["completed"] == 1
    assert (
        execute_evaluation(
            storage,
            tenant_id="alpha",
            config=config,
            confirm_external_egress=True,
            provider=provider,
        )["completed"]
        == 0
    )
    [saved] = storage.list_session_assessments("alpha")
    assert saved["session_revision"] == row["revision"]
    assert saved["dimensions"]["repetition"]["state"] == "fail"


def test_numeric_without_threshold_retains_score_not_binary(snapshot):
    _, row = snapshot
    definition = rubric(
        dimensions=[
            {
                "name": "rating",
                "description": "Support response quality.",
                "type": "number",
                "min": 0,
                "max": 100,
            }
        ]
    )
    result = assess_snapshot(
        row,
        definition,
        provider=FakeProvider(
            '{"dimensions":{"rating":{"score":42,"reason":"Synthetic rating."}}}'
        ),
        model="fake",
    )
    assert result["status"] == "completed"
    assert result["dimensions"]["rating"] == {
        "score": 42,
        "state": "unclear",
        "reason": "Synthetic rating.",
    }


@pytest.mark.parametrize(
    "output",
    [
        '{"dimensions":{"repetition":{"verdict":"PASS","reason":"Unsupported.","findings":[{"message_id":"message-1","quote":"fabricated quote","reason":"bad"}]}}}',
        '{"dimensions":{}}',
        '{"dimensions":{"repetition":{"verdict":"MISSING","reason":"invalid state"}}}',
        '{"dimensions":{"repetition":{"verdict":"ERROR","reason":"invalid state"}}}',
        '{"dimensions":{"repetition":{"verdict":"PASS","reason":"first"}},"dimensions":{}}',
    ],
)
def test_bad_judge_output_records_error_and_import_rejects(snapshot, output):
    storage, row = snapshot
    result = assess_snapshot(row, rubric(), provider=FakeProvider(output), model="fake")
    assert result["status"] == "error"
    assert storage.save_session_assessment(result)
    with pytest.raises(ValueError):
        assess_snapshot(
            row,
            rubric(),
            provider=FakeProvider(),
            model="external",
            imported_findings=json.loads(output),
        )


def test_response_target_excludes_future_and_rejects_future_evidence(snapshot):
    _, row = snapshot
    observed = []
    provider = FakeProvider(
        lambda request: (
            observed.append(request)
            or '{"dimensions":{"repetition":{"verdict":"PASS","reason":"Synthetic."}}}'
        )
    )
    definition = rubric(target="response")
    result = assess_snapshot(
        row, definition, provider=provider, model="fake", target_message_id="message-1"
    )
    assert result["status"] == "completed"
    assert "ABC123." not in observed[0].messages[1]["content"]
    with pytest.raises(ValueError):
        assess_snapshot(
            row,
            definition,
            provider=provider,
            model="external",
            target_message_id="message-1",
            imported_findings={
                "dimensions": {
                    "repetition": {
                        "verdict": "FAIL",
                        "reason": "Future leakage.",
                        "findings": [
                            {
                                "message_id": "message-2",
                                "quote": "ABC123.",
                                "reason": "Not available then.",
                            }
                        ],
                    }
                }
            },
        )


def test_rubric_edit_is_a_distinct_evaluator(snapshot):
    storage, _ = snapshot
    base = {"unit": "conversation", "provider": "openai", "model": "local", "maxCalls": 1}
    original = preview_evaluation(storage, tenant_id="alpha", config={**base, "rubric": rubric()})
    updated = preview_evaluation(
        storage,
        tenant_id="alpha",
        config={**base, "rubric": rubric(instructions="Different support instructions.")},
    )
    assert original["evaluatorFingerprint"] != updated["evaluatorFingerprint"]


def test_approval_may_not_expand_or_retarget_plan(snapshot):
    storage, _ = snapshot
    provider = FakeProvider(lambda _: pytest.fail("unapproved judge call"))
    provider.name = "openai"
    config = {
        "unit": "conversation",
        "provider": "openai",
        "model": "local",
        "maxCalls": 1,
        "rubric": rubric(),
    }
    plan = preview_evaluation(storage, tenant_id="alpha", config=config)
    config.update(
        plannedSessions=plan["plannedSessions"] * 2, planFingerprint=plan["planFingerprint"]
    )
    with pytest.raises(ValueError):
        execute_evaluation(
            storage,
            tenant_id="alpha",
            config=config,
            confirm_external_egress=True,
            provider=provider,
        )
