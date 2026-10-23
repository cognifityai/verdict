"""Canonical transaction boundary for normalized agent evidence."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from verdict.evidence import AgentCaptureBatch, AgentRunBundle
from verdict.local_recent_facts import LocalFullProjection, LocalRecentFact
from verdict.local_source_revision import LocalSourceRevision
from verdict.normalized_evidence import _LegacyLocalTextUpgrade
from verdict.schema import Trace
from verdict.storage.base import Storage


class AgentCaptureService:
    """Route one capture through the storage-owned transaction boundary."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def capture(
        self,
        bundle: AgentRunBundle,
        *,
        traces: Iterable[Trace] = (),
    ) -> None:
        self._storage.replace_agent_capture(bundle, tuple(traces))

    def _capture_local_history(
        self,
        bundle: AgentRunBundle,
        *,
        traces: Iterable[Trace] = (),
        legacy_text_upgrade: _LegacyLocalTextUpgrade | None = None,
        source_revision: LocalSourceRevision | None = None,
        source_path: Path | None = None,
        recent_window_json: str | None = None,
        recent_facts: tuple[LocalRecentFact, ...] = (),
        historical_facts: tuple[LocalRecentFact, ...] = (),
        full_projection: LocalFullProjection | None = None,
        recent_traces: tuple[Trace, ...] = (),
    ) -> None:
        """Use an optional built-in adapter hook for the a17 text transition."""
        capture_traces = tuple(traces)
        replace_local = getattr(self._storage, "_replace_local_agent_capture", None)
        if not callable(replace_local):
            if source_revision is not None:
                raise ValueError("local_source_revision_unsupported")
            self._storage.replace_agent_capture(bundle, capture_traces)
            return
        replace_local(
            bundle, capture_traces, legacy_text_upgrade,
            source_revision=source_revision, source_path=source_path,
            recent_window_json=recent_window_json,
            recent_facts=recent_facts, historical_facts=historical_facts,
            full_projection=full_projection,
            recent_traces=recent_traces,
        )

    def append(
        self,
        batch: AgentCaptureBatch,
        *,
        traces: Iterable[Trace] = (),
    ) -> None:
        self._storage.append_agent_capture(batch, tuple(traces))
