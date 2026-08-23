"""Frozen pre-view Task 5 quality gate; run directly, never during unit tests."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score
from verdict.redaction import redact
from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering import FrozenMiniLMEmbedder, SentenceTransformerEmbedder
from verdict_eval.clustering_strategies import FitConfig
from verdict_eval.stable_clustering import (
    UNCLUSTERED_ID,
    ClusterRegistry,
    StableIntentClusterer,
)

SOURCE_SHA256 = "d12d6e3bc4c3103966ae786dc435913c0c563dfa328f5a3646d0e62cfeeb474d"
DEVELOPMENT_MANIFEST_SHA256 = "5099fac8c40cc67072799f019ceecd36fdbb08b15173df8f724ba717ff88694e"
UNSEEN_MANIFEST_SHA256 = "2c16e6d3f60f8a479ede71700cb781bc36e8690db958357e437934f6ac745c12"
MODEL_AGGREGATE_SHA256 = "af9f3a2c9b7056efdc59ccf96399be15bd0c52f153b4f8007e757b287b5ebc31"
VIEWED_CATEGORIES = frozenset(
    {
        "verify_my_identity",
        "compromised_card",
        "country_support",
        "age_limit",
        "top_up_by_bank_transfer_charge",
        "verify_source_of_funds",
        "exchange_rate",
        "pending_transfer",
        "supported_cards_and_currencies",
        "transfer_not_received_by_recipient",
    }
)
OUTLIER = "__outlier__"
BOOTSTRAP_REPLICATES = 2_000
BOOTSTRAP_SEED = 50_506
SHUFFLE_SEED = 50_507
EXPECTED_CHECKS = frozenset(
    {
        "ari_improvement_point",
        "ari_improvement_interval",
        "ari_lower",
        "nmi_lower",
        "purity_lower",
        "outlier_upper",
        "useful_clusters",
        "fragmentation",
        "terminal_minimum",
        "dominant_share",
        "order_stability",
        "deletion_median",
        "deletion_minimum",
    }
)


class GateFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class Row:
    source_row: int
    query: str
    truth: str


class _PreflightEmbedder:
    dim = 2
    model_name = "task5-preflight"
    model_revision = "v1"
    model_file_sha256 = "synthetic"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float64)


class _ObservedClusterRegistryService(ClusterRegistryService):
    """Capture the exact candidate/input objects consumed by one scored fit."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.observed_candidates = None
        self.observed_inputs = None

    def _candidates(self, *args, **kwargs):
        rows = super()._candidates(*args, **kwargs)
        if kwargs.get("missing_version_id") is None:
            if self.observed_candidates is not None:
                raise GateFailure("scored fit read its candidate population more than once")
            self.observed_candidates = list(rows)
        return rows

    def _inputs(self, *args, **kwargs):
        inputs = super()._inputs(*args, **kwargs)
        if kwargs.get("evidence_only") is True:
            if self.observed_inputs is not None:
                raise GateFailure("scored fit selected its semantic inputs more than once")
            self.observed_inputs = list(inputs)
        return inputs


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_rows(path: Path) -> list[dict[str, str]]:
    lines = [line for line in path.read_text().splitlines() if not line.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


def _load_holdout(
    source: Path,
    development_manifest: Path,
    unseen_manifest: Path,
) -> list[Row]:
    if _sha256(source) != SOURCE_SHA256:
        raise GateFailure("Banking77 source digest mismatch")
    if _sha256(development_manifest) != DEVELOPMENT_MANIFEST_SHA256:
        raise GateFailure("development manifest digest mismatch")
    if _sha256(unseen_manifest) != UNSEEN_MANIFEST_SHA256:
        raise GateFailure("unseen manifest digest mismatch")
    development = {row["truth_intent"] for row in _manifest_rows(development_manifest)}
    with source.open(newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    categories = {row["category"] for row in source_rows} - development - VIEWED_CATEGORIES
    ranked = tuple(
        sorted(
            categories,
            key=lambda value: hashlib.sha256(
                b"verdict-task5-unseen-v2\0" + value.encode("utf-8")
            ).digest(),
        )[:10]
    )
    manifest = _manifest_rows(unseen_manifest)
    if [row["category"] for row in manifest] != list(ranked):
        raise GateFailure("unseen category selection mismatch")
    for index, row in enumerate(manifest, 1):
        category = row["category"]
        expected_digest = hashlib.sha256(
            b"verdict-task5-unseen-v2\0" + category.encode("utf-8")
        ).hexdigest()
        if (
            row.get("rank") != str(index)
            or row.get("category_rank_sha256") != expected_digest
            or row.get("row_count") != "40"
        ):
            raise GateFailure("unseen manifest row mismatch")
    selected = [
        Row(index, row["text"], row["category"])
        for index, row in enumerate(source_rows, start=1)
        if row["category"] in ranked
    ]
    if len(selected) != 400 or Counter(row.truth for row in selected) != {
        category: 40 for category in ranked
    }:
        raise GateFailure("unseen row counts mismatch")
    return selected


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def _rank(tenant: str, trace_id: str) -> bytes:
    parts = (b"cluster-fit-rank-v1", tenant.encode(), trace_id.encode("utf-8"))
    return hashlib.sha256(b"".join(len(part).to_bytes(4, "big") + part for part in parts)).digest()


def _assert_observed_fit(
    service: _ObservedClusterRegistryService,
    *,
    expected_order: list[str],
    expected_text: dict[str, str],
) -> None:
    if service.observed_candidates is None or service.observed_inputs is None:
        raise GateFailure("scored fit bypassed the production candidate/input path")
    if [row.trace_id for row in service.observed_candidates] != expected_order:
        raise GateFailure("production candidate rank bypassed")
    if [(item.trace_id, item.semantic_text) for item in service.observed_inputs] != [
        (trace_id, expected_text[trace_id]) for trace_id in expected_order
    ]:
        raise GateFailure("production latest-user selector bypassed")


def _baseline_clusterer(embedder: object) -> StableIntentClusterer:
    return StableIntentClusterer(
        embedder=embedder,
        threshold=0.50,
        freeze_after=200,
        min_chars=3,
        registry=ClusterRegistry(version="v2"),
    )


def _synthetic_product_preflight() -> None:
    """Exercise rank, multi-message selection, and baseline identity pre-view."""
    tenant = "__task5_preflight__"
    cutoff = datetime(2026, 7, 2, tzinfo=timezone.utc)
    storage = InMemoryStorage()
    expected_text: dict[str, str] = {}
    trace_ids = ["preflight-e", "preflight-a", "preflight-d", "preflight-b", "preflight-c"]
    for index, trace_id in enumerate(trace_ids, start=1):
        latest = f"latest request {index}"
        detail = f"detail {index}"
        expected_text[trace_id] = f"{latest} {detail}"
        storage.insert_trace(
            Trace(
                trace_id=trace_id,
                tenant_id=tenant,
                started_at=cutoff - timedelta(days=1) + timedelta(microseconds=index),
                ended_at=cutoff - timedelta(microseconds=1),
                raw_messages=[
                    {"role": "system", "content": "stable system context"},
                    {"role": "user", "content": f"stale request {index}"},
                    {"role": "assistant", "content": "stale answer"},
                    {"role": "tool", "content": "tool output"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": latest},
                            {"type": "image", "source": "ignored"},
                            {"type": "text", "text": detail},
                        ],
                    },
                ],
                tags={"verdict.workload": "agent"},
            )
        )
    service = _ObservedClusterRegistryService(storage, embedder=_PreflightEmbedder())
    version = service.fit(
        tenant,
        actor="task5-quality-gate",
        strategy="semantic",
        cutoff=cutoff,
        config=FitConfig(
            strategy="semantic",
            target_workload="agent",
            min_cluster_size=5,
        ),
    )
    expected_order = sorted(
        trace_ids,
        key=lambda trace_id: (_rank(tenant, trace_id), trace_id.encode("utf-8")),
    )
    _assert_observed_fit(
        service,
        expected_order=expected_order,
        expected_text=expected_text,
    )
    definition = json.loads(version.fit_definition_json)
    if definition.get("algorithm") != "ward-best-k-v2":
        raise GateFailure("scored fit did not use frozen ward-best-k-v2")
    baseline = _baseline_clusterer(_PreflightEmbedder())
    labels = baseline.assign([expected_text[trace_id] for trace_id in trace_ids])
    if not labels or any(not label.startswith("v2-") for label in labels):
        raise GateFailure("shipped baseline did not use the frozen empty v2 registry")


def _candidate_predictions(
    rows: list[Row],
    *,
    tenant: str,
    cutoff: datetime,
    embedder: FrozenMiniLMEmbedder,
) -> tuple[list[str], dict[str, int | float]]:
    storage = InMemoryStorage()
    for row in rows:
        storage.insert_trace(
            Trace(
                trace_id=f"heldout-{row.source_row:05d}",
                tenant_id=tenant,
                started_at=cutoff - timedelta(days=1) + timedelta(microseconds=row.source_row),
                ended_at=cutoff - timedelta(microseconds=1),
                raw_messages=[{"role": "user", "content": row.query}],
                tags={"verdict.workload": "agent"},
            )
        )
    service = _ObservedClusterRegistryService(storage, embedder=embedder)
    config = FitConfig(strategy="semantic", target_workload="agent")
    expected_order = sorted(
        (f"heldout-{row.source_row:05d}" for row in rows),
        key=lambda trace_id: (_rank(tenant, trace_id), trace_id.encode("utf-8")),
    )
    query_for_trace = {
        f"heldout-{row.source_row:05d}": redact(_normalize(row.query), mode="redact")
        for row in rows
    }
    version = service.fit(
        tenant,
        actor="task5-quality-gate",
        strategy="semantic",
        cutoff=cutoff,
        config=config,
    )
    _assert_observed_fit(
        service,
        expected_order=expected_order,
        expected_text=query_for_trace,
    )
    assignments = {
        item.trace_id: item
        for item in storage.list_trace_cluster_assignments(tenant, version.version_id)
    }
    labels = []
    for row in rows:
        assignment = assignments[f"heldout-{row.source_row:05d}"]
        labels.append(assignment.cluster_id if assignment.status == "assigned" else OUTLIER)
    clusters = len({label for label in labels if label != OUTLIER})
    counts = Counter(label for label in labels if label != OUTLIER)
    assigned = sum(counts.values())
    return labels, {
        "clusters": clusters,
        "terminal_minimum": min(counts.values(), default=0),
        "dominant_share": max(counts.values(), default=0) / assigned if assigned else 1.0,
    }


def _baseline_predictions(rows: list[Row], model_path: Path) -> list[str]:
    clusterer = _baseline_clusterer(SentenceTransformerEmbedder(str(model_path)))
    labels = clusterer.assign([row.query for row in rows])
    if any(label != UNCLUSTERED_ID and not label.startswith("v2-") for label in labels):
        raise GateFailure("shipped baseline did not use the frozen empty v2 registry")
    return [OUTLIER if label == UNCLUSTERED_ID else label for label in labels]


def _purity(truth: list[str], predicted: list[str]) -> float:
    total = 0
    for label in set(predicted):
        counts = Counter(t for t, p in zip(truth, predicted, strict=True) if p == label)
        total += max(counts.values())
    return total / len(truth)


def _metrics(truth: list[str], predicted: list[str]) -> dict[str, float]:
    return {
        "ari": float(adjusted_rand_score(truth, predicted)),
        "nmi": float(normalized_mutual_info_score(truth, predicted, average_method="arithmetic")),
        "purity": _purity(truth, predicted),
        "outlier_rate": predicted.count(OUTLIER) / len(predicted),
    }


def _same_partition(left: list[str], right: list[str]) -> bool:
    if len(left) != len(right):
        return False
    if [value == OUTLIER for value in left] != [value == OUTLIER for value in right]:
        return False
    return float(adjusted_rand_score(left, right)) == 1.0


def _stability(
    rows: list[Row],
    full: list[str],
    *,
    tenant: str,
    cutoff: datetime,
    embedder: FrozenMiniLMEmbedder,
) -> dict[str, object]:
    full_by_row = dict(zip((row.source_row for row in rows), full, strict=True))
    variants = [list(reversed(rows))]
    rng = np.random.Generator(np.random.PCG64(SHUFFLE_SEED))
    for _ in range(2):
        order = rng.permutation(len(rows))
        variants.append([rows[int(index)] for index in order])
    exact = True
    for variant in variants:
        predicted, _summary = _candidate_predictions(
            variant, tenant=tenant, cutoff=cutoff, embedder=embedder
        )
        aligned = {row.source_row: label for row, label in zip(variant, predicted, strict=True)}
        exact &= _same_partition(
            full,
            [aligned[row.source_row] for row in rows],
        )

    ranked = sorted(
        rows,
        key=lambda row: (
            _rank(tenant, f"heldout-{row.source_row:05d}"),
            row.source_row,
        ),
    )
    fold_by_row = {
        row.source_row: min(9, index * 10 // len(ranked)) for index, row in enumerate(ranked)
    }
    deletion_ari: list[float] = []
    for fold in range(10):
        retained = [row for row in rows if fold_by_row[row.source_row] != fold]
        predicted, _summary = _candidate_predictions(
            retained, tenant=tenant, cutoff=cutoff, embedder=embedder
        )
        deletion_ari.append(
            float(
                adjusted_rand_score(
                    [full_by_row[row.source_row] for row in retained],
                    predicted,
                )
            )
        )
    return {
        "exact_partition": exact,
        "deletion_ari": deletion_ari,
        "deletion_median": float(np.median(deletion_ari)),
        "deletion_minimum": min(deletion_ari),
    }


def _groups(rows: list[Row]) -> list[list[int]]:
    tokens = []
    for row in rows:
        normalized = _normalize(row.query)
        tokens.append(
            {normalized[index : index + 5] for index in range(len(normalized) - 4)}
            if len(normalized) >= 5
            else {normalized}
        )
    parents = list(range(len(rows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            intersection = len(tokens[left] & tokens[right])
            union_size = len(tokens[left] | tokens[right])
            if union_size and intersection / union_size >= 0.90:
                union(left, right)
    grouped: dict[int, list[int]] = {}
    for index in range(len(rows)):
        grouped.setdefault(find(index), []).append(index)
    return sorted(grouped.values(), key=lambda group: rows[min(group)].source_row)


def _intervals(
    rows: list[Row], candidate: list[str], baseline: list[str]
) -> tuple[int, dict[str, list[float]]]:
    truth = [row.truth for row in rows]
    groups = _groups(rows)
    rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_SEED))
    values = {name: [] for name in ("ari", "nmi", "purity", "outlier_rate")}
    values["ari_improvement"] = []
    for _ in range(BOOTSTRAP_REPLICATES):
        chosen = rng.integers(0, len(groups), size=len(groups))
        indices = [index for group_index in chosen for index in groups[group_index]]
        sampled_truth = [truth[index] for index in indices]
        candidate_metrics = _metrics(sampled_truth, [candidate[index] for index in indices])
        baseline_metrics = _metrics(sampled_truth, [baseline[index] for index in indices])
        for name in ("ari", "nmi", "purity", "outlier_rate"):
            values[name].append(candidate_metrics[name])
        values["ari_improvement"].append(candidate_metrics["ari"] - baseline_metrics["ari"])
    return len(groups), {
        name: [
            float(np.quantile(series, 0.025, method="linear")),
            float(np.quantile(series, 0.975, method="linear")),
        ]
        for name, series in values.items()
    }


def _finite_number(value: object, name: str) -> float:
    if type(value) not in {int, float} or not np.isfinite(value):
        raise GateFailure(f"{name} must be a finite number")
    return float(value)


def _finalize_checks(checks: dict[str, bool]) -> bool:
    if set(checks) != EXPECTED_CHECKS or any(type(value) is not bool for value in checks.values()):
        raise GateFailure("acceptance check schema mismatch")
    return all(checks.values())


def _acceptance(
    candidate_point: dict[str, float],
    baseline_point: dict[str, float],
    intervals: dict[str, list[float]],
    *,
    candidate_summary: dict[str, int | float],
    stability: dict[str, object],
    truth_intents: int,
) -> tuple[float, float, dict[str, bool], bool]:
    metric_names = {"ari", "nmi", "purity", "outlier_rate"}
    interval_names = metric_names | {"ari_improvement"}
    if set(candidate_point) != metric_names or set(baseline_point) != metric_names:
        raise GateFailure("point-estimate schema mismatch")
    if set(intervals) != interval_names:
        raise GateFailure("interval schema mismatch")
    candidate = {
        name: _finite_number(candidate_point[name], f"candidate.{name}") for name in metric_names
    }
    baseline = {
        name: _finite_number(baseline_point[name], f"baseline.{name}") for name in metric_names
    }
    bounds: dict[str, tuple[float, float]] = {}
    for name in interval_names:
        value = intervals[name]
        if type(value) is not list or len(value) != 2:
            raise GateFailure(f"intervals.{name} must contain exactly two endpoints")
        lower = _finite_number(value[0], f"intervals.{name}.lower")
        upper = _finite_number(value[1], f"intervals.{name}.upper")
        if lower > upper:
            raise GateFailure(f"intervals.{name} endpoints are reversed")
        bounds[name] = (lower, upper)
    if set(candidate_summary) != {"clusters", "terminal_minimum", "dominant_share"}:
        raise GateFailure("candidate summary schema mismatch")
    clusters = candidate_summary["clusters"]
    terminal_minimum = candidate_summary["terminal_minimum"]
    if type(clusters) is not int or type(terminal_minimum) is not int:
        raise GateFailure("candidate count values must be integers")
    dominant_share = _finite_number(candidate_summary["dominant_share"], "candidate.dominant_share")
    if clusters < 0 or terminal_minimum < 0 or not 0.0 <= dominant_share <= 1.0:
        raise GateFailure("candidate summary values are out of range")
    if type(truth_intents) is not int or truth_intents <= 0:
        raise GateFailure("truth_intents must be a positive integer")
    if set(stability) != {
        "exact_partition",
        "deletion_ari",
        "deletion_median",
        "deletion_minimum",
    }:
        raise GateFailure("stability schema mismatch")
    if type(stability["exact_partition"]) is not bool:
        raise GateFailure("stability.exact_partition must be a boolean")
    deletion = stability["deletion_ari"]
    if type(deletion) is not list or len(deletion) != 10:
        raise GateFailure("stability.deletion_ari must contain ten values")
    deletion_values = [
        _finite_number(value, f"stability.deletion_ari.{index}")
        for index, value in enumerate(deletion)
    ]
    deletion_median = _finite_number(stability["deletion_median"], "stability.deletion_median")
    deletion_minimum = _finite_number(stability["deletion_minimum"], "stability.deletion_minimum")
    if deletion_median != float(np.median(deletion_values)) or deletion_minimum != min(
        deletion_values
    ):
        raise GateFailure("stability summary does not match deletion values")
    improvement = candidate["ari"] - baseline["ari"]
    fragmentation = clusters / truth_intents
    checks = {
        "ari_improvement_point": improvement > 0.0,
        "ari_improvement_interval": bounds["ari_improvement"][0] > 0.0,
        "ari_lower": bounds["ari"][0] >= 0.50,
        "nmi_lower": bounds["nmi"][0] >= 0.70,
        "purity_lower": bounds["purity"][0] >= 0.75,
        "outlier_upper": bounds["outlier_rate"][1] <= 0.20,
        "useful_clusters": 5 <= clusters <= 15,
        "fragmentation": 0.5 <= fragmentation <= 1.5,
        "terminal_minimum": terminal_minimum >= 5,
        "dominant_share": dominant_share <= 0.30,
        "order_stability": stability["exact_partition"],
        "deletion_median": deletion_median >= 0.80,
        "deletion_minimum": deletion_minimum >= 0.65,
    }
    return improvement, fragmentation, checks, _finalize_checks(checks)


def run(args: argparse.Namespace) -> dict[str, object]:
    _synthetic_product_preflight()
    embedder = FrozenMiniLMEmbedder(args.model_path)
    if embedder.model_file_sha256 != MODEL_AGGREGATE_SHA256:
        raise GateFailure("model aggregate mismatch")
    rows = _load_holdout(
        args.source,
        args.development_manifest,
        args.unseen_manifest,
    )
    cutoff = datetime(2026, 7, 2, tzinfo=timezone.utc)
    tenant = "__task5_unseen__"
    candidate, candidate_summary = _candidate_predictions(
        rows, tenant=tenant, cutoff=cutoff, embedder=embedder
    )
    baseline = _baseline_predictions(rows, args.model_path)
    truth = [row.truth for row in rows]
    candidate_point = _metrics(truth, candidate)
    baseline_point = _metrics(truth, baseline)
    group_count, intervals = _intervals(rows, candidate, baseline)
    stability = _stability(
        rows,
        candidate,
        tenant=tenant,
        cutoff=cutoff,
        embedder=embedder,
    )
    improvement, fragmentation, checks, accepted = _acceptance(
        candidate_point,
        baseline_point,
        intervals,
        candidate_summary=candidate_summary,
        stability=stability,
        truth_intents=len(set(truth)),
    )
    return {
        "schema": "task5-quality-gate-v2",
        "environment": {"python": platform.python_version(), "machine": platform.machine()},
        "sample_size": len(rows),
        "group_count": group_count,
        "bootstrap": {
            "replicates": BOOTSTRAP_REPLICATES,
            "seed": BOOTSTRAP_SEED,
            "method": "whole-group with replacement; central 95%; linear quantile",
        },
        "missing_data_policy": "all rows; shared __outlier__ label",
        "candidate": {
            **candidate_summary,
            "fragmentation": fragmentation,
            "point": candidate_point,
        },
        "baseline": {"point": baseline_point},
        "ari_improvement": improvement,
        "intervals": intervals,
        "stability": stability,
        "checks": checks,
        "accepted": accepted,
        "failed_folds": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--development-manifest", type=Path, required=True)
    parser.add_argument("--unseen-manifest", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run(args)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload, end="")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
