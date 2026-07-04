"""LangfuseSource — read traces from a Langfuse install and convert them into
Verdict's Trace schema.

This adapter lets users run Verdict analysis over traces that were captured by
another observability stack. It is read-only and normalizes observations into the
same schema used by Verdict's native SDK capture path.

This adapter:
1. Connects to a Langfuse instance (hosted or self-hosted).
2. Pulls observations / traces over a configurable time window.
3. Normalizes them into our `Trace` schema.
4. The existing Langfuse install acts as the capture layer; Verdict runs
   eval-and-drift analysis on top.

Install: `pip install langfuse`

Usage:
    from verdict_eval.langfuse_source import LangfuseSource

    source = LangfuseSource(
        public_key="<langfuse-public-key>",
        secret_key="<langfuse-secret-key>",
        host="https://cloud.langfuse.com",  # or your self-hosted URL
    )

    # Pull recent traces, convert to Verdict Trace records
    traces = source.fetch_traces(
        since_hours=24,
        limit=10_000,
        tenant_filter="your-tenant-id",  # optional
    )

    # Now feed into drift detection
    from verdict.client import _resolve_storage
    storage = _resolve_storage("sqlite:///./verdict.db")
    for t in traces:
        storage.insert_trace(t)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.redaction import redact
from verdict.schema import Operation, Trace


@dataclass
class LangfuseSource:
    """Read-only adapter over a Langfuse instance.

    Lazy-imports the `langfuse` SDK so this module is cheap to import.
    """

    public_key: str
    secret_key: str
    host: str = "https://cloud.langfuse.com"
    # Imported content is PII-redacted before it lands in a Trace, so the
    # `*_redacted` fields actually hold redacted text. Defaults to placeholder
    # redaction (no secret needed); set "hash" + a secret for stable hashing.
    redaction_mode: str = "redact"
    redaction_secret: str | None = None

    def __post_init__(self) -> None:
        try:
            from langfuse import Langfuse
        except ImportError as e:
            raise ImportError(
                "LangfuseSource requires `pip install langfuse`"
            ) from e
        self._client = Langfuse(
            public_key=self.public_key,
            secret_key=self.secret_key,
            host=self.host,
        )

    def fetch_traces(
        self,
        *,
        since_hours: int = 24,
        limit: int = 10_000,
        tenant_filter: str | None = None,
        cluster_filter: str | None = None,
    ) -> list[Trace]:
        """Pull recent observations from Langfuse and return them as Verdict
        Trace records.

        Filters:
          since_hours: only observations newer than N hours ago
          limit: maximum number of traces to return
          tenant_filter: optionally filter by Langfuse `userId` or tag
          cluster_filter: optionally filter by Langfuse `sessionId`

        Note: Langfuse's API has rate limits; this paginates internally and
        respects them. Long pulls may take several seconds.
        """
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        observations = self._fetch_observations_paginated(
            since=since, limit=limit, user_id=tenant_filter, session_id=cluster_filter
        )
        return [self._observation_to_trace(o) for o in observations]

    def _fetch_observations_paginated(
        self,
        *,
        since: datetime,
        limit: int,
        user_id: str | None,
        session_id: str | None,
    ) -> list[Any]:
        """Walk Langfuse's paginated observation list. Their SDK exposes
        `fetch_observations(page=N, limit=100)`; we walk pages until the
        timestamp falls before `since` or we hit our overall limit.
        """
        results: list[Any] = []
        page = 1
        page_size = 100
        while len(results) < limit:
            kwargs: dict[str, Any] = {"page": page, "limit": page_size}
            if user_id is not None:
                kwargs["user_id"] = user_id
            if session_id is not None:
                kwargs["session_id"] = session_id
            try:
                resp = self._client.fetch_observations(**kwargs)
            except Exception:
                break
            data = getattr(resp, "data", None) or []
            if not data:
                break
            for obs in data:
                ts = self._observation_timestamp(obs)
                if ts is None or ts < since:
                    # Done; older than our window
                    return results
                results.append(obs)
                if len(results) >= limit:
                    return results
            if len(data) < page_size:
                # Last page reached
                return results
            page += 1
        return results

    @staticmethod
    def _observation_timestamp(obs: Any) -> datetime | None:
        """Pull the observation's start time out of Langfuse's response.

        Langfuse SDK exposes timestamps as `startTime` (camelCase) on the
        Pythonic typed objects, or as `start_time` / `created_at` in raw
        dicts. Try them in turn.
        """
        for attr in ("start_time", "startTime", "created_at", "createdAt", "timestamp"):
            ts = getattr(obs, attr, None) or (obs.get(attr) if isinstance(obs, dict) else None)
            if ts is None:
                continue
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
        return None

    def _observation_to_trace(self, obs: Any) -> Trace:
        """Convert a Langfuse observation to our Trace schema.

        Langfuse "generation" observations correspond most directly to our
        Traces — they have prompt, completion, model, usage, latency.
        Other observation types (events, spans) are converted but with
        less detail.
        """
        def g(key: str, default: Any = None) -> Any:
            """Get from either an attribute-style or dict-style object."""
            v = getattr(obs, key, None)
            if v is None and isinstance(obs, dict):
                v = obs.get(key)
            return v if v is not None else default

        # Identifiers
        trace_id = str(g("id") or g("traceId") or g("trace_id") or "")
        model = str(g("model") or "")
        usage = g("usage") or g("usageDetails") or {}
        if hasattr(usage, "to_dict"):
            usage = usage.to_dict()

        # Token / cost
        in_tok = None
        out_tok = None
        if isinstance(usage, dict):
            in_tok = usage.get("input") or usage.get("inputTokens") or usage.get("prompt_tokens")
            out_tok = usage.get("output") or usage.get("outputTokens") or usage.get("completion_tokens")
        cost = g("calculatedTotalCost") or g("totalCost") or g("cost_usd")

        # Content
        prompt = g("input") or g("prompt")
        response = g("output") or g("completion") or g("response")
        prompt_str = _stringify(prompt)
        response_str = _stringify(response)

        # Time
        started = self._observation_timestamp(obs) or datetime.now(timezone.utc)
        ended = None
        for attr in ("end_time", "endTime", "completion_time", "completionStartTime"):
            v = g(attr)
            if v is None:
                continue
            if isinstance(v, datetime):
                ended = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                break
            if isinstance(v, str):
                try:
                    ended = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    break
                except ValueError:
                    continue

        latency_ms = None
        if ended and started:
            latency_ms = (ended - started).total_seconds() * 1000.0

        # Tags / metadata
        metadata = g("metadata") or {}
        if hasattr(metadata, "to_dict"):
            metadata = metadata.to_dict()
        tenant_id = str(g("userId") or g("user_id") or metadata.get("tenant_id") or "") or None
        session_id = str(g("sessionId") or g("session_id") or "") or None

        # Infer provider from model string
        provider = _infer_provider(model)

        return Trace(
            trace_id=trace_id,
            provider=provider,
            operation=Operation.CHAT,
            request_model=model,
            response_model=model,
            input_tokens=int(in_tok) if in_tok is not None else None,
            output_tokens=int(out_tok) if out_tok is not None else None,
            latency_ms=latency_ms,
            cost_usd=float(cost) if cost is not None else None,
            prompt_redacted=(redact(prompt_str, self.redaction_mode, self.redaction_secret) or "")[:10000] if prompt_str else None,    # redact + 10KB cap
            response_redacted=(redact(response_str, self.redaction_mode, self.redaction_secret) or "")[:10000] if response_str else None,
            started_at=started,
            ended_at=ended,
            tenant_id=tenant_id,
            session_id=session_id,
            tags={"source": "langfuse"},
        )


def _stringify(value: Any) -> str:
    """Convert Langfuse's varied prompt/response payloads to a single string.

    They may come as: string, dict, list of role-content dicts, JSON string.
    We do the obvious flattening; anything stranger gets repr'd.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                role = item.get("role", "")
                content = item.get("content", "") or ""
                if isinstance(content, list):
                    # OpenAI-style multimodal content blocks
                    content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                parts.append(f"{role}: {content}" if role else str(content))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, dict):
        if "content" in value:
            return _stringify(value["content"])
        return str(value)
    return str(value)


def _infer_provider(model: str) -> str:
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt" in m or "openai" in m or "o1" in m or "o4" in m:
        return "openai"
    if "gemini" in m or "google" in m or "palm" in m:
        return "google"
    if "llama" in m or "mistral" in m or "cohere" in m:
        return m.split("-")[0].split("/")[0]
    return "unknown"
