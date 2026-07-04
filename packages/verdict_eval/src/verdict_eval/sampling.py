"""Stratified judge sampling — spend the judging budget where the statistics need it.

The drift detector needs >= `min_sample_size` (default 30) judged traces in EACH
window (current AND baseline) for EACH cluster before it can run a test on that
cluster. Judging costs one LLM call per trace, so you can't judge everything.

The old pipeline sampled uniformly at random (`--sample-rate`). That is the worst
of both worlds:

  - High-volume clusters get judged thousands of times — far past the 30 needed,
    burning budget on redundant samples.
  - Low-volume-but-important clusters never reach 30, so they silently drop out
    of drift detection entirely. (This is exactly why the old 8-hour run, with
    117 judgments spread uniformly, could not feed the detector.)

Stratified sampling fixes both: allocate judgments PER (cluster, window) cell up
to a target, sampling within a cell when it has more traffic than the target and
taking everything when it has less. Spend is capped per cell; coverage is
guaranteed wherever the traffic exists.

What it deliberately does NOT do: manufacture data. If a cell has only 12 traces,
you cannot judge 30 — the plan marks that cell `under_covered` so the gap is
visible instead of silently shrinking the window. Honest coverage beats a number
that looks complete but isn't.

This module is pure stdlib (no scipy/sklearn), so it imports and tests cheaply.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol


class _HasClusterAndTime(Protocol):
    trace_id: str
    cluster_id: str | None
    started_at: datetime


@dataclass(frozen=True)
class WindowSpec:
    """Defines the current/baseline windows. Boundaries match
    `drift.split_windows_by_time` exactly so the sampler and the detector agree
    on which trace falls in which window."""

    now: datetime | None = None
    current_hours: int = 24
    baseline_days: int = 7
    baseline_lag_hours: int = 24

    def _now(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def label(self, ts: datetime) -> str | None:
        """Return 'current', 'baseline', or None (in the gap / out of range)."""
        now = self._now()
        cur_start = now - timedelta(hours=self.current_hours)
        base_end = now - timedelta(hours=self.baseline_lag_hours)
        base_start = base_end - timedelta(days=self.baseline_days)
        if ts >= cur_start:
            return "current"
        if base_start <= ts < base_end:
            return "baseline"
        return None


@dataclass
class CellPlan:
    """Sampling plan for one (cluster_id, window) cell."""
    cluster_id: str
    window: str
    existing: int        # judgments already present for this cell
    available: int       # unjudged, judge-able traces available in this cell
    to_judge: int        # how many we will judge this run
    target: int

    @property
    def reached(self) -> int:
        return self.existing + self.to_judge

    @property
    def under_covered(self) -> bool:
        # We will fall short of the target even after judging all available
        # traffic — not a sampler failure, just not enough data.
        return self.reached < self.target


@dataclass
class SamplePlan:
    selected_trace_ids: list[str] = field(default_factory=list)
    cells: list[CellPlan] = field(default_factory=list)
    target_per_cell: int = 0

    @property
    def total_to_judge(self) -> int:
        return len(self.selected_trace_ids)

    @property
    def under_covered_cells(self) -> list[CellPlan]:
        return [c for c in self.cells if c.under_covered]

    @property
    def covered_cells(self) -> list[CellPlan]:
        return [c for c in self.cells if not c.under_covered]

    def summary(self) -> str:
        n_cov = len(self.covered_cells)
        n_under = len(self.under_covered_cells)
        lines = [
            f"Stratified plan: judge {self.total_to_judge} trace(s) across "
            f"{len(self.cells)} (cluster, window) cell(s); target {self.target_per_cell}/cell.",
            f"  reachable cells: {n_cov}   under-covered (insufficient traffic): {n_under}",
        ]
        for c in sorted(self.under_covered_cells, key=lambda c: (c.cluster_id, c.window)):
            lines.append(
                f"    UNDER-COVERED {c.cluster_id}/{c.window}: only {c.reached} "
                f"reachable < target {c.target} (existing {c.existing} + {c.to_judge} new)"
            )
        return "\n".join(lines)


@dataclass
class StratifiedJudgeSampler:
    """Plan which traces to judge so each (cluster, window) cell reaches a target.

    Parameters
    ----------
    target_per_cell:
        Desired judged traces per (cluster, window). Default 40 — a margin above
        the detector's default min_sample_size=30 so UNCLEAR exclusions don't
        drop the scored count below 30. One judgment scores all rubric
        dimensions, so a per-trace target also covers each (cluster, dimension).
    seed:
        RNG seed for reproducible within-cell sampling.
    """

    target_per_cell: int = 40
    seed: int = 0

    def plan(
        self,
        traces: Iterable[_HasClusterAndTime],
        *,
        window: WindowSpec,
        already_judged_trace_ids: set[str] | None = None,
    ) -> SamplePlan:
        already = already_judged_trace_ids or set()
        rng = random.Random(self.seed)

        # Bucket traces into (cluster, window) cells, splitting judged vs not.
        # Only traces with a cluster_id and a non-gap window are eligible.
        existing: dict[tuple[str, str], int] = {}
        unjudged: dict[tuple[str, str], list[str]] = {}
        for t in traces:
            cid = getattr(t, "cluster_id", None)
            if not cid:
                continue
            win = window.label(t.started_at)
            if win is None:
                continue
            key = (cid, win)
            if t.trace_id in already:
                existing[key] = existing.get(key, 0) + 1
            else:
                unjudged.setdefault(key, []).append(t.trace_id)

        plan = SamplePlan(target_per_cell=self.target_per_cell)
        all_keys = set(existing) | set(unjudged)
        for key in sorted(all_keys):
            cid, win = key
            have = existing.get(key, 0)
            pool = unjudged.get(key, [])
            need = max(0, self.target_per_cell - have)
            take = min(need, len(pool))
            if take > 0:
                # Deterministic within-cell sample (sort first so order of the
                # input iterable doesn't change which ids are picked).
                chosen = rng.sample(sorted(pool), take)
                plan.selected_trace_ids.extend(chosen)
            plan.cells.append(CellPlan(
                cluster_id=cid, window=win, existing=have,
                available=len(pool), to_judge=take, target=self.target_per_cell,
            ))
        return plan


def uniform_coverage_estimate(
    traces: Iterable[_HasClusterAndTime],
    *,
    window: WindowSpec,
    sample_rate: float,
    min_sample_size: int = 30,
) -> dict[str, int]:
    """For contrast: how many (cluster, window) cells would naive uniform
    sampling at `sample_rate` bring to >= min_sample_size, and how much it would
    over/under spend. Returns a small dict for the report."""
    cell_counts: dict[tuple[str, str], int] = {}
    for t in traces:
        cid = getattr(t, "cluster_id", None)
        if not cid:
            continue
        win = window.label(t.started_at)
        if win is None:
            continue
        cell_counts[(cid, win)] = cell_counts.get((cid, win), 0) + 1
    judged = 0
    reached = 0
    for n in cell_counts.values():
        expected = n * sample_rate
        judged += round(expected)
        if expected >= min_sample_size:
            reached += 1
    return {
        "cells_total": len(cell_counts),
        "cells_reaching_floor": reached,
        "expected_judgments": judged,
    }
