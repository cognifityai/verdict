"""Versioned clustering strategies with no storage or provider I/O."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import numpy as np
from scipy.cluster.hierarchy import cut_tree, linkage
from sklearn.metrics import silhouette_score
from verdict.redaction import redact
from verdict.schema import Trace


class AssignmentStatus(str, Enum):
    ASSIGNED = "assigned"
    OUTLIER = "outlier"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class InputSelection:
    text: str | None
    reason: str | None = None


@dataclass(frozen=True)
class ClusterInput:
    trace_id: str
    tenant_id: str
    started_at_us: int
    explicit_key: str | None = None
    semantic_text: str | None = None
    ineligible_reason: str | None = None
    member_weight: int = 1


@dataclass(frozen=True)
class FitConfig:
    strategy: str = "semantic"
    lookback_days: int = 90
    max_fit_candidates: int = 50_000
    max_fit_candidate_metadata_bytes: int = 33_554_432
    max_fit_content_scan_bytes: int = 67_108_864
    max_semantic_fit_inputs: int = 5_000
    min_cluster_size: int = 5
    max_explicit_clusters: int = 200
    max_semantic_clusters: int = 50
    max_explicit_identities_per_tenant: int = 10_000
    max_semantic_identities_per_tenant: int = 5_000
    target_workload: str | None = None
    silhouette_tolerance: float = 0.01
    radius_percentile: float = 95.0
    radius_margin: float = 0.02
    max_assignment_distance: float = 0.45
    carryover_distance: float = 0.15
    carryover_ambiguity_margin: float = 0.02

    def __post_init__(self) -> None:
        if self.strategy not in {"explicit", "semantic", "hybrid"}:
            raise ValueError("strategy must be explicit, semantic, or hybrid")
        positive_ints = (
            self.lookback_days,
            self.max_fit_candidates,
            self.max_fit_candidate_metadata_bytes,
            self.max_fit_content_scan_bytes,
            self.max_semantic_fit_inputs,
            self.min_cluster_size,
            self.max_explicit_clusters,
            self.max_semantic_clusters,
            self.max_explicit_identities_per_tenant,
            self.max_semantic_identities_per_tenant,
        )
        if any(type(value) is not int or value <= 0 for value in positive_ints):
            raise ValueError("fit count and byte limits must be positive integers")
        ceilings = {
            "max_fit_candidates": 50_000,
            "max_fit_candidate_metadata_bytes": 33_554_432,
            "max_fit_content_scan_bytes": 67_108_864,
            "max_semantic_fit_inputs": 5_000,
            "max_explicit_clusters": 200,
            "max_semantic_clusters": 50,
            "max_explicit_identities_per_tenant": 10_000,
            "max_semantic_identities_per_tenant": 5_000,
        }
        if any(getattr(self, name) > maximum for name, maximum in ceilings.items()):
            raise ValueError("fit limits cannot exceed the alpha hard maximum")
        if self.target_workload is not None:
            if (
                not isinstance(self.target_workload, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", self.target_workload) is None
                or self.target_workload in {"judge", "paired_replay"}
                or redact(self.target_workload, mode="redact") != self.target_workload
            ):
                raise ValueError("target workload is invalid, unsafe, or internal")
        if not 0.0 <= self.radius_percentile <= 100.0:
            raise ValueError("radius_percentile must be in [0,100]")
        for value in (
            self.silhouette_tolerance,
            self.radius_margin,
            self.max_assignment_distance,
            self.carryover_distance,
            self.carryover_ambiguity_margin,
        ):
            if not math.isfinite(value) or not 0 <= value <= 2:
                raise ValueError("fit distance values must be finite and in [0,2]")


@dataclass(frozen=True)
class ClusterDefinition:
    cluster_id: str
    kind: str
    display_name: str
    explicit_key: str | None = None
    centroid: tuple[float, ...] | None = None
    radius: float | None = None
    member_count: int = 0
    outlier_count: int = 0


@dataclass(frozen=True)
class AssignmentResult:
    trace_id: str
    status: AssignmentStatus
    cluster_id: str | None = None
    cluster_kind: str | None = None
    reason: str | None = None
    distance: float | None = None


@dataclass(frozen=True)
class RegistryDefinition:
    strategy: str
    clusters: tuple[ClusterDefinition, ...]


@dataclass(frozen=True)
class FitResult:
    clusters: tuple[ClusterDefinition, ...]
    assignments: tuple[AssignmentResult, ...]
    chosen_k: int | None = None
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class ClusteringStrategy(Protocol):
    name: str

    def fit(self, inputs: Sequence[ClusterInput], config: FitConfig) -> FitResult: ...

    def assign(
        self,
        definition: RegistryDefinition,
        inputs: Sequence[ClusterInput],
    ) -> list[AssignmentResult]: ...


def select_cluster_input(trace: Trace) -> InputSelection:
    """Select and re-redact the latest supported user text from a trace."""
    messages = trace.raw_messages
    if messages is None:
        return InputSelection(None, "content_not_captured")
    if not isinstance(messages, list):
        return InputSelection(None, "malformed_messages")

    for message in reversed(messages):
        if not isinstance(message, dict):
            return InputSelection(None, "malformed_messages")
        role = message.get("role")
        if not isinstance(role, str):
            return InputSelection(None, "malformed_messages")
        if role not in {"user", "assistant", "system", "tool"}:
            return InputSelection(None, "malformed_messages")
        if role != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            pieces = [content]
        elif isinstance(content, list):
            pieces: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    return InputSelection(None, "malformed_messages")
                block_type = block.get("type")
                if block_type not in {"text", "input_text"}:
                    continue
                text = block.get("text")
                if not isinstance(text, str):
                    return InputSelection(None, "malformed_messages")
                pieces.append(text)
        else:
            return InputSelection(None, "malformed_messages")
        if not pieces:
            return InputSelection(None, "no_supported_user_text")
        normalized = unicodedata.normalize("NFC", "\n".join(pieces))
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) < 3:
            return InputSelection(None, "text_too_short")
        if len(normalized) > 4_096:
            return InputSelection(None, "text_too_long")
        try:
            selected = redact(normalized, mode="redact")
        except Exception:
            return InputSelection(None, "redaction_error")
        if not isinstance(selected, str):
            return InputSelection(None, "redaction_error")
        return InputSelection(selected)
    return InputSelection(None, "no_supported_user_text")


def _provisional_id(kind: str, value: bytes) -> str:
    return f"{kind}:{hashlib.sha256(value).hexdigest()[:20]}"


def _semantic_display_name(text: str | None, fallback_index: int) -> str:
    """Return a bounded redacted representative title for a semantic cluster."""
    cleaned = "".join(
        character
        for character in unicodedata.normalize("NFC", text or "")
        if not unicodedata.category(character).startswith("C")
    )
    cleaned = re.sub(r"<[^>]{1,120}>", " ", cleaned)
    cleaned = re.sub(r"[`#*_]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:;,.\t\r\n")
    if not cleaned:
        return f"Semantic cluster {fallback_index + 1}"
    words = cleaned.split()
    title = ""
    for word in words:
        candidate = f"{title} {word}".strip()
        if len(candidate) > 72 or len(candidate.encode("utf-8")) > 220:
            break
        title = candidate
        if len(title) >= 48:
            break
    return title or f"Semantic cluster {fallback_index + 1}"


class ExplicitClusteringStrategy:
    name = "explicit"

    def fit(self, inputs: Sequence[ClusterInput], config: FitConfig) -> FitResult:
        keys = sorted(
            {
                item.explicit_key
                for item in inputs
                if item.ineligible_reason is None and item.explicit_key is not None
            }
        )
        if len(keys) > config.max_explicit_clusters:
            raise ValueError("explicit_cluster_limit")
        ids = {key: _provisional_id("explicit", key.encode("ascii")) for key in keys}
        counts = {key: 0 for key in keys}
        assignments: list[AssignmentResult] = []
        for item in inputs:
            if item.ineligible_reason is not None or item.explicit_key is None:
                assignments.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.INELIGIBLE,
                        reason=item.ineligible_reason or "missing_intent_key",
                    )
                )
                continue
            counts[item.explicit_key] += item.member_weight
            assignments.append(
                AssignmentResult(
                    item.trace_id,
                    AssignmentStatus.ASSIGNED,
                    cluster_id=ids[item.explicit_key],
                    cluster_kind="explicit",
                )
            )
        clusters = tuple(
            ClusterDefinition(
                ids[key],
                "explicit",
                key,
                explicit_key=key,
                member_count=counts[key],
            )
            for key in keys
        )
        return FitResult(clusters, tuple(assignments))

    def assign(
        self,
        definition: RegistryDefinition,
        inputs: Sequence[ClusterInput],
    ) -> list[AssignmentResult]:
        by_key = {
            cluster.explicit_key: cluster
            for cluster in definition.clusters
            if cluster.kind == "explicit" and cluster.explicit_key is not None
        }
        out: list[AssignmentResult] = []
        for item in inputs:
            if item.ineligible_reason is not None or item.explicit_key is None:
                out.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.INELIGIBLE,
                        reason=item.ineligible_reason or "missing_intent_key",
                    )
                )
            elif (cluster := by_key.get(item.explicit_key)) is None:
                out.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.OUTLIER,
                        reason="explicit_key_not_in_version",
                    )
                )
            else:
                out.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.ASSIGNED,
                        cluster.cluster_id,
                        "explicit",
                    )
                )
        return out


def _unit(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("semantic_zero_norm")
    result = vector / norm
    if not np.all(np.isfinite(result)):
        raise ValueError("semantic_non_finite")
    return result


def _cosine_distances(dots: np.ndarray) -> np.ndarray:
    """Return finite cosine distances in the persisted SQL domain."""
    distances = 1.0 - np.asarray(dots, dtype=np.float64)
    if not np.all(np.isfinite(distances)):
        raise ValueError("semantic_non_finite_distance")
    return np.clip(distances, 0.0, 2.0)


class SemanticClusteringStrategy:
    name = "semantic"

    def __init__(self, embedder: object) -> None:
        self._embedder = embedder

    def _embed(self, texts: Sequence[str]) -> np.ndarray:
        rows = []
        for text in texts:
            encoded = np.asarray(self._embedder.embed([text]))
            if encoded.ndim != 2 or encoded.shape[0] != 1:
                raise ValueError("semantic_embedding_shape")
            rows.append(_unit(encoded[0]))
        return np.asarray(rows, dtype=np.float64)

    @staticmethod
    def _prototypes(
        inputs: Sequence[ClusterInput],
        vectors: np.ndarray,
        labels: np.ndarray,
        config: FitConfig,
    ) -> tuple[ClusterDefinition, ...]:
        provisional: list[tuple[int, np.ndarray, float, str | None]] = []
        for label in sorted(set(int(value) for value in labels)):
            indices = np.flatnonzero(labels == label)
            centroid = _unit(np.mean(vectors[indices], axis=0))
            distances = _cosine_distances(vectors[indices] @ centroid)
            radius = min(
                config.max_assignment_distance,
                max(
                    0.0,
                    float(np.percentile(distances, config.radius_percentile, method="linear"))
                    + config.radius_margin,
                ),
            )
            first = int(indices[0])
            representative = int(indices[int(np.argmin(distances))])
            provisional.append(
                (first, centroid, radius, inputs[representative].semantic_text)
            )
        provisional.sort(key=lambda row: (row[0], row[1].tobytes()))
        return tuple(
            ClusterDefinition(
                f"semantic:{index:04d}",
                "semantic",
                _semantic_display_name(representative_text, index),
                centroid=tuple(float(value) for value in centroid),
                radius=radius,
            )
            for index, (_first_rank, centroid, radius, representative_text) in enumerate(
                provisional
            )
        )

    @staticmethod
    def _assign_embedded(
        clusters: Sequence[ClusterDefinition],
        inputs: Sequence[ClusterInput],
        vectors: np.ndarray,
    ) -> list[AssignmentResult]:
        ordered = sorted(clusters, key=lambda cluster: cluster.cluster_id)
        if not ordered:
            return [
                AssignmentResult(
                    item.trace_id,
                    AssignmentStatus.OUTLIER,
                    reason="semantic_fit_too_small",
                )
                for item in inputs
            ]
        centroids = np.asarray([cluster.centroid for cluster in ordered], dtype=np.float64)
        out: list[AssignmentResult] = []
        for item, vector in zip(inputs, vectors, strict=True):
            distances = _cosine_distances(centroids @ vector)
            nearest = int(np.argmin(distances))
            distance = float(distances[nearest])
            cluster = ordered[nearest]
            if cluster.radius is None or distance > cluster.radius:
                out.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.OUTLIER,
                        reason="distance",
                        distance=distance,
                    )
                )
            else:
                out.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.ASSIGNED,
                        cluster.cluster_id,
                        "semantic",
                        distance=distance,
                    )
                )
        return out

    def fit(self, inputs: Sequence[ClusterInput], config: FitConfig) -> FitResult:
        usable = [
            item
            for item in inputs
            if item.ineligible_reason is None and item.semantic_text is not None
        ]
        unavailable = [item for item in inputs if item not in usable]
        if len(usable) < config.min_cluster_size:
            results = [
                AssignmentResult(
                    item.trace_id,
                    AssignmentStatus.OUTLIER,
                    reason="semantic_fit_too_small",
                )
                for item in usable
            ]
            results.extend(
                AssignmentResult(
                    item.trace_id,
                    AssignmentStatus.INELIGIBLE,
                    reason=item.ineligible_reason or "content_not_captured",
                )
                for item in unavailable
            )
            return FitResult(
                (), tuple(results), chosen_k=None, warnings=("semantic_fit_too_small",)
            )

        vectors = self._embed([item.semantic_text or "" for item in usable])
        chosen: (
            tuple[
                float,
                int,
                tuple[ClusterDefinition, ...],
                list[AssignmentResult],
            ]
            | None
        ) = None
        chosen_score: float | None = None
        maximum = min(config.max_semantic_clusters, len(usable) // config.min_cluster_size)
        if maximum >= 2:
            hierarchy = linkage(vectors, method="ward", optimal_ordering=True)
            valid: list[
                tuple[
                    float,
                    int,
                    tuple[ClusterDefinition, ...],
                    list[AssignmentResult],
                ]
            ] = []
            for k in range(2, maximum + 1):
                candidate = np.asarray(cut_tree(hierarchy, n_clusters=[k])).reshape(-1)
                counts = np.bincount(candidate)
                if len(counts) != k or np.any(counts < config.min_cluster_size):
                    continue
                clusters = self._prototypes(usable, vectors, candidate, config)
                assignments = self._assign_embedded(clusters, usable, vectors)
                terminal_counts = {
                    cluster.cluster_id: sum(
                        item.status is AssignmentStatus.ASSIGNED
                        and item.cluster_id == cluster.cluster_id
                        for item in assignments
                    )
                    for cluster in clusters
                }
                if any(count < config.min_cluster_size for count in terminal_counts.values()):
                    continue
                score = float(silhouette_score(vectors, candidate, metric="cosine"))
                if math.isfinite(score):
                    valid.append((score, k, clusters, assignments))
            if valid:
                best_score = max(score for score, _, _, _ in valid)
                eligible = [
                    row for row in valid if best_score - row[0] <= config.silhouette_tolerance
                ]
                chosen = min(eligible, key=lambda row: row[1])

        if chosen is None:
            labels = np.zeros(len(usable), dtype=int)
            clusters = self._prototypes(usable, vectors, labels, config)
            assignments = self._assign_embedded(clusters, usable, vectors)
            assigned = sum(item.status is AssignmentStatus.ASSIGNED for item in assignments)
            if assigned < config.min_cluster_size:
                clusters = ()
                assignments = self._assign_embedded(clusters, usable, vectors)
            chosen_k = 1
        else:
            chosen_score, chosen_k, clusters, assignments = chosen

        counts = {cluster.cluster_id: 0 for cluster in clusters}
        outliers = {cluster.cluster_id: 0 for cluster in clusters}
        for assignment in assignments:
            if assignment.cluster_id is not None:
                counts[assignment.cluster_id] += 1
        clusters = tuple(
            ClusterDefinition(
                cluster.cluster_id,
                cluster.kind,
                cluster.display_name,
                centroid=cluster.centroid,
                radius=cluster.radius,
                member_count=counts[cluster.cluster_id],
                outlier_count=outliers[cluster.cluster_id],
            )
            for cluster in clusters
        )
        assignments.extend(
            AssignmentResult(
                item.trace_id,
                AssignmentStatus.INELIGIBLE,
                reason=item.ineligible_reason or "content_not_captured",
            )
            for item in unavailable
        )
        assignments.sort(key=lambda item: item.trace_id)
        return FitResult(
            clusters,
            tuple(assignments),
            chosen_k=chosen_k,
            metrics={"silhouette": chosen_score},
        )

    def assign(
        self,
        definition: RegistryDefinition,
        inputs: Sequence[ClusterInput],
    ) -> list[AssignmentResult]:
        clusters = sorted(
            (cluster for cluster in definition.clusters if cluster.kind == "semantic"),
            key=lambda cluster: cluster.cluster_id,
        )
        if not clusters:
            return [
                AssignmentResult(
                    item.trace_id,
                    (
                        AssignmentStatus.OUTLIER
                        if item.ineligible_reason is None and item.semantic_text is not None
                        else AssignmentStatus.INELIGIBLE
                    ),
                    reason=(
                        "semantic_fit_too_small"
                        if item.ineligible_reason is None and item.semantic_text is not None
                        else item.ineligible_reason or "content_not_captured"
                    ),
                )
                for item in inputs
            ]
        out: list[AssignmentResult] = []
        usable = [
            item
            for item in inputs
            if item.ineligible_reason is None and item.semantic_text is not None
        ]
        vectors = self._embed([item.semantic_text or "" for item in usable]) if usable else []
        assigned = iter(self._assign_embedded(clusters, usable, np.asarray(vectors)))
        for item in inputs:
            if item.ineligible_reason is not None or item.semantic_text is None:
                out.append(
                    AssignmentResult(
                        item.trace_id,
                        AssignmentStatus.INELIGIBLE,
                        reason=item.ineligible_reason or "content_not_captured",
                    )
                )
                continue
            out.append(next(assigned))
        return out


class HybridClusteringStrategy:
    name = "hybrid"

    def __init__(self, embedder: object) -> None:
        self._explicit = ExplicitClusteringStrategy()
        self._semantic = SemanticClusteringStrategy(embedder)

    def fit(self, inputs: Sequence[ClusterInput], config: FitConfig) -> FitResult:
        explicit_inputs = [item for item in inputs if item.explicit_key is not None]
        fallback = [item for item in inputs if item.explicit_key is None]
        explicit = self._explicit.fit(explicit_inputs, config)
        semantic = self._semantic.fit(fallback, config)
        assignments = sorted(
            (*explicit.assignments, *semantic.assignments),
            key=lambda item: item.trace_id,
        )
        return FitResult(
            (*explicit.clusters, *semantic.clusters),
            tuple(assignments),
            semantic.chosen_k,
            semantic.metrics,
            semantic.warnings,
        )

    def assign(
        self,
        definition: RegistryDefinition,
        inputs: Sequence[ClusterInput],
    ) -> list[AssignmentResult]:
        explicit_inputs = [item for item in inputs if item.explicit_key is not None]
        fallback = [item for item in inputs if item.explicit_key is None]
        out = self._explicit.assign(definition, explicit_inputs)
        out.extend(self._semantic.assign(definition, fallback))
        return sorted(out, key=lambda item: item.trace_id)
