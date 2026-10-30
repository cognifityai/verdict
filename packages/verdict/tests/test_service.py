from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.analysis_records import (
    AnalysisRunStatus,
    DeliveryOutcome,
    DeterministicAnalysisRun,
    NotificationDeliveryAttempt,
)
from verdict.client import _resolve_storage
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.worker_status import WorkerCycleStatus, WorkerDue, WorkerHeartbeat
from verdict.delivery_queue import create_intent
from verdict.monitoring import (
    MonitorPolicy,
    compare_manifest,
    plan_historical_manifest,
    plan_prospective_manifest,
    trace_monitor_units,
)
from verdict.schema import DimensionScore, Judgment, Trace, Verdict
from verdict.service import (
    TENANT,
    CycleBusy,
    _finding_source_id,
    _schedule,
    cycle_guard,
    poll_cycle,
    run_cycle,
)
from verdict.storage import SQLiteStorage


def _queue_test_analysis(
    storage_url: str, source_id: str, notification: dict[str, object], *,
    tenant_id: str = TENANT, webhook_url: str | None = None,
    when: datetime | None = None,
):
    instant = when or datetime.now(timezone.utc)
    alert = next(item for item in ControlStore(storage_url).list_current(tenant_id)
                 if item["kind"] == "alert" and item["documentId"] == "default")
    intent = create_intent(
        alert, tenant_id=tenant_id, source_kind="analysis", source_id=source_id,
        notification=notification, webhook_url=webhook_url, created_at=instant,
    )
    assert intent is not None
    run = DeterministicAnalysisRun(
        analysis_id=hashlib.sha256(f"{tenant_id}:{source_id}".encode()).hexdigest(),
        tenant_id=tenant_id, scope_key="agent-and-trace", cutoff=instant,
        completed_at=instant, status=AnalysisRunStatus.COMPLETED,
        analyzer_version="test-v1", input_fingerprint=hashlib.sha256(b"input").hexdigest(),
        result={"findings": [notification]},
    )
    storage = _resolve_storage(storage_url)
    try:
        storage.save_worker_analysis_run(run, [intent])
    finally:
        storage.close()
    return intent


def _exercise_custom_tenant_worker(storage_url: str) -> None:
    import verdict.service as service

    control = ControlStore(storage_url)
    control.append(
        "customer-a", kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 24, "analyzeOnly": True,
                 "runMonitor": False, "includeApprovedEvaluations": False},
        expected_revision=None,
    )
    control.append(
        TENANT, kind="schedule", document_id="daily", state="disabled",
        payload={"intervalHours": 24, "analyzeOnly": True},
        expected_revision=None,
    )
    assert service.main(["--storage", storage_url, "--tenant-id", "customer-a", "--once"]) == 0
    assert WorkerCycleStatus(storage_url).read("customer-a")["state"] == "completed"
    assert WorkerCycleStatus(storage_url).read(TENANT)["state"] == "never_run"
    assert WorkerDue(storage_url).read("customer-a")["scheduleRevision"] == 1
    assert WorkerDue(storage_url).read(TENANT) is None


def test_service_cli_runs_only_the_selected_customer_tenant(tmp_path) -> None:
    _exercise_custom_tenant_worker(f"sqlite:///{tmp_path / 'customer.db'}")


def test_continuous_poll_reads_only_configured_customer_tenant(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'customer-poll.db'}"
    ControlStore(url).append(
        "customer-a", kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 24, "analyzeOnly": True,
                 "includeApprovedEvaluations": False}, expected_revision=None,
    )
    assert poll_cycle(url)["status"] == "no_active_schedule"
    result = poll_cycle(url, tenant_id="customer-a")
    assert result["analysis"]["status"] == "completed"
    assert WorkerCycleStatus(url).read("customer-a")["state"] == "completed"
    assert WorkerCycleStatus(url).read(TENANT)["state"] == "never_run"
    assert poll_cycle(url, tenant_id="customer-a")["status"] == "waiting"


def test_service_cli_rejects_invalid_tenant_before_touching_store(tmp_path, capsys) -> None:
    import verdict.service as service

    path = tmp_path / "must-not-exist.db"
    with pytest.raises(SystemExit) as error:
        service.main(["--storage", f"sqlite:///{path}", "--tenant-id", "../secret", "--once"])
    assert error.value.code == 2
    assert "non-sensitive bounded routing identifier" in capsys.readouterr().err
    assert not path.exists()


def test_live_postgres_service_cli_runs_only_the_selected_customer_tenant() -> None:
    from urllib.parse import urlencode

    from psycopg.conninfo import conninfo_to_dict

    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        options = conninfo_to_dict(isolated)["options"]
        separator = "&" if "?" in dsn else "?"
        _exercise_custom_tenant_worker(dsn + separator + urlencode({"options": options}))


def test_live_postgres_service_cli_uses_valid_keyword_conninfo() -> None:
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        _exercise_custom_tenant_worker(isolated)


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

    assert {**first, "analysis": {**first["analysis"], "verifiedRevision": None}} == {
        **second, "analysis": {**second["analysis"], "verifiedRevision": None}
    }
    assert second["analysis"]["verifiedRevision"] > first["analysis"]["verifiedRevision"]
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


def test_cycle_guard_rejects_overlap_and_recovers_after_release(tmp_path) -> None:
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"
    with cycle_guard(storage_url):
        with pytest.raises(CycleBusy):
            run_cycle(storage_url, {"analyzeOnly": True})
    assert WorkerCycleStatus(storage_url).read(TENANT)["state"] == "never_run"
    result = run_cycle(storage_url, {"analyzeOnly": True})
    assert result["analysis"]["status"] == "completed"
    assert WorkerCycleStatus(storage_url).read(TENANT)["state"] == "completed"


