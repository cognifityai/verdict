"""Saved-case and fixed-criteria comparison through the local dashboard API."""

import asyncio
import json
import time

import httpx
from verdict.dashboard.app import create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.experiments import _comparable_metrics, _trial
from verdict.storage import SQLiteStorage


def _case_set():
    return {
        "title": "Synthetic incident-routing tasks",
        "reviewed": True,
        "cases": [
            {"id": "critical_outage",
             "input": "Synthetic incident: checkout unavailable for all users; severity P1",
             "expectedBehavior": "Page on-call",
             "checks": [{"kind": "exact", "value": "page_on_call"}]},
            {"id": "routine_edit",
             "input": "Synthetic request: correct a dashboard label; severity P3",
             "expectedBehavior": "Backlog without paging",
             "checks": [{"kind": "exact", "value": "create_backlog_ticket"}]},
        ],
    }


def test_timed_out_trial_stops_ordinary_descendant_side_effect(tmp_path, monkeypatch):
    marker = tmp_path / "late-marker"
    (tmp_path / "trusted_trial.py").write_text(
        "import subprocess, sys, time\n"
        "def run(task_input, context, configuration):\n"
        "    subprocess.Popen([sys.executable, '-c', "
        "'import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text(\"late\")', "
        "configuration['marker']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    time.sleep(60)\n"
        "    return 'unreachable'\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "trusted_trial:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "1")
    case = {"input": "synthetic", "checks": [{"kind": "exact", "value": "ok"}]}
    result = _trial(case, {"version": "test", "marker": str(marker)})
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)
    assert result == {"status": "error", "error": "timeout"}
    assert not marker.exists()


def test_inherited_worker_output_is_bounded_and_next_trial_runs(tmp_path, monkeypatch):
    marker = tmp_path / "inherited-marker"
    (tmp_path / "trusted_trial.py").write_text(
        "import os, subprocess, sys, time\n"
        "def run(task_input, context, configuration):\n"
        "    if task_input == 'held_pipe':\n"
        "        subprocess.Popen([sys.executable, '-c', "
        "'import pathlib,sys,time; time.sleep(2); pathlib.Path(sys.argv[1]).write_text(\"late\")', "
        "configuration['marker']])\n"
        "        return 'ok'\n"
        "    if task_input == 'large_output':\n"
        "        os.write(1, b'x' * 20000)\n"
        "        return 'ok'\n"
        "    if task_input == 'failure':\n"
        "        raise ValueError('synthetic runner failure')\n"
        "    return 'ok'\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "trusted_trial:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "1")
    base = {"checks": [{"kind": "exact", "value": "ok"}]}
    assert _trial({**base, "input": "held_pipe"}, {"marker": str(marker)}) == {
        "status": "error", "error": "timeout",
    }
    assert _trial({**base, "input": "large_output"}, {}) == {
        "status": "error", "error": "runner_failed",
    }
    assert _trial({**base, "input": "failure"}, {}) == {
        "status": "error", "error": "runner_failed",
    }
    assert _trial({**base, "input": "normal"}, {})["status"] == "pass"
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)
    assert not marker.exists()


def test_experiment_trial_timeout_is_host_bounded_and_reported(tmp_path, monkeypatch):
    from verdict.dashboard.experiments import _trial_timeout_seconds

    for raw in ("0", "301", "3601", "-1", "1.5", "false", "999999"):
        monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", raw)
        try:
            _trial_timeout_seconds()
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid timeout accepted: {raw}")
    monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "300")
    assert _trial_timeout_seconds() == 300
    monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "verdict.dashboard.demo_task:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
        "baseline": {"version": "baseline-v1"}, "candidate": {"version": "candidate-v1"},
    }))
    path = tmp_path / "timeout-config.db"
    SQLiteStorage(str(path)).close()

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=f"sqlite:///{path}", tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/experiments")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["trialTimeoutSeconds"] == 45


