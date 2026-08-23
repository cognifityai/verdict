"""CLI for tenant-scoped immutable cluster registries."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from verdict.client import _resolve_storage

from verdict_eval.cluster_registry import (
    ClusterRegistryService,
    UnsupportedClusteringStorageError,
    clustering_strategy_status,
)
from verdict_eval.clustering import FrozenMiniLMEmbedder
from verdict_eval.clustering_strategies import FitConfig


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verdict-cluster")
    parser.add_argument(
        "--storage", default=os.environ.get("VERDICT_STORAGE", "sqlite:///./verdict.db")
    )
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--actor", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit")
    fit.add_argument(
        "--strategy",
        choices=["explicit", "semantic", "hybrid"],
        required=True,
        help=(
            "Required deliberate selection. Explicit is supported; semantic and "
            "hybrid's semantic fallback are experimental opt-in features."
        ),
    )
    fit.add_argument("--cutoff", type=_instant, required=True)
    fit.add_argument("--target-workload")
    fit.add_argument("--model-path")
    refit = commands.add_parser("refit")
    refit.add_argument("--cutoff", type=_instant, required=True)
    refit.add_argument("--model-path")
    for name in ("assign", "validate", "activate", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--version", required=True)
        command.add_argument("--model-path")
        if name == "assign":
            command.add_argument("--through-cutoff", type=_instant, required=True)
        if name in {"activate", "rollback"}:
            command.add_argument("--expected-generation", type=int, required=True)
        if name == "rollback":
            command.add_argument("--through-cutoff", type=_instant)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--version", required=True)
    inspect.add_argument("--identity-limit", type=int, default=250)
    inspect.add_argument("--identity-offset", type=int, default=0)
    inspect.add_argument("--assignment-limit", type=int, default=500)
    inspect.add_argument("--assignment-offset", type=int, default=0)
    inspect.add_argument("--event-limit", type=int, default=100)
    inspect.add_argument("--event-offset", type=int, default=0)
    rename = commands.add_parser("rename")
    rename.add_argument("--cluster-id", required=True)
    rename.add_argument("--display-name", required=True)
    normalize = commands.add_parser("normalize")
    normalize.add_argument("--limit", type=int, default=1_000)
    return parser


def _service(args: argparse.Namespace, storage: object) -> ClusterRegistryService:
    if not getattr(args, "model_path", None):
        return ClusterRegistryService(storage)

    def factory() -> object:
        return FrozenMiniLMEmbedder(args.model_path)

    return ClusterRegistryService(storage, embedder_factory=factory)


def _run(args: argparse.Namespace, service: ClusterRegistryService) -> object:
    if args.command == "normalize":
        return service.normalize(args.tenant, limit=args.limit)
    if args.command == "fit":
        if args.strategy == "explicit" and args.model_path:
            raise ValueError("explicit strategy forbids --model-path")
        if args.strategy != "explicit" and not args.model_path:
            raise ValueError("semantic and hybrid strategies require --model-path")
        return service.fit(
            args.tenant,
            actor=args.actor,
            strategy=args.strategy,
            cutoff=args.cutoff,
            config=FitConfig(strategy=args.strategy, target_workload=args.target_workload),
        )
    if args.command == "refit":
        return service.refit(args.tenant, actor=args.actor, cutoff=args.cutoff)
    if args.command == "assign":
        return {
            "assigned": service.assign(
                args.tenant, args.version, through_cutoff=args.through_cutoff
            )
        }
    if args.command == "validate":
        return service.validate(args.tenant, args.version, actor=args.actor)
    if args.command == "activate":
        return service.activate(
            args.tenant,
            args.version,
            expected_generation=args.expected_generation,
            actor=args.actor,
        )
    if args.command == "rollback":
        return service.rollback(
            args.tenant,
            args.version,
            expected_generation=args.expected_generation,
            through_cutoff=args.through_cutoff,
            actor=args.actor,
        )
    if args.command == "inspect":
        return service.inspect(
            args.tenant,
            args.version,
            identity_limit=args.identity_limit,
            identity_offset=args.identity_offset,
            assignment_limit=args.assignment_limit,
            assignment_offset=args.assignment_offset,
            event_limit=args.event_limit,
            event_offset=args.event_offset,
        )
    service.rename(args.tenant, args.cluster_id, args.display_name, actor=args.actor)
    return {"renamed": args.cluster_id}


_PASSTHROUGH_ERROR_CODES = {
    "analysis_index_pending",
    "explicit_cluster_limit",
    "fit_candidate_limit",
    "fit_content_scan_limit",
    "identity_limit",
    "model_unavailable",
    "semantic_embedding_shape",
    "semantic_non_finite",
    "semantic_non_finite_distance",
    "semantic_zero_norm",
}


def _safe_error_code(exc: Exception) -> str:
    """Map failures to a closed machine code without exposing exception text."""
    if isinstance(exc, UnsupportedClusteringStorageError):
        return "unsupported_storage"
    if not isinstance(exc, ValueError):
        return "internal_error"
    message = str(exc)
    if message in _PASSTHROUGH_ERROR_CODES:
        return message
    exact = {
        "unknown cluster registry version": "unknown_version",
        "cluster registry generation conflict": "generation_conflict",
        "cluster registry parent conflict": "parent_conflict",
        "cluster registry coverage changed": "coverage_changed",
        "cluster registry version is not validated": "validation_failed",
        "rollback target was never activated": "rollback_target_not_activated",
        "no active cluster registry": "no_active_registry",
        "invalid fit definition": "incompatible_definition",
        "normalization limit must be in [1,10000]": "invalid_limit",
        "explicit strategy forbids --model-path": "invalid_model_configuration",
        "semantic and hybrid strategies require --model-path": "model_path_required",
    }
    return exact.get(message, "invalid_request")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    storage = None
    try:
        storage = _resolve_storage(args.storage)
        result = _run(args, _service(args, storage))
        strategy = getattr(result, "strategy", None)
        if is_dataclass(result):
            result = asdict(result)
        payload = {"schema": "verdict-cluster-v1", "result": result}
        if strategy is not None:
            payload["strategy_status"] = clustering_strategy_status(strategy)
        print(json.dumps(payload, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"schema": "verdict-cluster-v1", "error": _safe_error_code(exc)}))
        return 2
    finally:
        if storage is not None:
            storage.close()