def test_worker_cycle_records_current_revision_and_analysis_error(tmp_path, monkeypatch):
    import verdict.service as service

    url = f"sqlite:///{tmp_path / 'cycle-outcome.db'}"
    ControlStore(url).append(TENANT, kind="schedule", document_id="daily",
                             state="active", payload={"analyzeOnly": True},
                             expected_revision=None)
    completed = run_cycle(url, _schedule(url))
    assert completed["analysis"]["status"] == "completed"
    first = WorkerCycleStatus(url).read(TENANT)
    assert first["trigger"] == "worker" and first["scheduleRevision"] == 1
    assert first["state"] == "completed" and first["lastSuccessAt"] is not None
    monkeypatch.setattr(service, "run_analysis", lambda *args, **kwargs: {
        "analysisState": {"status": "error"}, "findings": [],
    })
    failed = run_cycle(url, _schedule(url))
    assert failed["analysis"]["status"] == "error"
    latest = WorkerCycleStatus(url).read(TENANT)
    assert latest["state"] == "failed" and latest["failureCode"] == "analysis_error"
    assert latest["lastSuccessAt"] == first["lastSuccessAt"]


def test_worker_cycle_reports_skipped_local_history_as_incomplete(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    (root / "broken.jsonl").write_bytes(b"{malformed}\n")
    url = f"sqlite:///{tmp_path / 'incomplete-capture.db'}"

    result = run_cycle(url, {
        "codexRoot": str(root), "runMonitor": False,
        "includeApprovedEvaluations": False,
    })

    assert result["capture"]["skip_reasons"] == {"malformed_jsonl": 1}
    assert result["analysis"]["status"] == "completed"
    assert result["cycleFailureCode"] == "capture_incomplete"
    assert WorkerCycleStatus(url).read(TENANT)["failureCode"] == "capture_incomplete"


def test_live_postgres_worker_reports_skipped_local_history_as_incomplete(tmp_path):
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    root = tmp_path / "codex"
    root.mkdir()
    (root / "broken.jsonl").write_bytes(b"{malformed}\n")
    with isolated_test_dsn(dsn) as url:
        result = run_cycle(url, {
            "codexRoot": str(root), "runMonitor": False,
            "includeApprovedEvaluations": False,
        })
        assert result["capture"]["skip_reasons"] == {"malformed_jsonl": 1}
        assert result["cycleFailureCode"] == "capture_incomplete"
        assert WorkerCycleStatus(url).read(TENANT)["failureCode"] == "capture_incomplete"


def test_live_postgres_cycle_status_parity():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
                                    allow_any_database=False)
    if dsn is None:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        status = WorkerCycleStatus(isolated)
        now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
        assert status.read("tenant")["state"] == "never_run"
        status.begin("tenant", trigger="worker", schedule_revision=1, now=now)
        assert status.read("tenant")["state"] == "started"
        status.finish("tenant", state="completed", analysis_status="completed",
                      failure_code=None, now=now + timedelta(seconds=1))
        assert status.read("tenant")["lastSuccessAt"] == (now + timedelta(seconds=1)).isoformat()
        status.begin("tenant", trigger="manual", schedule_revision=None,
                     now=now + timedelta(seconds=2))
        status.finish("tenant", state="failed", analysis_status="error",
                      failure_code="analysis_error", now=now + timedelta(seconds=3))
        assert status.read("tenant")["lastSuccessAt"] == (now + timedelta(seconds=1)).isoformat()
        assert status.read("other")["state"] == "never_run"
        due = WorkerDue(isolated)
        assert due.ready("tenant", 1, now=now)
        due.begin("tenant", 1, now=now)
        assert not due.ready("tenant", 1, now=now + timedelta(seconds=29))
        due.finish("tenant", 1, interval_hours=1, now=now + timedelta(seconds=3))
        assert due.read("tenant")["nextDueAt"] == (now + timedelta(hours=1, seconds=3)).isoformat()
        assert due.ready("tenant", 2, now=now + timedelta(seconds=4))
        assert due.read("other") is None


def test_cycle_guard_uses_the_storage_file_for_tilde_and_relative_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for url, expected in (
        ("sqlite:///~/tilde.db", tmp_path / "tilde.db"),
        ("sqlite:///./relative.db", tmp_path / "relative.db"),
    ):
        storage = _resolve_storage(url)
        storage.close()
        assert expected.exists()
        with cycle_guard(url):
            assert expected.with_name(expected.name + ".service.lock").exists()
            with pytest.raises(CycleBusy):
                with cycle_guard(f"sqlite:///{expected}"):
                    pass


def test_continuous_cli_survives_busy_cycle_and_runs_after_release(tmp_path) -> None:
    storage_url = f"sqlite:///{tmp_path / 'busy-cli.db'}"
    ControlStore(storage_url).append(
        TENANT, kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 1, "analyzeOnly": True}, expected_revision=None,
    )
    command = [sys.executable, "-u", "-c",
               "import sys; from verdict.service import main; sys.exit(main())",
               "--storage", storage_url]
    with cycle_guard(storage_url):
        once = subprocess.run([*command, "--once"], capture_output=True, text=True,
                              timeout=5)
        assert once.returncode == 2
        assert json.loads(once.stdout)["cycleRan"] is False
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=os.environ.copy())
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.3)
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15), "service did not resume after the lease released"
        result = json.loads(process.stdout.readline())
        assert result["analysis"]["status"] == "completed"
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert run_cycle(storage_url, {"analyzeOnly": True})["analysis"]["status"] == "completed"


def test_once_cli_exit_code_matches_recorded_cycle_outcome(tmp_path, monkeypatch) -> None:
    import verdict.service as service

    storage_url = f"sqlite:///{tmp_path / 'once.db'}"
    monkeypatch.setattr(service, "_schedule", lambda *_args, **_kwargs: {"analyzeOnly": True})
    monkeypatch.setattr(service, "run_cycle", lambda *_args, **_kwargs: {
        "analysis": {"status": "error"}, "cycleFailureCode": "analysis_error",
    })
    assert service.main(["--storage", storage_url, "--once"]) == 1
    monkeypatch.setattr(service, "run_cycle", lambda *_args, **_kwargs: {
        "analysis": {"status": "completed"}, "cycleFailureCode": None,
    })
    assert service.main(["--storage", storage_url, "--once"]) == 0


