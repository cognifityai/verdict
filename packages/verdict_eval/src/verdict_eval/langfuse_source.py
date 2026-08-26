"""Compatibility wrapper for the core Langfuse telemetry adapter.

New integrations should use ``verdict-import langfuse``. This class preserves
the published alpha ``LangfuseSource.fetch_traces`` surface while sharing the
current v2 API reader and normalization logic with the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.redaction import RedactionMode, sanitize_trace
from verdict.schema import Trace
from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.model import ImportContext
from verdict.telemetry.normalize import parse_datetime
from verdict.telemetry.sources.langfuse import (
    LangfuseApiSource,
    map_langfuse_observation,
)


def _get(value: object, *names: str) -> Any:
    for name in names:
        candidate = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


@dataclass
class LangfuseSource:
    """Read Langfuse generations and return Verdict ``Trace`` records."""

    public_key: str
    secret_key: str
    host: str = "https://cloud.langfuse.com"
    redaction_mode: RedactionMode = "redact"
    redaction_secret: str | None = None

    def __post_init__(self) -> None:
        if not self.public_key or not self.secret_key:
            raise ValueError("Langfuse public and secret keys are required")
        if self.redaction_mode not in {"redact", "hash"}:
            raise ValueError("redaction_mode must be redact or hash")
        if self.redaction_mode == "hash" and not self.redaction_secret:
            raise ValueError("hash redaction requires redaction_secret")

    def fetch_traces(
        self,
        *,
        since_hours: int = 24,
        limit: int = 10_000,
        tenant_filter: str | None = None,
        cluster_filter: str | None = None,
    ) -> list[Trace]:
        if since_hours <= 0 or limit <= 0:
            raise ValueError("since_hours and limit must be positive")
        end = datetime.now(timezone.utc)
        source = LangfuseApiSource(
            client=JsonHttpClient(),
            context=ImportContext(adapter="langfuse", source_scope=self.host),
            base_url=self.host,
            public_key=self.public_key,
            secret_key=self.secret_key,
            start_time=end - timedelta(hours=since_hours),
            end_time=end,
            user_id=tenant_filter,
            session_id=cluster_filter,
            max_records=limit,
        )
        return [self._observation_to_trace(row) for row in source.iter_observations()]

    @staticmethod
    def _observation_timestamp(obs: object) -> datetime | None:
        return parse_datetime(
            _get(obs, "start_time", "startTime", "created_at", "createdAt", "timestamp")
        )

    def _observation_to_trace(self, obs: object) -> Trace:
        record = {
            "id": _get(obs, "id", "traceId", "trace_id"),
            "traceId": _get(obs, "traceId", "trace_id"),
            "type": _get(obs, "type", "observationType") or "GENERATION",
            "startTime": self._observation_timestamp(obs) or datetime.now(timezone.utc),
            "endTime": _get(obs, "end_time", "endTime", "completion_time"),
            "providedModelName": _get(obs, "providedModelName", "model"),
            "modelParameters": _get(obs, "modelParameters") or {},
            "input": _get(obs, "input", "prompt"),
            "output": _get(obs, "output", "completion", "response"),
            "usageDetails": _get(obs, "usageDetails", "usage") or {},
            "costDetails": _get(obs, "costDetails") or {},
            "totalCost": _get(obs, "totalCost", "calculatedTotalCost", "cost_usd"),
            "sessionId": _get(obs, "sessionId", "session_id"),
            "level": _get(obs, "level") or "DEFAULT",
            "statusMessage": _get(obs, "statusMessage"),
            "metadata": _get(obs, "metadata") or {},
        }
        result = map_langfuse_observation(
            record,
            ImportContext(adapter="langfuse", source_scope=self.host),
        )
        if result.trace is None:
            raise ValueError(f"Langfuse observation is not importable: {result.skip_reason}")
        trace = result.trace
        legacy_id = record["id"]
        if isinstance(legacy_id, str) and legacy_id:
            trace.trace_id = legacy_id
        trace.tenant_id = _get(obs, "userId", "user_id")
        trace.tags = {"source": "langfuse"}
        trace.prompt_redacted = (
            trace.prompt_redacted[:10_000] if trace.prompt_redacted is not None else None
        )
        trace.response_redacted = (
            trace.response_redacted[:10_000] if trace.response_redacted is not None else None
        )
        return sanitize_trace(trace, self.redaction_mode, self.redaction_secret)
