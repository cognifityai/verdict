"""Small domain types shared by telemetry source adapters."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from verdict.schema import Trace

_TRACE_NAMESPACE = UUID("bd452b3a-451e-5bb5-b4c5-b0057976227a")
_ADAPTER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ROUTING_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def safe_routing_id(value: object) -> str | None:
    """Return a bounded non-content routing identifier or ``None``."""
    if not isinstance(value, str) or _ROUTING_RE.fullmatch(value) is None:
        return None
    return value


@dataclass(frozen=True)
class ImportContext:
    """Operator-owned identity and routing context for one import source."""

    adapter: str
    source_scope: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, str) or _ADAPTER_RE.fullmatch(self.adapter) is None:
            raise ValueError("adapter must be a lowercase bounded identifier")
        try:
            scope_size = len(self.source_scope.encode("utf-8"))
        except (AttributeError, UnicodeEncodeError):
            scope_size = 0
        if (
            not isinstance(self.source_scope, str)
            or not self.source_scope.strip()
            or scope_size == 0
            or scope_size > 512
        ):
            raise ValueError("source_scope must be a non-empty UTF-8 value of at most 512 bytes")
        if self.tenant_id is not None and safe_routing_id(self.tenant_id) is None:
            raise ValueError("tenant_id must be a non-sensitive bounded routing identifier")

    @property
    def scope_digest(self) -> str:
        return hashlib.sha256(self.source_scope.encode("utf-8")).hexdigest()[:16]

    def trace_id(self, external_id: str, external_trace_id: str | None = None) -> str:
        if not isinstance(external_id, str) or not external_id:
            raise ValueError("external_id must not be empty")
        if external_trace_id is not None and not isinstance(external_trace_id, str):
            raise ValueError("external_trace_id must be a string when provided")
        name = json.dumps(
            [
                self.adapter,
                self.tenant_id or "",
                self.source_scope,
                external_trace_id or "",
                external_id,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return uuid5(_TRACE_NAMESPACE, name).hex

    def provenance_tags(
        self, external_id: str, external_trace_id: str | None = None
    ) -> dict[str, str]:
        """Build bounded provenance without persisting raw source identifiers."""
        tags = {
            "verdict.source": self.adapter,
            "verdict.source_scope": self.scope_digest,
            "verdict.source_record": hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:16],
        }
        if external_trace_id:
            tags["verdict.source_trace"] = hashlib.sha256(
                external_trace_id.encode("utf-8")
            ).hexdigest()[:16]
        return tags


@dataclass(frozen=True)
class MappingResult:
    """Exactly one terminal mapping outcome for one source record."""

    trace: Trace | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.trace is None) == (self.skip_reason is None):
            raise ValueError("mapping result must contain exactly one trace or skip reason")

    @classmethod
    def mapped(cls, trace: Trace) -> MappingResult:
        return cls(trace=trace)

    @classmethod
    def skipped(cls, reason: str) -> MappingResult:
        if not reason:
            raise ValueError("skip reason must not be empty")
        return cls(skip_reason=reason)


@dataclass
class ImportSummary:
    """Bounded counters returned by a synchronous import run."""

    seen: int = 0
    stored: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def add_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
