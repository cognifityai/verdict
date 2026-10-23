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
from verdict.delivery_queue import NotificationIntent

ANALYZER_VERSION = "agent-insights-v5"
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
        "verifiedRevision": run.verified_revision,
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
        "verifiedRevision": None,
        "inputFingerprint": None,
    }
    return result


def read_latest_analysis(
    storage_url: str, *, tenant: str, empty_result: dict[str, Any],
) -> dict[str, Any]:
    storage = _storage(storage_url)
    try:
        run = storage.get_latest_deterministic_analysis_run(
            tenant,
            SCOPE_KEY,
            analyzer_version=ANALYZER_VERSION,
        )
    finally:
        storage.close()
    if run is None or run.analyzer_version != ANALYZER_VERSION:
        return never_run_response(empty_result)
    return _run_response(run, empty_result=empty_result)


def run_analysis(
    storage_url: str,
    *,
    tenant: str,
    build: Callable[[], dict[str, Any]],
    delivery_intent_factory: Callable[[dict[str, Any]], list[NotificationIntent]] | None = None,
) -> dict[str, Any]:
    """Compute once, then publish one immutable terminal snapshot atomically."""
    # Ensure the evidence revision schema exists before the read-only builder
    # takes its snapshot. Close the writer handle before any evidence scan.
    storage = _storage(storage_url)
    storage.close()
    cutoff = datetime.now(timezone.utc)
    try:
        result = build()
        fingerprint = result.pop("_analysisInputFingerprint", None)
        evidence_revision = result.pop("_analysisEvidenceRevision", None)
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("analysis builder did not provide an input fingerprint")
        if evidence_revision is not None and (
            isinstance(evidence_revision, bool) or not isinstance(evidence_revision, int)
            or evidence_revision < 0
        ):
            raise ValueError("analysis builder did not provide a valid evidence revision")
        status = AnalysisRunStatus.COMPLETED
    except Exception as exc:
        result = {
            "schema": "agent-insights-v2",
            "error": {
                "code": "analysis_failed",
                "causeType": type(exc).__name__,
                "attemptedAt": cutoff.isoformat(),
            },
        }
        fingerprint = hashlib.sha256(_canonical(result)).hexdigest()
        evidence_revision = None
        status = AnalysisRunStatus.ERROR

    identity_material = {
        "tenant": tenant,
        "scope": SCOPE_KEY,
        "version": ANALYZER_VERSION,
        "input": fingerprint,
        "status": status.value,
    }
    analysis_id = hashlib.sha256(_canonical(identity_material)).hexdigest()
    intents = delivery_intent_factory(result) if delivery_intent_factory is not None else []
    storage = _storage(storage_url)
    try:
        prior = storage.get_deterministic_analysis_run(analysis_id)
        if prior is not None:
            if prior.result != result or prior.status is not status:
                raise ValueError("analysis input produced different content")
            prior = (
                storage.verify_worker_analysis_run(
                    analysis_id, revision=evidence_revision, intents=intents,
                ) if delivery_intent_factory is not None else
                storage.verify_deterministic_analysis_run(
                    analysis_id, revision=evidence_revision,
                )
            )
            return _run_response(prior)
        completed_at = datetime.now(timezone.utc)
        run = DeterministicAnalysisRun(
            analysis_id=analysis_id,
            tenant_id=tenant,
            scope_key=SCOPE_KEY,
            cutoff=cutoff,
            completed_at=completed_at,
            status=status,
            analyzer_version=ANALYZER_VERSION,
            input_fingerprint=fingerprint,
            result=result,
            verified_revision=evidence_revision,
        )
        try:
            if delivery_intent_factory is not None:
                storage.save_worker_analysis_run(run, intents)
                published = storage.verify_worker_analysis_run(
                    analysis_id, revision=evidence_revision, intents=intents,
                )
            else:
                storage.save_deterministic_analysis_run(run)
                published = storage.verify_deterministic_analysis_run(
                    analysis_id, revision=evidence_revision,
                )
            response = _run_response(published)
        except ValueError:
            # Another process may have saved the same identity after our read.
            prior = storage.get_deterministic_analysis_run(analysis_id)
            if prior is None or prior.result != result or prior.status is not status:
                raise
            prior = (
                storage.verify_worker_analysis_run(
                    analysis_id, revision=evidence_revision, intents=intents,
                ) if delivery_intent_factory is not None else
                storage.verify_deterministic_analysis_run(
                    analysis_id, revision=evidence_revision,
                )
            )
            response = _run_response(prior)
    finally:
        storage.close()
    return response
