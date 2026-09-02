"""Application service for immutable, user-visible deterministic analysis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.analysis_records import (
    AnalysisRunStatus,
    DeterministicAnalysisRun,
)
from verdict.dashboard.storage_url import is_postgres_storage

ANALYZER_VERSION = "agent-insights-v1"
SCOPE_KEY = "agent-and-trace"


def _storage(storage_url: str):
    if is_postgres_storage(storage_url):
        from verdict.storage.postgres import PostgresStorage

        return PostgresStorage(storage_url)
    from verdict.storage.sqlite import SQLiteStorage

    path = storage_url[len("sqlite:///"):] if storage_url.startswith("sqlite:///") else storage_url
    return SQLiteStorage(str(Path(path)))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _run_response(
    run: DeterministicAnalysisRun, *, empty_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = json.loads(json.dumps(run.result))
    if run.status is AnalysisRunStatus.ERROR and empty_result is not None:
        error = result.get("error")
        result = json.loads(json.dumps(empty_result))
        result["error"] = error
    result["analysisState"] = {
        "status": run.status.value,
        "analysisId": run.analysis_id,
        "analyzerVersion": run.analyzer_version,
        "cutoff": run.cutoff.isoformat(),
        "completedAt": run.completed_at.isoformat(),
        "inputFingerprint": run.input_fingerprint,
    }
    return result


def never_run_response(empty_result: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(empty_result))
    result["analysisState"] = {
        "status": "never_run",
        "analysisId": None,
        "analyzerVersion": ANALYZER_VERSION,
        "cutoff": None,
        "completedAt": None,
        "inputFingerprint": None,
    }
    return result


def read_latest_analysis(
    storage_url: str, *, tenant: str, empty_result: dict[str, Any],
) -> dict[str, Any]:
    storage = _storage(storage_url)
    try:
        run = storage.get_latest_deterministic_analysis_run(tenant, SCOPE_KEY)
    finally:
        storage.close()
    return (
        _run_response(run, empty_result=empty_result)
        if run is not None else never_run_response(empty_result)
    )


def run_analysis(
    storage_url: str,
    *,
    tenant: str,
    build: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Compute once, then publish one immutable terminal snapshot atomically."""
    cutoff = datetime.now(timezone.utc)
    storage = _storage(storage_url)
    try:
        try:
            result = build()
            fingerprint = result.pop("_analysisInputFingerprint", None)
            if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                raise ValueError("analysis builder did not provide an input fingerprint")
            status = AnalysisRunStatus.COMPLETED
        except Exception as exc:
            result = {
                "schema": "agent-insights-v1",
                "error": {
                    "code": "analysis_failed",
                    "causeType": type(exc).__name__,
                    "attemptedAt": cutoff.isoformat(),
                },
            }
            fingerprint = hashlib.sha256(_canonical(result)).hexdigest()
            status = AnalysisRunStatus.ERROR
        latest = storage.get_latest_deterministic_analysis_run(tenant, SCOPE_KEY)
        if (
            latest is not None
            and latest.analyzer_version == ANALYZER_VERSION
            and latest.input_fingerprint == fingerprint
            and latest.status is status
        ):
            return _run_response(latest)
        identity_material = {
            "tenant": tenant,
            "scope": SCOPE_KEY,
            "version": ANALYZER_VERSION,
            "input": fingerprint,
            "status": status.value,
        }
        completed_at = datetime.now(timezone.utc)
        run = DeterministicAnalysisRun(
            analysis_id=hashlib.sha256(_canonical(identity_material)).hexdigest(),
            tenant_id=tenant,
            scope_key=SCOPE_KEY,
            cutoff=cutoff,
            completed_at=completed_at,
            status=status,
            analyzer_version=ANALYZER_VERSION,
            input_fingerprint=fingerprint,
            result=result,
        )
        storage.save_deterministic_analysis_run(run)
        response = _run_response(run)
    finally:
        storage.close()
    return response
