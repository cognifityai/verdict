"""Canonical transaction boundary for normalized agent evidence."""

from __future__ import annotations

from collections.abc import Iterable

from verdict.evidence import AgentCaptureBatch, AgentRunBundle
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
    ) -> None:
        """Use an optional built-in adapter hook for the a17 text transition."""
        capture_traces = tuple(traces)
        replace_local = getattr(self._storage, "_replace_local_agent_capture", None)
        if legacy_text_upgrade is None or not callable(replace_local):
            self._storage.replace_agent_capture(bundle, capture_traces)
            return
        replace_local(bundle, capture_traces, legacy_text_upgrade)

    def append(
        self,
        batch: AgentCaptureBatch,
        *,
        traces: Iterable[Trace] = (),
    ) -> None:
        self._storage.append_agent_capture(batch, tuple(traces))