def test_experiment_rejects_worst_case_duration_before_runner_call(tmp_path, monkeypatch):
    import verdict.dashboard.experiments as experiments

    path = tmp_path / "duration.db"
    url = f"sqlite:///{path}"
    SQLiteStorage(str(path)).close()
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "verdict.dashboard.demo_task:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
        "baseline": {"version": "baseline-v1"}, "candidate": {"version": "candidate-v1"},
    }))
    monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "120")
    def forbidden_runner(*_args, **_kwargs):
        raise AssertionError("a rejected experiment started a runner")
    monkeypatch.setattr(experiments, "_trial", forbidden_runner)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url, tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            created = await client.post("/api/experiments/case-sets/synthetic",
                                        json=_case_set(), headers=headers)
            configuration = await client.get("/api/experiments")
            rejected = await client.post("/api/experiments/run", json={
                "caseSetId": "synthetic", "baseline": "baseline", "candidate": "candidate",
                "trials": 3,
            }, headers=headers)
            return created, configuration, rejected

    created, configuration, rejected = asyncio.run(request())
    assert created.status_code == 200
    assert configuration.json()["maxRunSeconds"] == 600
    assert rejected.status_code == 400
    assert "duration" in rejected.json()["error"]
    assert not [row for row in ControlStore(url).list_current("local")
                if row["kind"] == "experiment"]


def test_experiment_accepts_exact_600_second_configured_runner_bound(tmp_path, monkeypatch):
    path = tmp_path / "duration-boundary.db"
    url = f"sqlite:///{path}"
    SQLiteStorage(str(path)).close()
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "verdict.dashboard.demo_task:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
        "baseline": {"version": "baseline-v1"}, "candidate": {"version": "candidate-v1"},
    }))
    monkeypatch.setenv("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "300")
    one_case = {**_case_set(), "cases": _case_set()["cases"][:1]}

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url, tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            saved = await client.post("/api/experiments/case-sets/one",
                                      json=one_case, headers=headers)
            ran = await client.post("/api/experiments/run", json={
                "caseSetId": "one", "baseline": "baseline", "candidate": "candidate",
                "trials": 1,
            }, headers=headers)
            return saved, ran

    saved, ran = asyncio.run(request())
    assert saved.status_code == 200
    assert ran.status_code == 200
    assert len(ran.json()["cases"]) == 1


def test_reviewed_case_fields_round_trip_and_runner_receives_saved_input(tmp_path, monkeypatch):
    (tmp_path / "trusted_echo.py").write_text(
        "def run(task_input, context, configuration):\n"
        "    expected = 'Fetch https://example.com/docs/page and email support@acme.example a summary'\n"
        "    return 'matched https://example.com/docs/page' if task_input == expected and context == 'contact support@acme.example' else 'mismatch'\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "trusted_echo:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
        "baseline": {"version": "baseline-v1"}, "candidate": {"version": "candidate-v1"},
    }))
    path = tmp_path / "reviewed-case.db"
    url = f"sqlite:///{path}"
    SQLiteStorage(str(path)).close()
    submitted_input = "Fetch https://example.com/docs/page and email support@acme.example a summary"
    payload = {"title": "Reviewed input", "reviewed": True, "cases": [{
        "id": "contact", "input": submitted_input,
        "context": "contact support@acme.example",
        "expectedBehavior": "Send to https://example.com/docs/page",
        "checks": [{"kind": "contains", "value": "https://example.com/docs/page"}],
    }]}

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url, tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            saved = await client.post("/api/experiments/case-sets/reviewed",
                                      json=payload, headers=headers)
            rejected_secret_key = await client.post(
                "/api/experiments/case-sets/rejected",
                json={**payload, "api_key": "synthetic"},
                headers=headers,
            )
            listed = await client.get("/api/experiments")
            ran = await client.post("/api/experiments/run", json={
                "caseSetId": "reviewed", "baseline": "baseline", "candidate": "candidate",
                "trials": 1,
            }, headers=headers)
            return saved, rejected_secret_key, listed, ran

    saved, rejected_secret_key, listed, ran = asyncio.run(request())
    assert saved.status_code == 200
    assert rejected_secret_key.status_code == 400
    assert listed.status_code == 200
    assert ran.status_code == 200
    stored = ControlStore(url).history("local", "case_set", "reviewed")[0]["payload"]
    assert stored == payload
    assert listed.json()["caseSets"][0]["payload"] == payload
    assert ran.json()["cases"][0]["baseline"]["status"] == "pass"
    assert ran.json()["cases"][0]["candidate"]["status"] == "pass"