def test_once_cli_reports_real_missing_selected_monitor_as_failure(tmp_path) -> None:
    storage_url = f"sqlite:///{tmp_path / 'missing-monitor.db'}"
    ControlStore(storage_url).append(
        TENANT, kind="schedule", document_id="daily", state="active",
        payload={"analyzeOnly": True, "runMonitor": True,
                 "includeApprovedEvaluations": False}, expected_revision=None,
    )
    command = [sys.executable, "-c",
               "import sys; from verdict.service import main; sys.exit(main())",
               "--storage", storage_url, "--once"]
    run = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert run.returncode == 1
    assert json.loads(run.stdout)["cycleFailureCode"] == "monitor_incomplete"
    assert WorkerCycleStatus(storage_url).read(TENANT)["failureCode"] == "monitor_incomplete"


def test_worker_poll_observes_save_pause_resume_and_manual_interleaving(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'poll.db'}"
    now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    assert poll_cycle(url, now=now)["status"] == "no_active_schedule"
    control = ControlStore(url)
    first = control.append(TENANT, kind="schedule", document_id="daily", state="active",
                           payload={"intervalHours": 24, "analyzeOnly": True,
                                    "includeApprovedEvaluations": False}, expected_revision=None)
    ran = poll_cycle(url, now=now)
    assert ran["analysis"]["status"] == "completed"
    assert WorkerDue(url).read(TENANT)["scheduleRevision"] == first["revision"]
    assert poll_cycle(url, now=now + timedelta(seconds=1))["status"] == "waiting"
    status = WorkerCycleStatus(url)
    status.begin(TENANT, trigger="manual", schedule_revision=None, now=now + timedelta(minutes=1))
    status.finish(TENANT, state="completed", analysis_status="completed", failure_code=None,
                  now=now + timedelta(minutes=1, seconds=1))
    assert poll_cycle(url, now=now + timedelta(minutes=2))["status"] == "waiting"
    disabled = control.append(TENANT, kind="schedule", document_id="daily", state="disabled",
                              payload=first["payload"], expected_revision=first["revision"])
    assert poll_cycle(url, now=now + timedelta(minutes=3))["status"] == "no_active_schedule"
    resumed = control.append(TENANT, kind="schedule", document_id="daily", state="active",
                             payload={**disabled["payload"], "includeApprovedEvaluations": False},
                             expected_revision=disabled["revision"])
    assert poll_cycle(url, now=now + timedelta(minutes=4))["analysis"]["status"] == "completed"
    assert WorkerDue(url).read(TENANT)["scheduleRevision"] == resumed["revision"]


def test_worker_poll_cools_failed_and_interrupted_cycles(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'retry.db'}"
    now = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
    ControlStore(url).append(TENANT, kind="schedule", document_id="daily", state="active",
                             payload={"intervalHours": 1, "analyzeOnly": True,
                                      "runMonitor": True, "includeApprovedEvaluations": False},
                             expected_revision=None)
    failed = poll_cycle(url, now=now)
    assert failed["cycleFailureCode"] == "monitor_incomplete"
    assert poll_cycle(url, now=now + timedelta(seconds=30))["status"] == "waiting"
    due = WorkerDue(url)
    due.begin(TENANT, 1, now=now + timedelta(hours=2))
    assert poll_cycle(url, now=now + timedelta(hours=2, seconds=29))["status"] == "waiting"
    assert poll_cycle(url, now=now + timedelta(hours=2, seconds=30))["cycleFailureCode"] == "monitor_incomplete"


def test_one_shot_worker_updates_due_before_continuous_worker_polls(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'once-and-loop.db'}"
    ControlStore(url).append(TENANT, kind="schedule", document_id="daily", state="active",
                             payload={"intervalHours": 24, "analyzeOnly": True,
                                      "includeApprovedEvaluations": False}, expected_revision=None)
    assert run_cycle(url, _schedule(url))["cycleFailureCode"] is None
    assert poll_cycle(url)["status"] == "waiting"


@pytest.mark.parametrize("replacement_state", ["disabled", "active"])
def test_one_shot_rejects_stale_saved_plan_before_capture_or_due_change(
    tmp_path, monkeypatch, replacement_state
) -> None:
    import verdict.service as service

    url = f"sqlite:///{tmp_path / 'stale-once.db'}"
    control = ControlStore(url)
    first = control.append(
        TENANT, kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 1, "includeApprovedEvaluations": True},
        expected_revision=None,
    )
    stale = _schedule(url)
    replacement = control.append(
        TENANT, kind="schedule", document_id="daily", state=replacement_state,
        payload={"intervalHours": 24, "includeApprovedEvaluations": False},
        expected_revision=first["revision"],
    )
    if replacement_state == "active":
        WorkerDue(url).begin(TENANT, replacement["revision"])
    due_before = WorkerDue(url).read(TENANT)
    monkeypatch.setattr(service, "capture_local_agents", lambda *_args, **_kwargs:
                        pytest.fail("stale plan reached capture"))

    with pytest.raises(ValueError, match="saved schedule changed or paused"):
        run_cycle(url, stale)

    assert WorkerDue(url).read(TENANT) == due_before
    assert WorkerCycleStatus(url).read(TENANT)["state"] == "never_run"


