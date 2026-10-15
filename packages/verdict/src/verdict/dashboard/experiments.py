"""Small local saved-case experiment workflow; execution comes only from host configuration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.setup_routes import SetupRoutes
from verdict.redaction import redact_structure

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RUNNER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*\Z")
_RUN_LOCK = threading.Lock()
_CHECK_IDENTITY = "deterministic-output-checks-v1"
_MAX_WORKER_OUTPUT = 8_192
_MAX_WORKER_INPUT = 16_384  # Match the worker's bounded stdin read.
_MAX_RUN_SECONDS = 600


def _trial_timeout_seconds() -> int:
    raw = os.environ.get("VERDICT_EXPERIMENT_TRIAL_TIMEOUT_SECONDS", "120")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", raw) or not 1 <= int(raw) <= 300:
        raise ValueError("experiment trial timeout must be 1-300 seconds")
    return int(raw)


def _stop_worker_group(worker: subprocess.Popen) -> None:
    """Quiesce the ordinary POSIX process group before another trial starts."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(worker.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            if sig == signal.SIGTERM:
                raise
            # macOS can report EPERM for a group containing only dead zombies.
        if sig == signal.SIGTERM:
            try:
                worker.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
    try:
        worker.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    for stream in (worker.stdin, worker.stdout):
        if stream is not None:
            stream.close()


