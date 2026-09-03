"""Tenant-authorized workflows for immutable v2 cluster registries."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any
from uuid import uuid4

import numpy as np
from scipy.optimize import linear_sum_assignment
from verdict.redaction import redact
from verdict.schema import (
    ActiveClusterRegistry,
    ClusterIdentity,
    ClusterRegistryCluster,
    ClusterRegistryEvent,
    ClusterRegistryVersion,
    ClusterTraceCandidate,
    Trace,
    TraceClusterAssignment,
    cluster_candidate_digest,
    datetime_to_utc_us,
)

from verdict_eval.clustering_strategies import (
    AssignmentResult,
    ClusterDefinition,
    ClusterInput,
    ExplicitClusteringStrategy,
    FitConfig,
    FitResult,
    HybridClusteringStrategy,
    RegistryDefinition,
    SemanticClusteringStrategy,
    select_cluster_input,
)


def clustering_strategy_status(strategy: str) -> dict[str, str | bool]:
    """Return the product status derived from the immutable strategy name."""
    if strategy == "explicit":
        return {
            "strategy": strategy,
            "experimental": False,
            "semantic_component": "none",
        }
    if strategy == "semantic":
        return {
            "strategy": strategy,
            "experimental": True,
            "semantic_component": "automatic",
        }
    if strategy == "hybrid":
        return {
            "strategy": strategy,
            "experimental": True,
            "semantic_component": "automatic_fallback",
        }
    raise ValueError("invalid clustering strategy")


_ROUTING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_REQUIRED_STORAGE_METHODS = (
    "activate_cluster_registry",
    "cluster_analysis_snapshot",
    "count_pending_analysis_rows",
    "get_active_cluster_registry",
    "get_cluster_registry_version",
    "get_cluster_trace_messages",
    "insert_cluster_preview",
    "insert_cluster_registry_event",
    "insert_trace_cluster_assignments",
    "list_cluster_identities",
    "list_cluster_registry_clusters",
    "list_cluster_registry_events",
    "cluster_trace_time_bounds",
    "list_cluster_trace_candidates",
    "list_trace_cluster_assignments",
    "normalize_cluster_trace_analysis",
    "rename_cluster_identity",
)


class UnsupportedClusteringStorageError(TypeError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _valid_tenant(tenant: str) -> None:
    try:
        valid = isinstance(tenant, str) and bool(tenant) and len(tenant.encode("utf-8")) <= 128
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError("tenant must be nonempty strict UTF-8 at most 128 bytes")


def _valid_actor(actor: str) -> None:
    try:
        valid = isinstance(actor, str) and bool(actor) and len(actor.encode("utf-8")) <= 256
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError("actor must be nonempty strict UTF-8 at most 256 bytes")


def _routing_reason(
    candidate: ClusterTraceCandidate, *, intent: bool
) -> tuple[str | None, str | None]:
    kind = candidate.intent_key_json_type if intent else candidate.workload_json_type
    value = candidate.intent_key if intent else candidate.workload
    prefix = "intent_key" if intent else "workload"
    if kind == "missing":
        return None, "missing_intent_key" if intent else None
    if kind != "string" or value is None or _ROUTING.fullmatch(value) is None:
        return None, f"invalid_{prefix}"
    if redact(value, mode="redact") != value:
        return None, f"unsafe_{prefix}"
    return value, None


class ClusterRegistryService:
    """One service boundary shared by CLI, Task 6, and the later dashboard."""

    def __init__(
        self,
        storage: object,
        *,
        embedder: object | None = None,
        embedder_factory: Callable[[], object] | None = None,
    ) -> None:
        missing = [
            name for name in _REQUIRED_STORAGE_METHODS if not callable(getattr(storage, name, None))
        ]
        if missing:
            raise UnsupportedClusteringStorageError(
                "storage lacks versioned clustering capability: " + ", ".join(missing)
            )
        self.storage = storage
        self.embedder = embedder
        self._embedder_factory = embedder_factory

    def _require_embedder(self) -> object:
        if self.embedder is None and self._embedder_factory is not None:
            self.embedder = self._embedder_factory()
        if self.embedder is None:
            raise ValueError("model_unavailable")
        return self.embedder

    def _strategy(self, name: str):
        if name == "explicit":
            return ExplicitClusteringStrategy()
        embedder = self._require_embedder()
        if name == "semantic":
            return SemanticClusteringStrategy(embedder)
        if name == "hybrid":
            return HybridClusteringStrategy(embedder)
        raise ValueError("invalid clustering strategy")

    def _definition(self, strategy: str, config: FitConfig) -> tuple[str, str, str]:
        model = None
        if strategy != "explicit":
            embedder = self._require_embedder()
            model = {
                "name": getattr(embedder, "model_name", type(embedder).__name__),
                "revision": getattr(embedder, "model_revision", "unknown"),
                "files_sha256": getattr(embedder, "model_file_sha256", "unknown"),
                "dimension": getattr(embedder, "dim", None),
            }
        runtime = {}
        if strategy != "explicit":
            for package in ("numpy", "scipy", "scikit-learn", "torch", "transformers"):
                try:
                    runtime[package] = package_version(package)
                except PackageNotFoundError:
                    runtime[package] = "missing"
        model_fingerprint = _fingerprint(_json(model)) if model is not None else ""
        definition = _json(
            {
                "schema": "fit-definition-v1",
                "strategy": strategy,
                "selector": "latest-user-v1",
                "algorithm": "ward-best-k-v2",
                "model": model,
                "model_fingerprint": model_fingerprint,
                "runtime": runtime,
                "config": asdict(config),
            }
        )
        return definition, _fingerprint(definition), model_fingerprint

    @staticmethod
    def _rank(tenant: str, trace_id: str) -> bytes:
        parts = [b"cluster-fit-rank-v1", tenant.encode(), trace_id.encode("utf-8", "surrogatepass")]
        return hashlib.sha256(
            b"".join(len(part).to_bytes(4, "big") + part for part in parts)
        ).digest()

    @staticmethod
    def _metadata_size(rows: list[ClusterTraceCandidate]) -> int:
        def body_size(value: str | None, *, surrogatepass: bool = False) -> int:
            if value is None:
                return 4
            errors = "surrogatepass" if surrogatepass else "strict"
            return 4 + len(value.encode("utf-8", errors))

        def projected_size(
            value: str | None,
            stored_size: int | None,
            *,
            surrogatepass: bool = False,
        ) -> int:
            if value is not None:
                return body_size(value, surrogatepass=surrogatepass)
            return 4 + (stored_size or 0)

        total = 0
        for row in rows:
            lengths = (
                row.trace_id_utf8_bytes,
                row.workload_utf8_bytes,
                row.intent_key_utf8_bytes,
                row.raw_messages_utf8_bytes,
            )
            if any(value is not None and value < 0 for value in lengths):
                raise ValueError("fit candidate metadata is invalid")
            if row.workload_json_type not in {
                "missing",
                "string",
                "null",
                "boolean",
                "number",
                "array",
                "object",
            } or row.intent_key_json_type not in {
                "missing",
                "string",
                "null",
                "boolean",
                "number",
                "array",
                "object",
            }:
                raise ValueError("fit candidate metadata is invalid")
            total += 8 + projected_size(row.trace_id, row.trace_id_utf8_bytes, surrogatepass=True)
            total += body_size(row.tenant_id) + 8
            total += (
                body_size(row.workload_json_type)
                + 8
                + projected_size(row.workload, row.workload_utf8_bytes)
            )
            total += (
                body_size(row.intent_key_json_type)
                + 8
                + projected_size(row.intent_key, row.intent_key_utf8_bytes)
            )
            total += body_size(row.raw_messages_state) + 8
        return total

    def _candidates(
        self,
        tenant: str,
        start: datetime,
        cutoff: datetime,
        config: FitConfig,
        *,
        limit: int | None = None,
        source: object | None = None,
        missing_version_id: str | None = None,
        bounded_batch: bool = False,
    ) -> list[ClusterTraceCandidate]:
        source = source or self.storage
        if source.count_pending_analysis_rows(tenant):
            raise ValueError("analysis_index_pending")
        maximum = limit or config.max_fit_candidates
        rows = source.list_cluster_trace_candidates(
            tenant,
            datetime_to_utc_us(start),
            datetime_to_utc_us(cutoff),
            target_workload=config.target_workload,
            limit=maximum if bounded_batch else maximum + 1,
            missing_version_id=missing_version_id,
        )
        if (not bounded_batch and len(rows) > maximum) or any(row.trace_id is None for row in rows):
            raise ValueError("fit_candidate_limit")
        if self._metadata_size(rows) > config.max_fit_candidate_metadata_bytes:
            raise ValueError("fit_candidate_limit")
        return sorted(
            rows, key=lambda row: (self._rank(tenant, row.trace_id or ""), row.trace_id or "")
        )

    def _inputs(
        self,
        tenant: str,
        rows: list[ClusterTraceCandidate],
        strategy: str,
        config: FitConfig,
        *,
        evidence_only: bool,
        source: object | None = None,
        candidate_summary: dict[str, Any] | None = None,
    ) -> list[ClusterInput]:
        source = source or self.storage
        classified: list[tuple[ClusterTraceCandidate, str | None, str | None]] = []
        keys: dict[str, tuple[ClusterTraceCandidate, int]] = {}
        semantic_rows: list[ClusterTraceCandidate] = []
        for row in rows:
            _, workload_reason = _routing_reason(row, intent=False)
            key, key_reason = _routing_reason(row, intent=True)
            if strategy == "semantic":
                reason = workload_reason
            elif strategy == "hybrid" and key_reason == "missing_intent_key":
                reason = workload_reason
            else:
                reason = workload_reason or key_reason
            classified.append((row, key, reason))
            if reason is None and key is not None:
                representative, count = keys.get(key, (row, 0))
                keys[key] = (representative, count + 1)
            if reason is None and (
                strategy == "semantic"
                or (strategy == "hybrid" and key is None and key_reason == "missing_intent_key")
            ):
                semantic_rows.append(row)
        if strategy in {"explicit", "hybrid"} and len(keys) > config.max_explicit_clusters:
            raise ValueError("explicit_cluster_limit")
        selected: dict[str, tuple[str | None, str | None]] = {}
        for row in semantic_rows:
            if row.raw_messages_state == "missing":
                selected[row.trace_id or ""] = (None, "content_not_captured")
            elif row.raw_messages_state == "oversize":
                selected[row.trace_id or ""] = (None, "raw_messages_oversize")
            elif row.raw_messages_state != "valid":
                selected[row.trace_id or ""] = (None, "malformed_messages")
        valid_rows = [row for row in semantic_rows if row.raw_messages_state == "valid"]
        eligible = scanned_bytes = index = 0
        while index < len(valid_rows) and (
            not evidence_only or eligible < config.max_semantic_fit_inputs
        ):
            remaining = (
                config.max_semantic_fit_inputs - eligible if evidence_only else len(valid_rows)
            )
            page = valid_rows[index : index + min(100, remaining)]
            index += len(page)
            scanned_bytes += sum(row.raw_messages_utf8_bytes or 0 for row in page)
            if scanned_bytes > config.max_fit_content_scan_bytes:
                raise ValueError("fit_content_scan_limit")
            bodies = source.get_cluster_trace_messages(
                tenant, [row.trace_id for row in page if row.trace_id]
            )
            for row in page:
                choice = select_cluster_input(
                    Trace(trace_id=row.trace_id or "", raw_messages=bodies.get(row.trace_id or ""))
                )
                selected[row.trace_id or ""] = (choice.text, choice.reason)
                eligible += choice.text is not None
        inputs: list[ClusterInput] = []
        explicit_evidence = {row.trace_id: (key, count) for key, (row, count) in keys.items()}
        branches: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for row, key, reason in classified:
            trace_id = row.trace_id or ""
            text, text_reason = selected.get(trace_id, (None, None))
            candidate_reason = reason or text_reason
            if candidate_reason is not None:
                reasons[candidate_reason] += 1
            elif strategy in {"explicit", "hybrid"} and key is not None:
                branches["explicit"] += 1
            elif text is not None:
                branches["semantic"] += 1
            else:
                branches["not_selected"] += 1
            include_explicit = strategy in {"explicit", "hybrid"} and trace_id in explicit_evidence
            include_semantic = trace_id in selected and (
                not evidence_only or selected[trace_id][0] is not None
            )
            if (
                evidence_only
                and not (include_explicit or include_semantic)
                and candidate_reason is None
            ):
                continue
            inputs.append(
                ClusterInput(
                    trace_id,
                    tenant,
                    row.started_at_us,
                    key,
                    text,
                    reason or text_reason,
                    explicit_evidence.get(trace_id, ("", 1))[1],
                )
            )
        if candidate_summary is not None:
            candidate_summary.update(
                {
                    "schema": "candidate-summary-v1",
                    "candidate_count": len(rows),
                    "branches": {
                        name: branches[name] for name in ("explicit", "semantic", "not_selected")
                    },
                    "ineligible_count": sum(reasons.values()),
                    "ineligible_reasons": dict(sorted(reasons.items())),
                    "fit_evidence_count": sum(
                        item.ineligible_reason is None for item in inputs
                    ),
                }
            )
        return inputs

    def _stable_result(
        self,
        tenant: str,
        result: FitResult,
        config: FitConfig,
        model_fingerprint: str,
        existing: list[ClusterIdentity],
        actor: str,
    ) -> tuple[FitResult, list[ClusterIdentity]]:
        by_key = {identity.explicit_key: identity for identity in existing if identity.explicit_key}
        mapping: dict[str, str] = {}
        new_identities: list[ClusterIdentity] = []
        semantic = [cluster for cluster in result.clusters if cluster.kind == "semantic"]
        old = [
            identity
            for identity in existing
            if identity.kind == "semantic"
            and identity.lifecycle == "active"
            and identity.last_centroid is not None
            and identity.last_model_fingerprint == model_fingerprint
        ]
        if semantic and old:
            costs = np.clip(
                1.0
                - np.asarray([item.last_centroid for item in old])
                @ np.asarray([item.centroid for item in semantic]).T,
                0.0,
                2.0,
            )
            if not np.all(np.isfinite(costs)):
                raise ValueError("semantic_non_finite_distance")
            for old_index, new_index in zip(*linear_sum_assignment(costs), strict=True):
                distance = float(costs[old_index, new_index])
                row = np.delete(costs[old_index], new_index)
                column = np.delete(costs[:, new_index], old_index)
                if (
                    distance <= config.carryover_distance
                    and (
                        not len(row)
                        or float(np.min(row)) - distance > config.carryover_ambiguity_margin
                    )
                    and (
                        not len(column)
                        or float(np.min(column)) - distance > config.carryover_ambiguity_margin
                    )
                ):
                    mapping[semantic[new_index].cluster_id] = old[old_index].cluster_id
        for cluster in result.clusters:
            identity = by_key.get(cluster.explicit_key) if cluster.kind == "explicit" else None
            stable_id = identity.cluster_id if identity else mapping.get(cluster.cluster_id)
            if stable_id is None:
                stable_id = f"clu_{uuid4().hex}"
                new_identities.append(
                    ClusterIdentity(
                        tenant_id=tenant,
                        cluster_id=stable_id,
                        kind=cluster.kind,
                        explicit_key=cluster.explicit_key,
                        display_name=cluster.display_name,
                        created_by=actor,
                        updated_by=actor,
                    )
                )
            mapping[cluster.cluster_id] = stable_id
        clusters = tuple(
            replace(cluster, cluster_id=mapping[cluster.cluster_id]) for cluster in result.clusters
        )
        assignments = tuple(
            replace(item, cluster_id=mapping.get(item.cluster_id, item.cluster_id))
            for item in result.assignments
        )
        return replace(result, clusters=clusters, assignments=assignments), new_identities

    def fit(
        self,
        tenant: str,
        *,
        actor: str,
        strategy: str,
        cutoff: datetime,
        config: FitConfig | None = None,
    ) -> ClusterRegistryVersion:
        _valid_tenant(tenant)
        _valid_actor(actor)
        config = config or FitConfig(strategy=strategy)
        if config.strategy != strategy:
            raise ValueError("fit strategy/config mismatch")
        pointer = self.storage.get_active_cluster_registry(tenant)
        existing = self.storage.list_cluster_identities(tenant)
        active_counts = {
            kind: sum(
                identity.lifecycle == "active" and identity.kind == kind for identity in existing
            )
            for kind in ("explicit", "semantic")
        }
        if (
            active_counts["explicit"] > config.max_explicit_identities_per_tenant
            or active_counts["semantic"] > config.max_semantic_identities_per_tenant
        ):
            raise ValueError("identity_limit")
        with self.storage.cluster_analysis_snapshot() as source:
            rows = self._candidates(
                tenant,
                cutoff - timedelta(days=config.lookback_days),
                cutoff,
                config,
                source=source,
            )
            candidate_summary: dict[str, Any] = {}
            inputs = self._inputs(
                tenant,
                rows,
                strategy,
                config,
                evidence_only=True,
                source=source,
                candidate_summary=candidate_summary,
            )
        definition, definition_fingerprint, model_fingerprint = self._definition(strategy, config)
        result = self._strategy(strategy).fit(inputs, config)
        result, identities = self._stable_result(
            tenant, result, config, model_fingerprint, existing, actor
        )
        assigned_ids = {item.trace_id for item in result.assignments}
        missing_rows = [row for row in rows if row.trace_id not in assigned_ids]
        if missing_rows:
            with self.storage.cluster_analysis_snapshot() as source:
                assignment_inputs = self._inputs(
                    tenant,
                    missing_rows,
                    strategy,
                    config,
                    evidence_only=False,
                    source=source,
                )
            extra_assignments = self._strategy(strategy).assign(
                RegistryDefinition(strategy, result.clusters), assignment_inputs
            )
            result = replace(
                result,
                assignments=(*result.assignments, *extra_assignments),
            )
        statuses = {
            status: sum(item.status.value == status for item in result.assignments)
            for status in ("assigned", "outlier", "ineligible")
        }
        preview = _json(
            {
                "schema": "preview-report-v1",
                "candidate_count": len(rows),
                "fit_assignment_count": len(result.assignments),
                "cluster_count": len(result.clusters),
                "explicit_cluster_count": sum(item.kind == "explicit" for item in result.clusters),
                "semantic_cluster_count": sum(item.kind == "semantic" for item in result.clusters),
                "statuses": statuses,
                "chosen_k": result.chosen_k,
                "metrics": result.metrics,
                "warnings": result.warnings,
                "candidate_summary": candidate_summary,
            }
        )
        version = ClusterRegistryVersion(
            tenant_id=tenant,
            parent_version_id=pointer.version_id,
            strategy=strategy,
            cutoff=cutoff,
            lookback_days=config.lookback_days,
            fit_definition_json=definition,
            fit_definition_fingerprint=definition_fingerprint,
            preview_report_json=preview,
            created_by=actor,
        )
        clusters = [
            ClusterRegistryCluster(
                tenant,
                version.version_id,
                item.cluster_id,
                item.kind,
                list(item.centroid) if item.centroid else None,
                item.radius,
                item.member_count,
                item.outlier_count,
            )
            for item in result.clusters
        ]
        assignments = [
            self._stored_assignment(tenant, version.version_id, item, "fit")
            for item in result.assignments
        ]
        self.storage.insert_cluster_preview(version, identities, clusters, assignments)
        return version

    @staticmethod
    def _stored_assignment(
        tenant: str,
        version_id: str,
        item: AssignmentResult,
        origin: str,
    ) -> TraceClusterAssignment:
        return TraceClusterAssignment(
            tenant,
            version_id,
            item.trace_id,
            origin,
            item.status.value,
            item.cluster_id,
            item.cluster_kind,
            item.reason,
            item.distance,
        )

    def _loaded_definition(self, tenant: str, version: ClusterRegistryVersion):
        try:
            payload = json.loads(version.fit_definition_json)
            canonical = _json(payload)
            expected_fields = set(asdict(FitConfig()))
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "schema",
                    "strategy",
                    "selector",
                    "algorithm",
                    "model",
                    "model_fingerprint",
                    "runtime",
                    "config",
                }
                or payload["schema"] != "fit-definition-v1"
                or payload["strategy"] != version.strategy
                or payload["selector"] != "latest-user-v1"
                or payload["algorithm"] != "ward-best-k-v2"
                or not isinstance(payload["config"], dict)
                or set(payload["config"]) != expected_fields
                or _fingerprint(canonical) != version.fit_definition_fingerprint
            ):
                raise ValueError
            config = FitConfig(**payload["config"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid fit definition") from exc
        identities = {
            item.cluster_id: item for item in self.storage.list_cluster_identities(tenant)
        }
        clusters = tuple(
            ClusterDefinition(
                item.cluster_id,
                item.kind,
                identities[item.cluster_id].display_name,
                identities[item.cluster_id].explicit_key,
                tuple(item.centroid) if item.centroid else None,
                item.radius,
                item.member_count,
                item.outlier_count,
            )
            for item in self.storage.list_cluster_registry_clusters(tenant, version.version_id)
        )
        return config, RegistryDefinition(version.strategy, clusters)

    def assign(
        self,
        tenant: str,
        version_id: str,
        *,
        through_cutoff: datetime,
    ) -> int:
        version = self.storage.get_cluster_registry_version(tenant, version_id)
        if version is None:
            raise ValueError("unknown cluster registry version")
        config, definition = self._loaded_definition(tenant, version)
        _, current_fingerprint, _ = self._definition(version.strategy, config)
        if current_fingerprint != version.fit_definition_fingerprint:
            raise ValueError("model_unavailable")
        with self.storage.cluster_analysis_snapshot() as source:
            rows = self._candidates(
                tenant,
                version.cutoff - timedelta(days=version.lookback_days),
                through_cutoff,
                config,
                limit=10_000,
                source=source,
                missing_version_id=version_id,
                bounded_batch=True,
            )
            admitted: list[ClusterTraceCandidate] = []
            admitted_bytes = 0
            for row in rows:
                row_bytes = (
                    row.raw_messages_utf8_bytes
                    if row.raw_messages_state == "valid" and row.raw_messages_utf8_bytes is not None
                    else 0
                )
                if row_bytes > config.max_fit_content_scan_bytes:
                    admitted.append(replace(row, raw_messages_state="oversize"))
                    break
                if admitted_bytes + row_bytes > config.max_fit_content_scan_bytes:
                    break
                admitted.append(row)
                admitted_bytes += row_bytes
            inputs = self._inputs(
                tenant,
                admitted,
                version.strategy,
                config,
                evidence_only=False,
                source=source,
            )
        results = self._strategy(version.strategy).assign(definition, inputs)
        stored = [
            self._stored_assignment(tenant, version_id, item, "incremental") for item in results
        ]
        for start in range(0, len(stored), 1_000):
            self.storage.insert_trace_cluster_assignments(tenant, stored[start : start + 1_000])
        return len(stored)

    def validate(self, tenant: str, version_id: str, *, actor: str) -> dict[str, Any]:
        _valid_tenant(tenant)
        _valid_actor(actor)
        version = self.storage.get_cluster_registry_version(tenant, version_id)
        if version is None:
            raise ValueError("unknown cluster registry version")
        config, definition = self._loaded_definition(tenant, version)
        assignments = self.storage.list_trace_cluster_assignments(tenant, version_id)
        fit_counts = {cluster.cluster_id: 0 for cluster in definition.clusters}
        for item in assignments:
            if item.origin == "fit" and item.status == "assigned" and item.cluster_id:
                fit_counts[item.cluster_id] += 1
        explicit_count = sum(cluster.kind == "explicit" for cluster in definition.clusters)
        semantic_count = len(definition.clusters) - explicit_count
        health = (
            explicit_count <= config.max_explicit_clusters
            and semantic_count <= config.max_semantic_clusters
            and len(definition.clusters)
            <= config.max_explicit_clusters + config.max_semantic_clusters
            and all(
                fit_counts[cluster.cluster_id]
                >= (1 if cluster.kind == "explicit" else config.min_cluster_size)
                and (
                    cluster.kind == "explicit"
                    or (
                        cluster.radius is not None
                        and cluster.radius <= config.max_assignment_distance
                    )
                )
                for cluster in definition.clusters
            )
        )
        stored_definition = _json(json.loads(version.fit_definition_json))
        current_definition, current_fingerprint, _ = self._definition(version.strategy, config)
        try:
            preview = json.loads(version.preview_report_json)
        except (TypeError, ValueError):
            preview = {}
        fit_assignments = [item for item in assignments if item.origin == "fit"]
        candidate_ids = [item.trace_id for item in fit_assignments]
        expected_statuses = {
            status: sum(item.status == status for item in fit_assignments)
            for status in ("assigned", "outlier", "ineligible")
        }
        preview_ok = (
            isinstance(preview, dict)
            and set(preview)
            == {
                "schema",
                "candidate_count",
                "fit_assignment_count",
                "cluster_count",
                "explicit_cluster_count",
                "semantic_cluster_count",
                "statuses",
                "chosen_k",
                "metrics",
                "warnings",
                "candidate_summary",
            }
            and preview.get("schema") == "preview-report-v1"
            and preview.get("candidate_count") == len(fit_assignments)
            and preview.get("fit_assignment_count") == len(fit_assignments)
            and preview.get("cluster_count") == len(definition.clusters)
            and preview.get("explicit_cluster_count") == explicit_count
            and preview.get("semantic_cluster_count") == semantic_count
            and preview.get("statuses") == expected_statuses
            and isinstance(preview.get("metrics"), dict)
            and isinstance(preview.get("warnings"), list)
            and isinstance(preview.get("candidate_summary"), dict)
            and preview["candidate_summary"].get("candidate_count")
            == len(fit_assignments)
        )
        coverage = (
            len(candidate_ids) == len(set(candidate_ids))
            and preview_ok
        )
        definition_ok = (
            _fingerprint(stored_definition) == version.fit_definition_fingerprint
            and current_fingerprint == version.fit_definition_fingerprint
            and stored_definition == current_definition
            and preview_ok
        )
        model_ok = definition_ok
        passed = bool(definition.clusters) and health and coverage and definition_ok and model_ok
        if version.strategy == "hybrid" and any(
            cluster.kind == "explicit" for cluster in definition.clusters
        ):
            passed = health and coverage and definition_ok and model_ok
        report = {
            "schema": "validation-report-v1",
            "version_id": version_id,
            "passed": passed,
            "structural": health,
            "fit_counts": fit_counts,
            "coverage": coverage,
            "assignment_count": len(assignments),
            "candidate_count": len(fit_assignments),
            "candidate_digest": cluster_candidate_digest(
                candidate_ids
            ),
            "definition": definition_ok,
            "model": model_ok,
        }
        self.storage.insert_cluster_registry_event(
            ClusterRegistryEvent(
                tenant_id=tenant,
                action="validated" if passed else "validation_failed",
                to_version_id=version_id,
                actor=actor,
                details_json=_json(report),
            )
        )
        return report

    def activate(
        self,
        tenant: str,
        version_id: str,
        *,
        expected_generation: int,
        actor: str,
    ) -> ActiveClusterRegistry:
        _valid_tenant(tenant)
        _valid_actor(actor)
        version = self.storage.get_cluster_registry_version(tenant, version_id)
        if version is None:
            raise ValueError("unknown cluster registry version")
        self.assign(tenant, version_id, through_cutoff=version.cutoff)
        report = self.validate(tenant, version_id, actor=actor)
        if not report["passed"]:
            raise ValueError("cluster registry version is not validated")
        return self.storage.activate_cluster_registry(
            tenant,
            version_id,
            expected_generation=expected_generation,
            actor=actor,
            action="activated",
            expected_candidate_digest=report["candidate_digest"],
        )

    def rollback(
        self,
        tenant: str,
        version_id: str,
        *,
        expected_generation: int,
        actor: str,
        through_cutoff: datetime | None = None,
    ) -> ActiveClusterRegistry:
        _valid_tenant(tenant)
        _valid_actor(actor)
        activated = any(
            event.to_version_id == version_id and event.action in {"activated", "rolled_back"}
            for event in self.storage.list_cluster_registry_events(tenant, version_id)
        )
        if not activated:
            raise ValueError("rollback target was never activated")
        version = self.storage.get_cluster_registry_version(tenant, version_id)
        self.assign(tenant, version_id, through_cutoff=through_cutoff or version.cutoff)
        report = self.validate(tenant, version_id, actor=actor)
        if not report["passed"]:
            raise ValueError("cluster registry version is not validated")
        return self.storage.activate_cluster_registry(
            tenant,
            version_id,
            expected_generation=expected_generation,
            actor=actor,
            action="rolled_back",
            expected_candidate_digest=report["candidate_digest"],
        )

    def refit(
        self,
        tenant: str,
        *,
        actor: str,
        cutoff: datetime,
    ) -> ClusterRegistryVersion:
        _valid_tenant(tenant)
        _valid_actor(actor)
        pointer = self.storage.get_active_cluster_registry(tenant)
        if pointer.version_id is None:
            raise ValueError("no active cluster registry")
        version = self.storage.get_cluster_registry_version(tenant, pointer.version_id)
        config = FitConfig(**json.loads(version.fit_definition_json)["config"])
        return self.fit(
            tenant, actor=actor, strategy=version.strategy, cutoff=cutoff, config=config
        )

    def inspect(
        self,
        tenant: str,
        version_id: str,
        *,
        identity_limit: int = 250,
        assignment_limit: int = 500,
        event_limit: int = 100,
        identity_offset: int = 0,
        assignment_offset: int = 0,
        event_offset: int = 0,
    ) -> dict[str, Any]:
        _valid_tenant(tenant)
        for name, value, maximum in (
            ("identity_limit", identity_limit, 250),
            ("assignment_limit", assignment_limit, 500),
            ("event_limit", event_limit, 100),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside the inspect bound")
        for name, value in (
            ("identity_offset", identity_offset),
            ("assignment_offset", assignment_offset),
            ("event_offset", event_offset),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        version = self.storage.get_cluster_registry_version(tenant, version_id)
        if version is None:
            raise ValueError("unknown cluster registry version")
        clusters = self.storage.list_cluster_registry_clusters(tenant, version_id)
        if len(clusters) > 250:
            raise ValueError("cluster registry exceeds the inspect limit")
        identities = self.storage.list_cluster_identities(
            tenant,
            cluster_ids=[item.cluster_id for item in clusters],
            limit=identity_limit + 1,
            offset=identity_offset,
        )
        assignments = self.storage.list_trace_cluster_assignments(
            tenant,
            version_id,
            limit=assignment_limit + 1,
            offset=assignment_offset,
        )
        events = self.storage.list_cluster_registry_events(
            tenant,
            version_id,
            limit=event_limit + 1,
            offset=event_offset,
        )
        return {
            "version": asdict(version),
            "strategy_status": clustering_strategy_status(version.strategy),
            "clusters": [asdict(item) for item in clusters],
            "identities": [asdict(item) for item in identities[:identity_limit]],
            "assignments": [asdict(item) for item in assignments[:assignment_limit]],
            "events": [asdict(item) for item in events[:event_limit]],
            "truncated": {
                "identities": len(identities) > identity_limit,
                "assignments": len(assignments) > assignment_limit,
                "events": len(events) > event_limit,
            },
            "page": {
                "identity_limit": identity_limit,
                "identity_offset": identity_offset,
                "assignment_limit": assignment_limit,
                "assignment_offset": assignment_offset,
                "event_limit": event_limit,
                "event_offset": event_offset,
            },
        }

    def normalize(self, tenant: str, *, limit: int = 1_000) -> dict[str, Any]:
        """Normalize one bounded page of pre-Task-5 traces for registry queries."""
        _valid_tenant(tenant)
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("normalization limit must be in [1,10000]")
        processed = self.storage.normalize_cluster_trace_analysis(tenant, limit=limit)
        pending = self.storage.count_pending_analysis_rows(tenant)
        return {
            "schema": "analysis-normalization-v1",
            "processed": processed,
            "pending": pending,
            "complete": pending == 0,
        }

    def rename(self, tenant: str, cluster_id: str, display_name: str, *, actor: str) -> None:
        _valid_tenant(tenant)
        _valid_actor(actor)
        if not isinstance(display_name, str):
            raise ValueError("invalid cluster display name")
        normalized = unicodedata.normalize("NFC", display_name)
        try:
            encoded_size = len(normalized.encode("utf-8"))
        except UnicodeError:
            encoded_size = 257
        if (
            not normalized
            or len(normalized) > 80
            or encoded_size > 256
            or any(unicodedata.category(char).startswith("C") for char in normalized)
        ):
            raise ValueError("invalid cluster display name")
        self.storage.rename_cluster_identity(tenant, cluster_id, normalized, actor=actor)