def test_continuous_worker_survives_cycle_error_and_preserves_cooldown(
    tmp_path, monkeypatch
) -> None:
    import contextlib

    import verdict.service as service

    url = f"sqlite:///{tmp_path / 'recover.db'}"
    ControlStore(url).append(
        TENANT, kind="schedule", document_id="daily", state="active",
        payload={"intervalHours": 1, "includeApprovedEvaluations": False},
        expected_revision=None,
    )
    monkeypatch.setattr(service, "_run_cycle_unlocked", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("transient capture failure")))
    monkeypatch.setattr(service, "worker_presence", lambda *_args, **_kwargs: contextlib.nullcontext())

    class EndAfterOneWait(Exception):
        pass

    class WaitOnce:
        def is_set(self):
            return False

        def wait(self, seconds):
            assert seconds == 30
            raise EndAfterOneWait

    monkeypatch.setattr(service.threading, "Event", WaitOnce)
    with pytest.raises(EndAfterOneWait):
        service.main(["--storage", url])
    assert WorkerCycleStatus(url).read(TENANT)["failureCode"] == "capture_or_cycle_error"
    assert WorkerDue(url).read(TENANT)["scheduleRevision"] == 1
    assert poll_cycle(url)["status"] == "waiting"


def test_worker_presence_survives_initial_heartbeat_failure(monkeypatch, tmp_path) -> None:
    import verdict.service as service

    calls = []

    class FlakyHeartbeat:
        def __init__(self, _storage_url):
            pass

        def touch(self, _tenant, _worker_id):
            calls.append("touch")
            if len(calls) == 1:
                raise OSError("temporary database failure")

        def remove(self, _tenant, _worker_id):
            calls.append("remove")

    monkeypatch.setattr(service, "WorkerHeartbeat", FlakyHeartbeat)
    with service.worker_presence(f"sqlite:///{tmp_path / 'startup.db'}"):
        calls.append("entered")
    assert "entered" in calls
    assert "remove" in calls


