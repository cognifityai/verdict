"""Stable, persistent intent clustering for cross-run drift comparison.

The drift detector compares quality scores for the *same* cluster across time
windows ("this week's cluster X vs. last week's cluster X"). That is only valid
if a cluster keeps the same ID run-to-run.

The Birch-based `IntentClusterer` cannot guarantee that: it re-fits from scratch
each run and emits Birch's integer labels, whose numbering is an artifact of the
CF-tree built during that fit. So `c0003` this week is not the same intent as
`c0003` last week, and the week-over-week comparison silently lines up
mismatched buckets.

This module replaces *label-based* identity with *assignment-based* identity:

  - A persistent `ClusterRegistry` of (stable_id, unit-norm centroid).
  - Each prompt is embedded and assigned to its nearest centroid by cosine
    distance. If the nearest centroid is within `threshold`, the prompt joins
    that cluster and nudges its centroid (running mean). Otherwise a brand-new
    stable_id is minted and seeded.
  - Existing IDs are never renumbered. Adding traffic only ever (a) grows an
    existing cluster or (b) creates a new one — it cannot relabel old ones.

Two hard requirements for this to actually be stable:

  1. The embedder must be *deterministic across runs* — the same text must embed
     to the same vector every run. Both shipped embedders satisfy this, but only
     `SentenceTransformerEmbedder` captures paraphrased semantic intent well.
     `HashingEmbedder` is a lexical fallback and can fragment paraphrases.
  2. The registry must be persisted between runs (see `save`/`load`). Clustering
     happens when an unassigned trace first reaches the analysis pipeline; later
     runs read its stored cluster_id and do not re-cluster it by default.

`clustering_version` tags every assignment. Bumping the embedder or threshold
bumps the version so drift signals computed under incompatible cluster
definitions are never compared.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

UNCLUSTERED_ID = "unclustered"


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


@dataclass(frozen=True)
class ClusterHealth:
    """Observable health summary for an intent-clustering result."""

    n_traces: int
    n_clusters: int
    median_cluster_size: float
    clusters_meeting_sample_floor: int
    min_sample_size: int
    is_fragmented: bool
    messages: tuple[str, ...]


def assess_cluster_health(
    cluster_ids: list[str | None], *, min_sample_size: int = 30,
) -> ClusterHealth:
    """Flag clustering that cannot support meaningful per-cluster drift tests.

    Fragmentation and low volume are reported separately. Ten singleton
    clusters are a clustering failure; two coherent clusters with twenty
    traces each simply need more traffic.
    """
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


@dataclass
class ClusterRegistry:
    """Persistent set of (stable_id, unit-norm centroid).

    Centroids are stored normalized so cosine distance is `1 - dot`. Member
    counts back a running-mean centroid update.
    """

    ids: list[str] = field(default_factory=list)
    centroids: list[list[float]] = field(default_factory=list)  # JSON-friendly
    counts: list[int] = field(default_factory=list)
    next_index: int = 0
    version: str = "v1"
    distance_threshold: float | None = None
    embedding_dimension: int | None = None

    # -- assignment --------------------------------------------------------- #

    def assign(self, vec: np.ndarray, *, threshold: float, freeze_after: int) -> tuple[str, bool]:
        """Assign `vec` to the nearest centroid or mint a new cluster.

        Returns (stable_id, is_new). `threshold` is the max cosine distance for
        a match. `freeze_after` stops a centroid from drifting once it has that
        many members (0 = never freeze), so a cluster's identity stays anchored
        to what originally defined it.
        """
        u = _unit(np.asarray(vec, dtype=np.float64))
        if self.centroids:
            mat = np.asarray(self.centroids, dtype=np.float64)
            dists = 1.0 - mat @ u  # cosine distance to every centroid
            j = int(np.argmin(dists))
            if float(dists[j]) <= threshold:
                self._update(j, u, freeze_after=freeze_after)
                return self.ids[j], False
        return self._mint(u), True

    def _update(self, j: int, u: np.ndarray, *, freeze_after: int) -> None:
        self.counts[j] += 1
        if freeze_after and self.counts[j] > freeze_after:
            return  # identity locked; ignore further drift
        n = self.counts[j]
        old = np.asarray(self.centroids[j], dtype=np.float64)
        new = _unit(old + (u - old) / n)  # incremental mean, re-normalized
        self.centroids[j] = new.tolist()

    def _mint(self, u: np.ndarray) -> str:
        cid = f"{self.version}-{self.next_index:06d}"
        self.next_index += 1
        self.ids.append(cid)
        self.centroids.append(u.tolist())
        self.counts.append(1)
        return cid

    # -- persistence -------------------------------------------------------- #

    def to_json(self) -> str:
        return json.dumps({
            "ids": self.ids,
            "centroids": self.centroids,
            "counts": self.counts,
            "next_index": self.next_index,
            "version": self.version,
            "distance_threshold": self.distance_threshold,
            "embedding_dimension": self.embedding_dimension,
        })

    @classmethod
    def from_json(cls, s: str | None) -> ClusterRegistry:
        if not s:
            return cls()
        d = json.loads(s)
        return cls(
            ids=d["ids"], centroids=d["centroids"], counts=d["counts"],
            next_index=d["next_index"], version=d.get("version", "v1"),
            distance_threshold=d.get("distance_threshold"),
            embedding_dimension=d.get("embedding_dimension"),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json())

    @classmethod
    def load(cls, path: str | Path) -> ClusterRegistry:
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_json(p.read_text())


@dataclass
class StableIntentClusterer:
    """Streaming clusterer with stable, persistent cluster IDs.

    Parameters
    ----------
    embedder:
        Anything with `.embed(list[str]) -> np.ndarray`. MUST be deterministic
        across runs (see module docstring).
    threshold:
        Max cosine distance for a prompt to join an existing cluster. Higher =
        coarser clusters. Cosine distance is in [0, 2]; 0.50 is the shipped
        starting point for the MiniLM adapter, but production workloads should
        validate it against representative prompts.
    freeze_after:
        Lock a centroid once it has this many members so its identity stays
        anchored. 0 disables freezing.
    min_chars:
        Prompts shorter than this (after strip) route to UNCLUSTERED_ID instead
        of fragmenting the space — replaces the request_model/trace_id fallback.
    """

    embedder: object
    threshold: float = 0.50
    freeze_after: int = 200
    min_chars: int = 3
    registry: ClusterRegistry = field(default_factory=ClusterRegistry)

    def __post_init__(self) -> None:
        dimension = getattr(self.embedder, "dim", None)
        if not isinstance(dimension, int) or dimension <= 0:
            raise ValueError("Embedder must expose a positive integer `dim` attribute.")

        stored = self.registry.distance_threshold
        if stored is None:
            if self.registry.ids:
                raise ValueError(
                    "Existing cluster registry has no recorded distance threshold; "
                    "bump clustering_version and rebuild it before assigning new traffic."
                )
            self.registry.distance_threshold = self.threshold
        elif not math.isclose(stored, self.threshold, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(
                f"Cluster registry threshold is {stored}, but this run requested "
                f"{self.threshold}; use the stored threshold or bump clustering_version."
            )

        stored_dimension = self.registry.embedding_dimension
        if stored_dimension is None:
            if self.registry.ids:
                raise ValueError(
                    "Existing cluster registry has no recorded embedding dimension; "
                    "bump clustering_version and rebuild it before assigning new traffic."
                )
            self.registry.embedding_dimension = dimension
        elif stored_dimension != dimension:
            raise ValueError(
                f"Cluster registry embedding dimension is {stored_dimension}, but this "
                f"embedder produces {dimension}; bump clustering_version before changing "
                "the embedding model."
            )

    def assign(self, texts: list[str]) -> list[str]:
        """Assign each text a stable cluster_id, independent of input order.

        Order-independence is a hard requirement: the previous implementation
        walked the batch sequentially and mutated the registry as it went, so a
        prompt could land in a different cluster depending on what preceded it
        in the same call. That silently undermines the stability guarantee this
        module exists to provide.

        The fix has two phases:

          1. **Match against a frozen snapshot.** Existing centroids are
             snapshotted at the start of the call; every text is matched
             against that snapshot. So whether a text joins an existing cluster
             — and which one — does NOT depend on batch order or on the other
             texts in the batch. Matched centroids are updated only after every
             assignment decision, in canonical text order.
          2. **Mint new clusters in canonical order.** Texts that match no
             existing cluster are processed in a canonical (sorted-by-text)
             order, so brand-new clusters are minted deterministically and
             same-intent new texts group together — regardless of the order the
             caller passed them.

        Guarantee: for a given starting registry, any permutation of the same
        `texts` yields identical per-text ids and an identical resulting
        registry. (A brand-new cluster's *numeric* id reflects how many new
        clusters preceded it, but that value is irrelevant once persisted — what
        matters, cross-run stability of existing clusters, holds exactly.)
        """
        if not texts:
            return []
        out: list[str] = [UNCLUSTERED_ID] * len(texts)
        usable_idx = [i for i, t in enumerate(texts) if len((t or "").strip()) >= self.min_chars]
        if not usable_idx:
            return out
        vecs = self.embedder.embed([texts[i] for i in usable_idx])

        # Phase 1: match against a frozen snapshot of existing centroids.
        snap_ids = list(self.registry.ids)
        snap = (np.asarray(self.registry.centroids, dtype=np.float64)
                if self.registry.centroids else None)
        unmatched: list[int] = []   # positions k into usable_idx
        matched_updates: dict[int, list[tuple[str, np.ndarray]]] = {}
        for k, i in enumerate(usable_idx):
            u = _unit(np.asarray(vecs[k], dtype=np.float64))
            if snap is not None and len(snap):
                dists = 1.0 - snap @ u
                j = int(np.argmin(dists))
                if float(dists[j]) <= self.threshold:
                    out[i] = snap_ids[j]
                    matched_updates.setdefault(j, []).append((texts[i], u))
                    continue
            unmatched.append(k)

        # Update matched centroids only after every assignment decision has
        # been made against the frozen snapshot. Canonical text order makes
        # the resulting registry independent of caller batch order too.
        for j in sorted(matched_updates):
            for _, u in sorted(matched_updates[j], key=lambda item: item[0]):
                self.registry._update(j, u, freeze_after=self.freeze_after)

        # Phase 2: mint new clusters for unmatched texts in canonical order.
        for k in sorted(unmatched, key=lambda k: texts[usable_idx[k]]):
            cid, _ = self.registry.assign(
                vecs[k], threshold=self.threshold, freeze_after=self.freeze_after,
            )
            out[usable_idx[k]] = cid
        return out

    # convenience persistence passthroughs
    def save(self, path: str | Path) -> None:
        self.registry.save(path)

    @classmethod
    def load(
        cls, path: str | Path, *, embedder: object, **kw,
    ) -> StableIntentClusterer:
        return cls(embedder=embedder, registry=ClusterRegistry.load(path), **kw)