def _run_worker(request: bytes, timeout: int) -> tuple[int, bytes]:
    if os.name != "posix":
        raise OSError("experiment process-group cleanup requires POSIX")
    worker = subprocess.Popen(
        [sys.executable, "-m", "verdict.dashboard.experiment_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    output = bytearray()
    sent = 0
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        assert worker.stdin is not None and worker.stdout is not None
        os.set_blocking(worker.stdin.fileno(), False)
        os.set_blocking(worker.stdout.fileno(), False)
        selector.register(worker.stdin, selectors.EVENT_WRITE)
        selector.register(worker.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map() or worker.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(worker.args, timeout)
                for key, _ in selector.select(timeout=min(remaining, 0.1)):
                    if key.fileobj is worker.stdin:
                        try:
                            sent += os.write(worker.stdin.fileno(), request[sent:sent + 4_096])
                        except BrokenPipeError:
                            sent = len(request)
                        if sent == len(request):
                            selector.unregister(worker.stdin)
                            worker.stdin.close()
                    else:
                        chunk = os.read(worker.stdout.fileno(), 4_096)
                        if not chunk:
                            selector.unregister(worker.stdout)
                            worker.stdout.close()
                        else:
                            output.extend(chunk)
                            if len(output) > _MAX_WORKER_OUTPUT:
                                raise ValueError("runner output exceeded limit")
            return worker.returncode, bytes(output)
        except BaseException:
            _stop_worker_group(worker)
            raise
        finally:
            for stream in (worker.stdin, worker.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def _configuration() -> tuple[str, dict[str, dict[str, Any]]]:
    runner = os.environ.get("VERDICT_EXPERIMENT_RUNNER", "")
    if _RUNNER.fullmatch(runner) is None:
        raise ValueError("a trusted local runner must be configured")
    raw = os.environ.get("VERDICT_EXPERIMENT_CONFIGS", "")
    if len(raw) > 8_192:
        raise ValueError("experiment configurations are too large")
    configs = json.loads(raw)
    if not isinstance(configs, dict) or not 2 <= len(configs) <= 10:
        raise ValueError("at least two experiment configurations are required")
    for name, value in configs.items():
        if (_ID.fullmatch(name) is None or not isinstance(value, dict)
                or not isinstance(value.get("version"), str) or not value["version"]):
            raise ValueError("invalid experiment configuration")
    return runner, configs


def _check(output: str, checks: list[dict[str, str]]) -> bool:
    return all(
        (output == check["value"] if check["kind"] == "exact" else
         check["value"] in output if check["kind"] == "contains" else
         check["value"] not in output)
        for check in checks
    )


def _trial(case: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    request = {"input": case["input"], "context": case.get("context"), "configuration": config}
    encoded = json.dumps(request, ensure_ascii=False).encode()
    if len(encoded) > _MAX_WORKER_INPUT:
        return {"status": "error", "error": "input_too_large"}
    try:
        code, output = _run_worker(encoded, _trial_timeout_seconds())
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "timeout"}
    except (OSError, ValueError):
        return {"status": "error", "error": "runner_failed"}
    if code != 0:
        return {"status": "error", "error": "runner_failed"}
    try:
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get("output"), str):
            raise ValueError("invalid runner result")
    except (TypeError, ValueError):
        return {"status": "error", "error": "invalid_runner_result"}
    return {
        "status": "pass" if _check(result["output"], case["checks"]) else "fail",
        "output": result["output"], "taskElapsedMs": result.get("taskElapsedMs"),
        "usage": result.get("usage"), "costUsd": result.get("costUsd"),
        "costBasis": result.get("costBasis"),
    }


def _side(case: dict[str, Any], config: dict[str, Any], trials: int) -> dict[str, Any]:
    attempts = [_trial(case, config) for _ in range(trials)]
    states = {attempt["status"] for attempt in attempts}
    status = next(iter(states)) if len(states) == 1 else "unverified"
    if status == "error":
        status = "unverified"
    return {"status": status, "trials": attempts}


def _comparison(cases: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {
        "newRegressions": [], "fixed": [], "unchangedFailures": [],
        "unchangedPasses": [], "unverified": [],
    }
    for case in cases:
        before = case["baseline"]["status"]
        after = case["candidate"]["status"]
        if "unverified" in (before, after):
            group = "unverified"
        elif before == "pass" and after == "fail":
            group = "newRegressions"
        elif before == "fail" and after == "pass":
            group = "fixed"
        elif before == "fail":
            group = "unchangedFailures"
        else:
            group = "unchangedPasses"
        groups[group].append(case["id"])
    return groups


def _comparable_metrics(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    def mean(side: dict[str, Any], field: str) -> float | None:
        trials = side["trials"]
        values = [trial.get(field) for trial in trials]
        if side["status"] == "unverified" or not all(
            type(value) in (int, float) and math.isfinite(value) and value >= 0
            for value in values
        ):
            return None
        return round(sum(values) / len(values), 6)

    elapsed = [mean(side, "taskElapsedMs") for side in (baseline, candidate)]
    costs = [mean(side, "costUsd") for side in (baseline, candidate)]
    bases = {trial.get("costBasis") for side in (baseline, candidate) for trial in side["trials"]}
    return {
        "taskElapsedMs": elapsed if None not in elapsed else None,
        "reportedCostUsd": costs if None not in costs and len(bases) == 1 else None,
        "reportedCostBasis": next(iter(bases)) if None not in costs and len(bases) == 1 else None,
    }


def register_experiment_routes(app, setup: SetupRoutes) -> None:
    store = ControlStore(setup.storage_url)

    @app.get("/api/experiments")
    def list_experiments(request: Request):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error": "experiments unavailable"}, status_code=403)
        rows = store.list_current(setup.tenant_id)
        try:
            _, variants = _configuration()
            timeout_seconds = _trial_timeout_seconds()
        except (TypeError, ValueError, json.JSONDecodeError):
            variants, timeout_seconds = {}, None
        case_sets = [row for row in rows if row["kind"] == "case_set"]
        experiments = sorted(
            (row for row in rows if row["kind"] == "experiment"),
            key=lambda row: (str(row["createdAt"]), str(row["documentId"])), reverse=True,
        )
        return {
            "caseSets": case_sets[:20], "caseSetsTruncated": len(case_sets) > 20,
            "experiments": experiments[:20], "experimentsTruncated": len(experiments) > 20,
            "runnerConfigured": bool(variants),
            "configuredVariants": sorted(variants),
            "trialTimeoutSeconds": timeout_seconds,
            "maxRunSeconds": _MAX_RUN_SECONDS,
        }

    @app.post("/api/experiments/case-sets/{case_set_id}")
    def save_case_set(request: Request, case_set_id: str, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "case authorization required"}, status_code=403)
        if _ID.fullmatch(case_set_id) is None:
            return JSONResponse({"error": "invalid case set id"}, status_code=400)
        try:
            current = next((row for row in store.list_current(setup.tenant_id)
                            if row["kind"] == "case_set" and row["documentId"] == case_set_id), None)
            return store.append(
                setup.tenant_id, kind="case_set", document_id=case_set_id,
                state="active", payload=payload,
                expected_revision=current["revision"] if current else None,
            )
        except (TypeError, ValueError, OSError):
            return JSONResponse({"error": "invalid reviewed case set"}, status_code=400)

    @app.post("/api/experiments/run")
    def run_experiment(request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "experiment authorization required"}, status_code=403)
        if not _RUN_LOCK.acquire(blocking=False):
            return JSONResponse({"error": "experiment already running"}, status_code=409)
        try:
            case_set_id = payload.get("caseSetId")
            baseline = payload.get("baseline")
            candidate = payload.get("candidate")
            trials = payload.get("trials", 1)
            if (not all(isinstance(value, str) and _ID.fullmatch(value)
                        for value in (case_set_id, baseline, candidate))
                    or baseline == candidate or type(trials) is not int or not 1 <= trials <= 3):
                raise ValueError("invalid experiment request")
            runner, configs = _configuration()
            timeout_seconds = _trial_timeout_seconds()
            if baseline not in configs or candidate not in configs:
                raise ValueError("unknown configured variant")
            row = next((item for item in store.list_current(setup.tenant_id)
                        if item["kind"] == "case_set" and item["documentId"] == case_set_id
                        and item["state"] == "active"), None)
            if row is None:
                return JSONResponse({"error": "case set unavailable"}, status_code=404)
            if len(row["payload"]["cases"]) * 2 * trials * timeout_seconds > _MAX_RUN_SECONDS:
                return JSONResponse({"error": "experiment duration exceeds the 600-second runner limit"},
                                    status_code=400)
            cases = []
            for case in row["payload"]["cases"]:
                before = _side(case, configs[baseline], trials)
                after = _side(case, configs[candidate], trials)
                cases.append({
                    "id": case["id"], "expectedBehavior": case["expectedBehavior"],
                    "baseline": before, "candidate": after,
                    "comparableMetrics": _comparable_metrics(before, after),
                })
            result = {
                "caseSetId": case_set_id, "caseSetRevision": row["revision"],
                "runnerIdentity": runner,
                "baseline": baseline, "candidate": candidate,
                "baselineVersion": configs[baseline]["version"],
                "candidateVersion": configs[candidate]["version"],
                "configurationFingerprint": hashlib.sha256(json.dumps(
                    [configs[baseline], configs[candidate]], sort_keys=True,
                ).encode()).hexdigest(),
                "evaluatorIdentity": _CHECK_IDENTITY, "trialsPerSide": trials,
                "cases": cases, "comparison": _comparison(cases),
                "claim": "Observed fixed-case comparison; task quality outside these checks is unverified.",
            }
            safe = redact_structure(result)
            if not isinstance(safe, dict):
                raise ValueError("invalid experiment result")
            experiment_id = secrets.token_hex(16)
            store.append(setup.tenant_id, kind="experiment", document_id=experiment_id,
                         state="completed", payload=safe, expected_revision=None)
            return {"experimentId": experiment_id, **safe}
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid or unavailable experiment"}, status_code=400)
        finally:
            _RUN_LOCK.release()
