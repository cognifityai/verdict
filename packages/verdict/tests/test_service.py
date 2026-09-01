from __future__ import annotations

import json
import urllib.error

import pytest
from verdict.dashboard.control_plane import ControlStore
from verdict.service import TENANT, _finding_source_id, _notify, _schedule, run_cycle
from verdict.storage import SQLiteStorage


def test_saved_schedule_runs_content_on_rescan_idempotently(tmp_path) -> None:
    source = tmp_path / "claude"
    source.mkdir()
    history = source / "session.jsonl"
    history.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "timestamp": "2026-08-30T11:00:00Z",
                    "type": "user",
                    "uuid": "user-1",
                    "sessionId": "session-1",
                    "message": {"content": "diagnose the build"},
                },
                {
                    "timestamp": "2026-08-30T11:00:01Z",
                    "type": "assistant",
                    "uuid": "assistant-1",
                    "sessionId": "session-1",
                    "message": {
                        "id": "message-1",
                        "model": "claude-sonnet-4",
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 4, "output_tokens": 3},
                        "content": [{"type": "text", "text": "The build passes."}],
                    },
                },
            )
        )
        + "\n"
    )
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"
    ControlStore(storage_url).append(
        TENANT,
        kind="schedule",
        document_id="daily",
        state="active",
        payload={"intervalHours": 24, "claudeRoot": str(source), "runMonitor": False},
        expected_revision=None,
    )

    first = run_cycle(storage_url, _schedule(storage_url))
    second = run_cycle(storage_url, _schedule(storage_url))

    assert first == second
    assert first["analysis"]["status"] == "completed"
    assert first["capture"] == {
        "files": 1, "stored": 1, "skipped": 0, "skip_reasons": {},
    }
    assert first["monitor"] is None
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        [trace] = storage.list_traces(tenant_id=TENANT, limit=10)
        assert trace.prompt_redacted == "diagnose the build"
        assert trace.response_redacted == "The build passes."
        assert trace.raw_messages == [
            {"role": "user", "content": "diagnose the build"},
            {"role": "assistant", "content": "The build passes."},
        ]
        assert len(storage.list_agent_run_bundles(TENANT)) == 1
    finally:
        storage.close()


def test_service_rejects_missing_or_disabled_schedule(tmp_path) -> None:
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"
    with pytest.raises(ValueError, match="no active daily schedule"):
        _schedule(storage_url)


def test_notification_filters_and_persists_one_successful_delivery(tmp_path) -> None:
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"
    ControlStore(storage_url).append(
        TENANT,
        kind="alert",
        document_id="default",
        state="active",
        payload={
            "destination": "local_log", "findings": True, "drift": False,
        },
        expected_revision=None,
    )
    finding = {"kind": "finding", "code": "tool_error", "severity": "error", "runs": 2}

    first = _notify(
        storage_url, source_kind="analysis", source_id="a" * 64,
        notification=finding,
    )
    second = _notify(
        storage_url, source_kind="analysis", source_id="a" * 64,
        notification=finding,
    )
    filtered = _notify(
        storage_url, source_kind="monitor", source_id="b" * 64,
        notification={"kind": "drift", "status": "alert"},
    )

    assert first["status"] == "delivered"
    assert second == {"status": "already_delivered", **{
        key: first[key] for key in ("notificationId", "destinationFingerprint")
    }}
    assert filtered == {"status": "disabled"}
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        attempts = storage.list_notification_delivery_attempts(
            first["notificationId"], first["destinationFingerprint"],
        )
        assert len(attempts) == 1
        assert attempts[0].outcome.value == "delivered"
    finally:
        storage.close()
    ControlStore(storage_url).append(
        TENANT,
        kind="schedule",
        document_id="daily",
        state="disabled",
        payload={"intervalHours": 24},
        expected_revision=None,
    )
    with pytest.raises(ValueError, match="no active daily schedule"):
        _schedule(storage_url)


def test_finding_identity_is_independent_of_analysis_execution() -> None:
    finding = {
        "code": "tool_error",
        "severity": "error",
        "runs": 2,
        "runIds": ["run-a", "run-b"],
    }

    assert _finding_source_id(finding) == _finding_source_id(dict(finding))
    assert _finding_source_id(finding) != _finding_source_id(finding | {"runs": 3})


def test_webhook_failure_is_recorded_then_retried_without_duplicate_success(
    tmp_path, monkeypatch
) -> None:
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"
    env_name = "VERDICT_TEST_WEBHOOK_URL"
    monkeypatch.setenv(env_name, "https://127.0.0.1.invalid/verdict")
    ControlStore(storage_url).append(
        TENANT,
        kind="alert",
        document_id="default",
        state="active",
        payload={
            "destination": "webhook",
            "webhookUrlEnvVar": env_name,
            "findings": True,
            "drift": True,
        },
        expected_revision=None,
    )
    requests = []

    class Accepted:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def delivery(request, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            raise urllib.error.URLError("offline")
        return Accepted()

    monkeypatch.setattr("verdict.service.urllib.request.urlopen", delivery)
    notification = {
        "kind": "finding",
        "code": "tool_error",
        "severity": "error",
        "runs": 2,
    }

    failed = _notify(
        storage_url,
        source_kind="analysis",
        source_id="a" * 64,
        notification=notification,
    )
    delivered = _notify(
        storage_url,
        source_kind="analysis",
        source_id="a" * 64,
        notification=notification,
    )
    duplicate = _notify(
        storage_url,
        source_kind="analysis",
        source_id="a" * 64,
        notification=notification,
    )

    assert failed["status"] == "failed"
    assert delivered["status"] == "delivered"
    assert duplicate["status"] == "already_delivered"
    assert failed["notificationId"] == delivered["notificationId"]
    assert len(requests) == 2
    request, timeout = requests[1]
    assert timeout == 10
    assert request.get_header("Idempotency-key") == delivered["notificationId"]
    assert json.loads(request.data) == {
        "source": "verdict",
        "notification": notification,
    }
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        attempts = storage.list_notification_delivery_attempts(
            delivered["notificationId"], delivered["destinationFingerprint"]
        )
    finally:
        storage.close()
    assert [attempt.outcome.value for attempt in attempts] == ["delivered", "failed"]
    assert attempts[0].http_status == 202
    assert attempts[1].error_code == "transport_error"
