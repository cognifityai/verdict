"""Dependency-free contracts for persisted cluster assignments.

The dashboard ships with the base package and must remain usable when the
optional evaluation package is not installed. Persisted cluster IDs and their
descriptive health summary therefore live at this lower layer.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass

UNCLUSTERED_ID = "unclustered"


@dataclass(frozen=True)
class ClusterHealth:
    """Observable health summary for a set of cluster assignments."""

    n_traces: int
    n_clusters: int
    median_cluster_size: float
    clusters_meeting_sample_floor: int
    min_sample_size: int
    is_fragmented: bool
    messages: tuple[str, ...]


def assess_cluster_health(
    cluster_ids: list[str | None], *, min_sample_size: int = 30
) -> ClusterHealth:
    """Describe assignment coverage without fitting or importing an evaluator."""
    usable = [cid for cid in cluster_ids if cid and cid != UNCLUSTERED_ID]
    counts = Counter(usable)
    sizes = list(counts.values())
    n_traces = len(usable)
    n_clusters = len(counts)
    median_size = float(statistics.median(sizes)) if sizes else 0.0
    ratio = n_clusters / n_traces if n_traces else 0.0
    fragmented = n_traces >= 4 and ratio > 0.5
    meeting_floor = sum(1 for size in sizes if size >= min_sample_size)

    messages: list[str] = []
    if not usable:
        messages.append("No usable intent-cluster assignments are available.")
    if fragmented:
        messages.append(
            f"Intent clustering is fragmented: {n_clusters} clusters for "
            f"{n_traces} traces ({ratio:.0%} clusters-to-traces). Use the "
            "semantic embedder or review the distance threshold before trusting drift results."
        )
    if sizes and median_size < min_sample_size:
        messages.append(
            f"Median cluster size is {median_size:g}; {meeting_floor}/{n_clusters} "
            f"clusters meet the {min_sample_size}-sample floor. Drift tests remain "
            "inactive for clusters below that floor; add traffic or use coarser, "
            "validated clusters."
        )

    return ClusterHealth(
        n_traces=n_traces,
        n_clusters=n_clusters,
        median_cluster_size=median_size,
        clusters_meeting_sample_floor=meeting_floor,
        min_sample_size=min_sample_size,
        is_fragmented=fragmented,
        messages=tuple(messages),
    )
