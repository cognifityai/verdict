"""Additive storage contract for count-cohort monitoring state."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from verdict.schema import (
    DriftRun,
    DriftSignal,
    MonitorMember,
    MonitorResult,
    MonitorSeries,
)


@runtime_checkable
class MonitorStorage(Protocol):
    def create_monitor_series(
        self,
        series: MonitorSeries,
        members: list[MonitorMember],
        *,
        snapshot: tuple[DriftRun, list[DriftSignal]] | None = None,
    ) -> None: ...

    def get_monitor_series(self, series_id: str) -> MonitorSeries | None: ...

    def get_active_monitor_series(self, scope_key: str) -> MonitorSeries | None: ...

    def list_monitor_series(self, *, scope_key: str | None = None) -> list[MonitorSeries]: ...

    def list_monitor_members(self, series_id: str) -> list[MonitorMember]: ...

    def list_monitor_results(self, series_id: str) -> list[MonitorResult]: ...

    def commit_monitor_cycle(
        self,
        *,
        series_id: str,
        expected_generation: int,
        members: list[MonitorMember],
        results: list[MonitorResult],
        snapshots: list[tuple[DriftRun, list[DriftSignal]]],
        late_arrival_delta: int,
    ) -> MonitorSeries: ...

    def activate_monitor_series(
        self,
        series_id: str,
        *,
        expected_active_series_id: str,
        snapshot: tuple[DriftRun, list[DriftSignal]] | None = None,
    ) -> MonitorSeries: ...
