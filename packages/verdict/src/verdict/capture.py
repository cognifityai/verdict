"""Canonical transaction boundary for normalized agent evidence."""

from __future__ import annotations

from collections.abc import Iterable

from verdict.evidence import AgentRunBundle
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