def test_worker_presence_is_scoped_to_configured_tenant(tmp_path) -> None:
    import verdict.service as service

    url = f"sqlite:///{tmp_path / 'presence.db'}"
    with service.worker_presence(url, tenant_id="customer-a"):
        assert service.WorkerHeartbeat(url).read("customer-a")["status"] == "recent"
        assert service.WorkerHeartbeat(url).read(TENANT)["status"] != "recent"
    assert service.WorkerHeartbeat(url).read("customer-a")["status"] != "recent"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX worker signals")
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_continuous_worker_clears_heartbeat_on_supervisor_stop(tmp_path, stop_signal) -> None:
    url = f"sqlite:///{tmp_path / 'stop.db'}"
    SQLiteStorage(str(tmp_path / "stop.db")).close()
    WorkerHeartbeat(url).read(TENANT)
    process = subprocess.Popen(
        [sys.executable, "-c", "from verdict.service import main; raise SystemExit(main())",
         "--storage", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while WorkerHeartbeat(url).read(TENANT)["status"] != "recent":
            assert process.poll() is None, "worker exited before publishing heartbeat"
            assert time.monotonic() < deadline, "worker did not publish heartbeat"
            time.sleep(0.05)
        process.send_signal(stop_signal)
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert b"Traceback" not in stderr
        assert WorkerHeartbeat(url).read(TENANT)["status"] == "unverified"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX worker signals")
@pytest.mark.parametrize(
    ("stop_signal", "poll_completed"),
    [(signal.SIGTERM, True), (signal.SIGINT, False)],
)
def test_continuous_worker_signal_during_poll(tmp_path, stop_signal, poll_completed) -> None:
    path = tmp_path / "active-stop.db"
    url = f"sqlite:///{path}"
    marker = tmp_path / "poll-state"
    release = tmp_path / "release-poll"
    handled = tmp_path / "term-handled"
    SQLiteStorage(str(path)).close()
    code = """
import sys
import time
from pathlib import Path
import verdict.service as service

marker = Path(sys.argv[1])
release = Path(sys.argv[3])
handled = Path(sys.argv[4])
register = service.signal.signal
def observe_term(number, handler):
    if number == service.signal.SIGTERM and callable(handler):
        def observed(signum, frame):
            handled.write_text('handled')
            return handler(signum, frame)
        return register(number, observed)
    return register(number, handler)
service.signal.signal = observe_term
def poll(_storage, **_kwargs):
    ready = marker.with_suffix('.ready')
    ready.write_text('entered')
    ready.replace(marker)
    while not release.exists():
        time.sleep(0.01)
    marker.write_text('completed')
    return {'status': 'waiting', 'cycleRan': False}
service.poll_cycle = poll
raise SystemExit(service.main(['--storage', sys.argv[2]]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(marker), url, str(release), str(handled)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert process.poll() is None, "worker exited before entering poll"
            assert time.monotonic() < deadline, "worker did not enter poll"
            time.sleep(0.01)
        assert WorkerHeartbeat(url).read(TENANT)["status"] == "recent"
        process.send_signal(stop_signal)
        if stop_signal == signal.SIGTERM:
            deadline = time.monotonic() + 15
            while not handled.exists():
                assert process.poll() is None, "worker exited before handling SIGTERM"
                assert time.monotonic() < deadline, "worker did not handle SIGTERM"
                time.sleep(0.01)
            release.touch()
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode(errors="replace")
        assert b"Traceback" not in stderr
        assert (marker.read_text() == "completed") is poll_completed
        assert WorkerHeartbeat(url).read(TENANT)["status"] == "unverified"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_two_worker_pollers_cannot_start_the_same_due_revision(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'two-workers.db'}"
    ControlStore(url).append(TENANT, kind="schedule", document_id="daily", state="active",
                             payload={"intervalHours": 24, "analyzeOnly": True,
                                      "includeApprovedEvaluations": False}, expected_revision=None)

    def attempt():
        try:
            return poll_cycle(url)
        except CycleBusy:
            return {"status": "busy", "cycleRan": False}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result.get("analysis", {}).get("status") == "completed" for result in results) == 1
    assert sum(result.get("cycleRan") is False for result in results) == 1
    assert WorkerDue(url).read(TENANT)["scheduleRevision"] == 1


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
    intent = _queue_test_analysis(storage_url, "a" * 64, finding)
    first = poll_cycle(storage_url)["notifications"]
    second = poll_cycle(storage_url)["notifications"]
    alert = next(item for item in ControlStore(storage_url).list_current(TENANT)
                 if item["kind"] == "alert")
    filtered = create_intent(
        alert, tenant_id=TENANT, source_kind="monitor", source_id="b" * 64,
        notification={"kind": "drift", "status": "alert"}, webhook_url=None,
    )

    assert first[0]["status"] == "delivered" and first[0]["notificationId"] == intent.notification_id
    assert second == []
    assert filtered is None
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        attempts = storage.list_notification_delivery_attempts(
            intent.notification_id, intent.destination_fingerprint,
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


def test_notification_identity_and_delivery_are_isolated_by_worker_tenant(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'notification-tenants.db'}"
    for tenant in (TENANT, "customer-a"):
        ControlStore(url).append(
            tenant, kind="alert", document_id="default", state="active",
            payload={"destination": "local_log", "findings": True, "drift": False},
            expected_revision=None,
        )
    notification = {"kind": "finding", "code": "tool_error", "severity": "error"}
    local = _queue_test_analysis(url, "same", notification)
    customer = _queue_test_analysis(url, "same", notification, tenant_id="customer-a")
    assert poll_cycle(url)["notifications"][0]["status"] == "delivered"
    assert poll_cycle(url, tenant_id="customer-a")["notifications"][0]["status"] == "delivered"
    assert local.notification_id != customer.notification_id
    storage = SQLiteStorage(str(tmp_path / "notification-tenants.db"))
    try:
        local_attempts = storage.list_notification_delivery_attempts_for_tenant(TENANT)
        customer_attempts = storage.list_notification_delivery_attempts_for_tenant("customer-a")
    finally:
        storage.close()
    assert len(local_attempts) == len(customer_attempts) == 1
    assert local_attempts[0].notification_id == local.notification_id
    assert customer_attempts[0].notification_id == customer.notification_id


@pytest.mark.parametrize("tenant", [TENANT, "customer-a"])
def test_scheduled_monitor_uses_the_frozen_evaluator_and_dimensions(
    tmp_path, monkeypatch, tenant,
) -> None:
    storage_url = f"sqlite:///{tmp_path / 'verdict.db'}"
    selected = "a" * 64
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    for index in range(20):
        trace_id = f"historical-{index:03d}"
        storage.insert_trace(Trace(
                trace_id=trace_id, tenant_id=tenant,
                started_at=now + timedelta(minutes=index),
                prompt_redacted="request", response_redacted="ok",
            ended_at=now + timedelta(minutes=index, seconds=1),
        ))
        storage.insert_judgment(Judgment(
            judgment_id=f"judgment-{trace_id}", trace_id=trace_id,
            evaluator_provider="anthropic", judge_models=["judge"],
            evaluator_fingerprint=selected, expected_dimensions=["quality"],
            rubric_name="quality", rubric_version="1",
            dimensions=[DimensionScore("quality", Verdict.PASS)],
        ))
    policy = MonitorPolicy(
        "policy", f"{tenant}:application:trace",
        reference_ratio=0.5, minimum_reference=5, minimum_current=5,
        prospective_target=10, minimum_effect=0.5,
        evaluator_fingerprint=selected, evaluator_dimensions=("quality",),
    )
    judgments = {
        row.trace_id: row
        for row in storage.list_latest_judgments_for_evaluator(
            tenant, selected, limit=100,
        )
    }
    historical_units = trace_monitor_units(
        storage.list_traces(tenant_id=tenant, limit=100),
        judgments_by_trace=judgments, evaluator_dimensions=("quality",),
    )
    historical = plan_historical_manifest(
        historical_units, policy, cutoff=now + timedelta(hours=1),
    )
    storage.save_monitor_policy(policy)
    storage.save_monitor_snapshot(
        policy.policy_id, historical,
        compare_manifest(historical_units, historical, policy),
    )
    storage.activate_monitor_policy(
        policy.scope_key, policy.policy_id, expected_active_policy_id=None,
    )
    collecting = plan_prospective_manifest(historical, historical_units, policy)
    storage.save_monitor_snapshot(
        policy.policy_id, collecting,
        compare_manifest(historical_units, collecting, policy),
    )
    for index in range(10):
        trace_id = f"current-{index:03d}"
        storage.insert_trace(Trace(
                trace_id=trace_id, tenant_id=tenant,
                started_at=now + timedelta(hours=2, minutes=index),
                ended_at=now + timedelta(hours=2, minutes=index, seconds=1),
                prompt_redacted="request", response_redacted="ok",
        ))
        storage.insert_judgment(Judgment(
            judgment_id=f"judgment-{trace_id}", trace_id=trace_id,
            evaluator_provider="anthropic", judge_models=["judge"],
            evaluator_fingerprint=selected, expected_dimensions=["quality"],
            rubric_name="quality", rubric_version="1",
            dimensions=[DimensionScore("quality", Verdict.FAIL)],
        ))
    storage.close()

    env_name = "VERDICT_TEST_DRIFT_WEBHOOK"
    monkeypatch.setenv(env_name, "https://127.0.0.1.invalid/verdict")
    ControlStore(storage_url).append(
        tenant, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": env_name,
                 "findings": False, "drift": True}, expected_revision=None,
    )

    def rejected_delivery(_request, timeout):
        assert timeout == 10
        raise urllib.error.URLError("synthetic offline destination")

    monkeypatch.setattr("verdict.service._open_webhook", rejected_delivery)

    result = run_cycle(storage_url, {"runMonitor": True}, tenant_id=tenant)

    assert result["monitor"]["status"] == "alert"
    assert result["analysis"]["status"] == "completed"
    assert [notice["status"] for notice in result["notifications"]] == ["failed"]
    assert result["cycleFailureCode"] == "delivery_failed"
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        snapshot = storage.get_latest_monitor_snapshot(policy.policy_id)
    finally:
        storage.close()
    assert snapshot is not None
    quality = next(
        metric for metric in snapshot[1].metrics
        if metric.metric == "judge.quality.pass"
    )
    assert quality.reference_value == 1.0
    assert quality.current_value == 0.0
    assert quality.alert is True

    class Accepted:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    delivered = []

    def accepted_delivery(request, timeout):
        delivered.append((request.get_header("Idempotency-key"), request.data))
        return Accepted()

    monkeypatch.setattr("verdict.service._open_webhook", accepted_delivery)
    # Delivery obligations must continue even when the source schedule is not
    # due. The first failed alert is no longer the latest comparison bucket.
    poll_cycle(
        storage_url, tenant_id=tenant,
        now=datetime.now(timezone.utc) + timedelta(seconds=31),
    )
    attempts = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        saved = attempts.list_notification_delivery_attempts_for_tenant(tenant)
    finally:
        attempts.close()
    assert len(delivered) == 1
    assert saved[0].outcome.value == "delivered"
    assert saved[0].notification_id == saved[1].notification_id


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

    monkeypatch.setattr("verdict.service._open_webhook", delivery)
    notification = {
        "kind": "finding",
        "code": "tool_error",
        "severity": "error",
        "runs": 2,
    }

    now = datetime.now(timezone.utc)
    intent = _queue_test_analysis(
        storage_url, "a" * 64, notification,
        webhook_url="https://127.0.0.1.invalid/verdict", when=now,
    )
    failed = poll_cycle(storage_url, now=now + timedelta(seconds=1))["notifications"]
    delivered = poll_cycle(storage_url, now=now + timedelta(seconds=32))["notifications"]
    duplicate = poll_cycle(storage_url, now=now + timedelta(seconds=33))["notifications"]

    assert failed[0]["status"] == "failed"
    assert delivered[0]["status"] == "delivered"
    assert duplicate == []
    assert failed[0]["notificationId"] == delivered[0]["notificationId"]
    assert len(requests) == 2
    request, timeout = requests[1]
    assert timeout == 10
    assert request.get_header("Idempotency-key") == intent.notification_id
    assert json.loads(request.data) == {
        "source": "verdict",
        "notification": notification,
    }
    storage = SQLiteStorage(str(tmp_path / "verdict.db"))
    try:
        attempts = storage.list_notification_delivery_attempts(
            intent.notification_id, intent.destination_fingerprint
        )
    finally:
        storage.close()
    assert [attempt.outcome.value for attempt in attempts] == ["delivered", "failed"]
    assert attempts[0].http_status == 202
    assert attempts[1].error_code == "transport_error"


def test_worker_replays_committed_intent_through_real_loopback_https(
    tmp_path, monkeypatch,
) -> None:
    import hashlib
    import shutil
    import ssl
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL executable unavailable for disposable loopback TLS test")
    cert, key = tmp_path / "loopback.crt", tmp_path / "loopback.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True, text=True,
    )
    received = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.headers["Idempotency-Key"], self.rfile.read(
                int(self.headers["Content-Length"]),
            )))
            self.send_response(503 if len(received) == 1 else 200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Sink)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"sqlite:///{tmp_path / 'replay.db'}"
        endpoint = f"https://127.0.0.1:{server.server_port}/verdict"
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        monkeypatch.setenv("VERDICT_REPLAY_ENDPOINT", endpoint)
        alert = ControlStore(url).append(
            TENANT, kind="alert", document_id="default", state="active",
            payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_REPLAY_ENDPOINT",
                     "findings": True, "drift": False}, expected_revision=None,
        )
        when = datetime.now(timezone.utc)
        notification = {"kind": "finding", "code": "synthetic_failure", "severity": "error"}
        intent = create_intent(
            alert, tenant_id=TENANT, source_kind="analysis", source_id="synthetic",
            notification=notification, webhook_url=endpoint, created_at=when,
        )
        assert intent is not None
        run = DeterministicAnalysisRun(
            analysis_id=hashlib.sha256(b"replay-analysis").hexdigest(),
            tenant_id=TENANT, scope_key="agent-and-trace", cutoff=when,
            completed_at=when, status=AnalysisRunStatus.COMPLETED,
            analyzer_version="test-v1", input_fingerprint=hashlib.sha256(b"replay").hexdigest(),
            result={"findings": [notification]},
        )
        storage = SQLiteStorage(str(tmp_path / "replay.db"))
        try:
            storage.save_worker_analysis_run(run, [intent])
        finally:
            storage.close()
        # Simulate a process exiting after source commit and before its first send.
        first = poll_cycle(url, now=when + timedelta(seconds=1))
        second = poll_cycle(url, now=when + timedelta(seconds=32))
        third = poll_cycle(url, now=when + timedelta(seconds=63))
        assert first["notifications"][0]["status"] == "failed"
        assert second["notifications"][0]["status"] == "delivered"
        assert third["notifications"] == []
        assert len(received) == 2
        assert [key for key, _ in received] == [intent.notification_id] * 2
        assert [json.loads(body) for _, body in received] == [
            {"source": "verdict", "notification": notification}
        ] * 2
        storage = SQLiteStorage(str(tmp_path / "replay.db"))
        try:
            item = storage.get_delivery_intent(TENANT, intent.intent_id)
            attempts = storage.list_notification_delivery_attempts_for_tenant(TENANT)
        finally:
            storage.close()
        assert item.status == "delivered" and item.attempt_count == 2
        assert {attempt.outcome.value for attempt in attempts} == {"failed", "delivered"}
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_worker_blocks_old_webhook_after_environment_destination_changes(
    tmp_path, monkeypatch,
) -> None:
    import hashlib

    url = f"sqlite:///{tmp_path / 'changed-destination.db'}"
    first_url = "https://example.test/first"
    second_url = "https://example.test/second"
    monkeypatch.setenv("VERDICT_CHANGED_ENDPOINT", first_url)
    alert = ControlStore(url).append(
        TENANT, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_CHANGED_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    when = datetime.now(timezone.utc)
    notification = {"kind": "finding", "code": "synthetic_failure", "severity": "error"}
    intent = create_intent(
        alert, tenant_id=TENANT, source_kind="analysis", source_id="synthetic",
        notification=notification, webhook_url=first_url, created_at=when,
    )
    run = DeterministicAnalysisRun(
        analysis_id=hashlib.sha256(b"changed-destination").hexdigest(),
        tenant_id=TENANT, scope_key="agent-and-trace", cutoff=when,
        completed_at=when, status=AnalysisRunStatus.COMPLETED,
        analyzer_version="test-v1", input_fingerprint=hashlib.sha256(b"input").hexdigest(),
        result={"findings": [notification]},
    )
    storage = SQLiteStorage(str(tmp_path / "changed-destination.db"))
    try:
        storage.save_worker_analysis_run(run, [intent])
    finally:
        storage.close()
    monkeypatch.setenv("VERDICT_CHANGED_ENDPOINT", second_url)
    sent = []
    monkeypatch.setattr("verdict.service._open_webhook", lambda request, **_kwargs: sent.append(request))
    response = poll_cycle(url, now=when + timedelta(seconds=1))
    assert response["notifications"] == [{"status": "blocked",
                                           "notificationId": intent.notification_id}]
    assert sent == []
    storage = SQLiteStorage(str(tmp_path / "changed-destination.db"))
    try:
        assert storage.get_delivery_intent(TENANT, intent.intent_id).status == "blocked"
        assert storage.list_notification_delivery_attempts_for_tenant(TENANT) == []
    finally:
        storage.close()


def test_worker_does_not_resend_published_beta_delivery(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'beta-delivered.db'}"
    endpoint = "https://example.test/receiver"
    monkeypatch.setenv("VERDICT_BETA_ENDPOINT", endpoint)
    alert = ControlStore(url).append(
        TENANT, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_BETA_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    now = datetime.now(timezone.utc)
    notification = {"kind": "finding", "code": "synthetic"}
    intent = create_intent(
        alert, tenant_id=TENANT, source_kind="analysis", source_id="synthetic",
        notification=notification, webhook_url=endpoint, created_at=now,
    )
    run = DeterministicAnalysisRun(
        analysis_id=hashlib.sha256(b"beta-delivered").hexdigest(),
        tenant_id=TENANT, scope_key="agent-and-trace", cutoff=now, completed_at=now,
        status=AnalysisRunStatus.COMPLETED, analyzer_version="test-v1",
        input_fingerprint=hashlib.sha256(b"input").hexdigest(),
        result={"findings": [notification]},
    )
    storage = SQLiteStorage(str(tmp_path / "beta-delivered.db"))
    try:
        storage.save_notification_delivery_attempt(NotificationDeliveryAttempt(
            attempt_id=hashlib.sha256(b"beta-attempt").hexdigest(),
            notification_id=intent.notification_id, tenant_id=TENANT,
            source_kind="analysis", source_id="synthetic",
            destination_fingerprint=intent.destination_fingerprint,
            attempted_at=now, outcome=DeliveryOutcome.DELIVERED,
            payload=notification, http_status=200,
        ))
        storage.save_worker_analysis_run(run, [intent])
    finally:
        storage.close()
    monkeypatch.setattr("verdict.service._open_webhook", lambda *_args, **_kwargs: (
        pytest.fail("a published delivery must not be sent again")
    ))
    result = poll_cycle(url, now=now + timedelta(seconds=1))
    assert result["notifications"] == [{"status": "already_delivered",
                                         "notificationId": intent.notification_id}]
    storage = SQLiteStorage(str(tmp_path / "beta-delivered.db"))
    try:
        assert storage.get_delivery_intent(TENANT, intent.intent_id).status == "delivered"
        assert len(storage.list_notification_delivery_attempts_for_tenant(TENANT)) == 1
    finally:
        storage.close()


def test_one_worker_poll_has_bounded_webhook_send_work(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'bounded-delivery.db'}"
    endpoint = "https://example.test/receiver"
    monkeypatch.setenv("VERDICT_BOUNDED_ENDPOINT", endpoint)
    alert = ControlStore(url).append(
        TENANT, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_BOUNDED_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    now = datetime.now(timezone.utc)
    intents = [create_intent(
        alert, tenant_id=TENANT, source_kind="analysis", source_id=f"synthetic-{index}",
        notification={"kind": "finding", "code": f"synthetic-{index}"},
        webhook_url=endpoint, created_at=now,
    ) for index in range(5)]
    run = DeterministicAnalysisRun(
        analysis_id=hashlib.sha256(b"bounded-delivery").hexdigest(),
        tenant_id=TENANT, scope_key="agent-and-trace", cutoff=now, completed_at=now,
        status=AnalysisRunStatus.COMPLETED, analyzer_version="test-v1",
        input_fingerprint=hashlib.sha256(b"input").hexdigest(), result={},
    )
    storage = SQLiteStorage(str(tmp_path / "bounded-delivery.db"))
    try:
        storage.save_worker_analysis_run(run, intents)
    finally:
        storage.close()

    class Accepted:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    sent = []
    monkeypatch.setattr("verdict.service._open_webhook", lambda request, **_kw: (
        sent.append(request), Accepted(),
    )[1])
    assert len(poll_cycle(url, now=now + timedelta(seconds=1))["notifications"]) == 2
    assert len(sent) == 2
    assert len(poll_cycle(url, now=now + timedelta(seconds=2))["notifications"]) == 2
    assert len(poll_cycle(url, now=now + timedelta(seconds=3))["notifications"]) == 1
    assert len(sent) == 5


def test_worker_reaches_unbound_alerts_beyond_first_probe_page(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'unbound-backlog.db'}"
    endpoint = "https://example.test/receiver"
    alert = ControlStore(url).append(
        TENANT, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_BACKLOG_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    now = datetime.now(timezone.utc)
    intents = [create_intent(
        alert, tenant_id=TENANT, source_kind="analysis", source_id=f"item-{index}",
        notification={"kind": "finding", "code": f"synthetic-{index}"},
        webhook_url=None, created_at=now,
    ) for index in range(105)]
    storage = SQLiteStorage(str(tmp_path / "unbound-backlog.db"))
    try:
        storage.save_worker_analysis_run(DeterministicAnalysisRun(
            analysis_id=hashlib.sha256(b"unbound-backlog").hexdigest(),
            tenant_id=TENANT, scope_key="agent-and-trace", cutoff=now, completed_at=now,
            status=AnalysisRunStatus.COMPLETED, analyzer_version="test-v1",
            input_fingerprint=hashlib.sha256(b"input").hexdigest(), result={},
        ), intents)
    finally:
        storage.close()
    monkeypatch.setenv("VERDICT_BACKLOG_ENDPOINT", endpoint)

    class Accepted:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("verdict.service._open_webhook", lambda *_args, **_kwargs: Accepted())
    poll_cycle(url, now=now + timedelta(seconds=1))
    storage = SQLiteStorage(str(tmp_path / "unbound-backlog.db"))
    try:
        assert storage.delivery_queue_counts(TENANT) == {
            "unbound": 5, "pending": 98, "delivered": 2,
        }
    finally:
        storage.close()
    poll_cycle(url, now=now + timedelta(seconds=2))
    storage = SQLiteStorage(str(tmp_path / "unbound-backlog.db"))
    try:
        assert storage.delivery_queue_counts(TENANT) == {
            "pending": 101, "delivered": 4,
        }
    finally:
        storage.close()


def test_malformed_webhook_url_cannot_abort_worker_poll(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'malformed-webhook.db'}"
    invalid = "https://exa mple.com/path"
    monkeypatch.setenv("VERDICT_MALFORMED_ENDPOINT", invalid)
    ControlStore(url).append(
        TENANT, kind="alert", document_id="default", state="active",
        payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_MALFORMED_ENDPOINT",
                 "findings": True, "drift": False}, expected_revision=None,
    )
    now = datetime.now(timezone.utc)
    intent = _queue_test_analysis(
        url, "malformed", {"kind": "finding", "code": "synthetic"},
        webhook_url=invalid, when=now,
    )
    assert intent.destination_fingerprint is None
    assert poll_cycle(url, now=now + timedelta(seconds=1))["notifications"] == []
    storage = SQLiteStorage(str(tmp_path / "malformed-webhook.db"))
    try:
        assert storage.get_delivery_intent(TENANT, intent.intent_id).status == "unbound"
        assert storage.list_notification_delivery_attempts_for_tenant(TENANT) == []
    finally:
        storage.close()


@pytest.mark.parametrize("redirect_code", [301, 302, 303, 307, 308])
def test_real_loopback_https_webhook_retry_and_idempotency(
    tmp_path, monkeypatch, redirect_code: int
) -> None:
    import shutil
    import ssl
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL executable unavailable for disposable loopback TLS test")
    cert = tmp_path / "loopback.crt"
    key = tmp_path / "loopback.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True, text=True,
    )
    received = []
    redirected = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append((self.headers["Idempotency-Key"], self.rfile.read(length)))
            if self.path == "/redirect":
                self.send_response(redirect_code)
                self.send_header("Location", "/target")
            else:
                self.send_response(503 if len(received) == 1 else 200)
            self.end_headers()

        def do_GET(self):
            redirected.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Sink)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"sqlite:///{tmp_path / 'real-webhook.db'}"
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        monkeypatch.setenv("VERDICT_TEST_REAL_WEBHOOK", f"https://127.0.0.1:{server.server_port}/verdict")
        ControlStore(url).append(
            TENANT, kind="alert", document_id="default", state="active",
            payload={"destination": "webhook", "webhookUrlEnvVar": "VERDICT_TEST_REAL_WEBHOOK",
                     "findings": True, "drift": False}, expected_revision=None,
        )
        notification = {"kind": "finding", "code": "synthetic_tool_error", "severity": "error"}
        now = datetime.now(timezone.utc)
        intent = _queue_test_analysis(
            url, "synthetic", notification,
            webhook_url=f"https://127.0.0.1:{server.server_port}/verdict", when=now,
        )
        failed = poll_cycle(url, now=now + timedelta(seconds=1))["notifications"]
        delivered = poll_cycle(url, now=now + timedelta(seconds=32))["notifications"]
        duplicate = poll_cycle(url, now=now + timedelta(seconds=33))["notifications"]
        assert failed[0]["status"] == "failed"
        assert delivered[0]["status"] == "delivered"
        assert duplicate == []
        assert len(received) == 2
        assert [item[0] for item in received] == [intent.notification_id] * 2
        assert [json.loads(item[1]) for item in received] == [
            {"source": "verdict", "notification": notification}
        ] * 2
        storage = SQLiteStorage(str(tmp_path / "real-webhook.db"))
        try:
            attempts = storage.list_notification_delivery_attempts_for_tenant(TENANT)
        finally:
            storage.close()
        assert len(attempts) == 2
        assert {attempt.http_status for attempt in attempts} == {503, 200}
        assert {attempt.outcome.value for attempt in attempts} == {"failed", "delivered"}

        monkeypatch.setenv("VERDICT_TEST_REAL_WEBHOOK", f"https://127.0.0.1:{server.server_port}/redirect")
        redirected_intent = _queue_test_analysis(
            url, "redirect", notification,
            webhook_url=f"https://127.0.0.1:{server.server_port}/redirect", when=now,
        )
        redirected_result = poll_cycle(url, now=now + timedelta(seconds=34))["notifications"]
        assert redirected_result[0]["status"] == "failed"
        assert redirected == []
        assert len(received) == 3
        storage = SQLiteStorage(str(tmp_path / "real-webhook.db"))
        try:
            redirect_attempts = storage.list_notification_delivery_attempts(
                redirected_intent.notification_id, redirected_intent.destination_fingerprint
            )
        finally:
            storage.close()
        assert len(redirect_attempts) == 1
        assert redirect_attempts[0].http_status == redirect_code
        assert redirect_attempts[0].error_code == "http_rejected"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