def test_experiment_runs_fixed_cases_and_reports_collateral_regression(tmp_path, monkeypatch):
    path = tmp_path / "experiments.db"
    SQLiteStorage(str(path)).close()
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "verdict.dashboard.demo_task:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
        "baseline": {"version": "baseline-v1"},
        "candidate": {"version": "candidate-v1"},
    }))

    async def request():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            created = await client.post("/api/experiments/case-sets/synthetic", json=_case_set(),
                                        headers=headers)
            ran = await client.post("/api/experiments/run", json={
                "caseSetId": "synthetic", "baseline": "baseline", "candidate": "candidate",
                "trials": 1,
            }, headers=headers)
            return created, ran

    created, ran = asyncio.run(request())
    assert created.status_code == 200
    assert ran.status_code == 200
    result = ran.json()
    assert result["caseSetRevision"] == 1
    assert result["evaluatorIdentity"] == "deterministic-output-checks-v1"
    assert result["comparison"]["fixed"] == ["critical_outage"]
    assert result["comparison"]["newRegressions"] == ["routine_edit"]
    assert len(result["cases"]) == 2
    assert result["cases"][0]["comparableMetrics"]["taskElapsedMs"] is not None
    assert result["cases"][0]["comparableMetrics"]["reportedCostUsd"] is None


def test_cost_comparison_requires_same_reported_basis_and_verified_trials():
    left = {"status": "pass", "trials": [{"status": "pass", "taskElapsedMs": 12,
            "costUsd": 0.1, "costBasis": "provider_bill"}]}
    right = {"status": "pass", "trials": [{"status": "pass", "taskElapsedMs": 10,
             "costUsd": 0.08, "costBasis": "provider_bill"}]}
    assert _comparable_metrics(left, right)["reportedCostUsd"] == [0.1, 0.08]
    right["trials"][0]["costBasis"] = "static_list_price"
    assert _comparable_metrics(left, right)["reportedCostUsd"] is None
    right["status"] = "unverified"
    assert _comparable_metrics(left, right)["taskElapsedMs"] is None


def test_experiment_rejects_unreviewed_cases_and_unconfigured_runner(tmp_path):
    path = tmp_path / "experiment-deny.db"
    SQLiteStorage(str(path)).close()

    async def request():
        transport = httpx.ASGITransport(app=create_app(
            storage=f"sqlite:///{path}", tenant_id="local",
        ))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            denied = await client.post("/api/experiments/case-sets/synthetic",
                                       json={**_case_set(), "reviewed": False}, headers=headers)
            unavailable = await client.post("/api/experiments/run", json={
                "caseSetId": "synthetic", "baseline": "baseline", "candidate": "candidate",
                "trials": 1,
            }, headers=headers)
            return denied, unavailable

    denied, unavailable = asyncio.run(request())
    assert denied.status_code == 400
    assert unavailable.status_code in {400, 404}


def test_experiment_case_sets_are_tenant_scoped(tmp_path, monkeypatch):
    path = tmp_path / "tenant.db"
    url = f"sqlite:///{path}"
    SQLiteStorage(str(path)).close()
    ControlStore(url).append("other", kind="case_set", document_id="other_cases",
                             state="active", payload=_case_set(), expected_revision=None)
    monkeypatch.setenv("VERDICT_EXPERIMENT_RUNNER", "verdict.dashboard.demo_task:run")
    monkeypatch.setenv("VERDICT_EXPERIMENT_CONFIGS", json.dumps({
        "baseline": {"version": "baseline-v1"}, "candidate": {"version": "candidate-v1"},
    }))

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url, tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            listed = await client.get("/api/experiments")
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            ran = await client.post("/api/experiments/run", json={
                "caseSetId": "other_cases", "baseline": "baseline", "candidate": "candidate",
            }, headers={"X-Verdict-Setup": token})
            return listed, ran

    listed, ran = asyncio.run(request())
    assert listed.status_code == 200
    assert listed.json()["caseSets"] == []
    assert ran.status_code == 404


def test_saved_experiment_history_is_recent_first_before_limit(tmp_path):
    path = tmp_path / "history-order.db"
    url = f"sqlite:///{path}"
    store = ControlStore(url)
    for identifier in ("z-old", "a-middle", "m-new"):
        store.append("local", kind="experiment", document_id=identifier,
                     state="completed", payload={"caseSetId": identifier,
                     "caseSetRevision": 1, "cases": []}, expected_revision=None)

    async def request():
        transport = httpx.ASGITransport(app=create_app(storage=url, tenant_id="local"))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/experiments")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert [row["documentId"] for row in response.json()["experiments"]] == [
        "m-new", "a-middle", "z-old",
    ]
