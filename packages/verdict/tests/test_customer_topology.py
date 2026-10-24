"""Synthetic local acceptance of separate customer-managed Verdict processes."""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
import verdict
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn

pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict
from verdict.agent_transport import FileCaptureSink
from verdict.schema import SpanRecord, Trace
from verdict.shipper import HttpCollectorClient, SegmentShipper

DSN, POSTGRES_SKIP_REASON = validate_test_dsn(
    os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
    allow_any_database=os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1",
)


def _free_ports() -> tuple[int, int]:
    with socket.socket() as dashboard, socket.socket() as collector:
        dashboard.bind(("127.0.0.1", 0))
        collector.bind(("127.0.0.1", 0))
        return (int(dashboard.getsockname()[1]),
                int(collector.getsockname()[1]))


def _wait_for(url: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"local service exited during startup: {process.returncode}")
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.15)
    pytest.fail(f"local service did not become ready at {url}")


@pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON)
def test_separate_sdk_collector_dashboard_and_worker_complete_a_saved_plan(tmp_path: Path) -> None:
    """Exercise CLI processes and HTTP/PG boundaries, with no provider egress."""
    assert DSN is not None
    if not DSN.startswith(("postgresql://", "postgres://")):
        pytest.skip("public collector and dashboard CLIs require a PostgreSQL URL")
    tenant = "synthetic-topology"
    password = secrets.token_urlsafe(24)
    collector_key = secrets.token_urlsafe(32)
    auth = httpx.BasicAuth("synthetic-reviewer", password)
    dashboard_port, collector_port = _free_ports()
    dashboard = f"http://127.0.0.1:{dashboard_port}"
    collector = f"http://127.0.0.1:{collector_port}"
    bin_dir = Path(sys.executable).parent
    children: list[subprocess.Popen] = []
    verdict.shutdown()

    with isolated_test_dsn(DSN) as isolated, ExitStack() as resources:
        # The test helper returns libpq keyword syntax. The three public CLIs
        # require a PostgreSQL URL, so retain its isolated search_path as a
        # URI query argument rather than weakening the real entry-point check.
        parsed = urlsplit(DSN)
        query = dict(parse_qsl(parsed.query))
        query["options"] = conninfo_to_dict(isolated)["options"]
        storage = urlunsplit(parsed._replace(query=urlencode(query)))
        environment = os.environ.copy()
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
                     "VERDICT_CLAUDE_ROOT", "VERDICT_CODEX_ROOT"):
            environment.pop(name, None)
        environment.update(
            VERDICT_STORAGE=storage,
            VERDICT_TENANT_ID=tenant,
            VERDICT_COLLECTOR_API_KEY=collector_key,
            VERDICT_USER="synthetic-reviewer",
            VERDICT_PASS=password,
            VERDICT_ALLOWED_HOSTS="127.0.0.1,localhost",
        )

        def start(name: str, *args: str) -> subprocess.Popen:
            if "--storage" in args or storage in args:
                pytest.fail("storage URL must stay out of process argv")
            executable = bin_dir / name
            assert executable.is_file(), f"missing installed CLI: {name}"
            log = resources.enter_context((tmp_path / f"{name}.log").open("w"))
            process = subprocess.Popen(
                [str(executable), *args], env=environment,
                stdout=log, stderr=subprocess.STDOUT,
            )
            children.append(process)
            return process

        try:
            dashboard_process = start(
                "verdict-dashboard", "--tenant-id", tenant,
                "--host", "127.0.0.1", "--port", str(dashboard_port),
            )
            collector_process = start(
                "verdict-collector", "--tenant-id", tenant,
                "--host", "127.0.0.1", "--port", str(collector_port),
            )
            _wait_for(f"{dashboard}/api/health", dashboard_process)
            _wait_for(f"{collector}/healthz", collector_process)
            assert httpx.get(f"{dashboard}/api/readiness").status_code == 401
            assert httpx.post(
                f"{collector}/v1/ingest", content=b"{}\n",
                headers={"Authorization": "Bearer wrong"},
            ).status_code == 401

            with httpx.Client(base_url=dashboard, auth=auth, timeout=20) as client:
                token = client.get("/api/setup/token").json()["setupToken"]
                headers = {"X-Verdict-Setup": token}
                first = client.post(
                    "/api/control/actions/run-schedule",
                    json={"analyzeOnly": True, "includeApprovedEvaluations": False},
                    headers=headers,
                )
                assert first.status_code == 200, first.text
                assert first.json()["analysis"]["status"] == "completed"
                saved = client.post(
                    "/api/control/schedule/daily",
                    json={"state": "active", "payload": {
                        "intervalHours": 24, "analyzeOnly": True,
                        "includeApprovedEvaluations": False,
                    }, "expectedRevision": None},
                    headers=headers,
                )
                assert saved.status_code == 200, saved.text
                assert saved.json()["revision"] == 1
                assert client.get("/api/readiness").json()["worker"]["status"] == "unverified"

            spool = tmp_path / "spool"
            verdict.init(
                transport="file", spool_directory=spool, tenant_id=tenant,
                service_name="synthetic-agent", instrumentors=[],
            )
            with pytest.raises(RuntimeError, match="synthetic execution failure"):
                with verdict.agent_run(
                    name="synthetic-support-agent", external_id="synthetic-failed-run",
                ) as run:
                    run_id = run.run_id
                    with run.turn(user_input="synthetic request"):
                        raise RuntimeError("synthetic execution failure")
            with verdict.agent_run(name="synthetic-support-agent",
                                   external_id="synthetic-check-failed") as failed_check_run:
                failed_check_run_id = failed_check_run.run_id
                with failed_check_run.turn(user_input="synthetic check") as turn:
                    turn.record_test(command="synthetic-check", exit_code=1, failed=1,
                                     check_id="stable-synthetic-check")
            with verdict.agent_run(name="synthetic-support-agent",
                                   external_id="synthetic-check-passed") as passed_check_run:
                passed_check_run_id = passed_check_run.run_id
                with passed_check_run.turn(user_input="synthetic recheck") as turn:
                    turn.record_test(command="synthetic-check", exit_code=0, failed=0,
                                     check_id="stable-synthetic-check")
            verdict.shutdown()
            standalone_trace_id = "synthetic-standalone-provider-call"
            standalone = FileCaptureSink(
                spool, redaction_mode="redact", redaction_secret=None,
            )
            standalone.capture_trace(Trace(
                trace_id=standalone_trace_id,
                tenant_id="untrusted-producer-tenant", provider="synthetic",
                request_model="synthetic-model", input_tokens=7, output_tokens=3,
                started_at=datetime.now(timezone.utc),
            ))
            standalone.capture_span(SpanRecord(
                span_id="synthetic-linked-span", name="synthetic work",
                trace_id=standalone_trace_id, tenant_id="untrusted-producer-tenant",
                started_at=datetime.now(timezone.utc),
            ))
            standalone.close()
            sent = SegmentShipper(
                spool,
                HttpCollectorClient(collector, collector_key, allow_insecure_http=True),
            ).ship_once()
            assert sent.accepted_records > 0 and sent.rejected_records == 0
            assert sent.deleted_segments > 0

            start("verdict-service", "--tenant-id", tenant)
            with httpx.Client(base_url=dashboard, auth=auth, timeout=10) as client:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    ready = client.get("/api/readiness").json()
                    if (ready["worker"]["status"] == "recent"
                            and ready["lastCycle"]["state"] == "completed"
                            and ready["lastCycle"]["trigger"] == "worker"
                            and ready["lastCycle"]["scheduleRevision"] == 1
                            and ready["openIssues"] >= 2):
                        break
                    time.sleep(0.25)
                else:
                    pytest.fail(f"worker did not complete the saved plan: {ready}")
                detail = client.get(f"/api/runs/{run_id}")
                assert detail.status_code == 200, detail.text
                assert detail.json()["agentName"] == "synthetic-support-agent"
                linked_spans = client.get(
                    f"/api/explain/spans/{standalone_trace_id}", headers=headers,
                )
                assert linked_spans.status_code == 200, linked_spans.text
                assert [row["spanId"] for row in linked_spans.json()["spans"]] == [
                    "synthetic-linked-span",
                ]
                issues = client.get("/api/control/issues").json()["issues"]
                assert any(
                    item["runId"] == run_id and item["checkCode"] == "run_failed"
                    and item["eventId"] is None for item in issues
                )
                test_notice = next(item for item in issues
                                   if item["runId"] == failed_check_run_id
                                   and item["checkCode"] == "test_failed")
                passed_detail = client.get(f"/api/runs/{passed_check_run_id}")
                assert passed_detail.status_code == 200, passed_detail.text
                [passed_event] = [event for event in passed_detail.json()["events"]
                                  if event["type"] == "test_result"]
                assert "check_id_hash" not in passed_event["attributes"]
                attached = client.post(
                    f"/api/control/issues/{test_notice['noticeId']}/rechecks",
                    json={"runId": passed_check_run_id,
                          "eventId": passed_event["eventId"]},
                    headers=headers,
                )
                assert attached.status_code == 200, attached.text
                assert attached.json()["recheck"]["sameCheckId"] is True
                refreshed = client.get("/api/control/issues").json()["issues"]
                assert next(item for item in refreshed
                            if item["noticeId"] == test_notice["noticeId"])["rechecks"][0][
                                "sameCheckId"] is True
                assert client.get(
                    "/api/control/issues", params={"state": "reviewed"},
                ).json()["issues"] == []
        finally:
            verdict.shutdown()
            for process in reversed(children):
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
            for process in reversed(children):
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
