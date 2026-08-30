"""One-command local agent capture, drift analysis, and dashboard."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from verdict.client import _resolve_storage
from verdict.telemetry.local_agents import capture_local_history_url
from verdict.telemetry.local_cli import default_storage
from verdict.telemetry.runner import ImportRunError

from verdict_eval.clustering_strategies import FitConfig
from verdict_eval.count_monitor import analyze_traces
from verdict_eval.live_judging import JudgeBudgetError
from verdict_eval.monitoring import create_series_from_history, run_scheduled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verdict-local",
        description="Import local Claude/Codex history, detect drift, and serve Verdict.",
    )
    parser.add_argument("--storage", default=default_storage())
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--source", choices=["all", "codex", "claude"], default="all")
    parser.add_argument("--tenant-id")
    parser.add_argument("--target-units", type=int)
    parser.add_argument(
        "--semantic-model-path",
        type=Path,
        help="Pinned MiniLM snapshot path (auto-resolved or downloaded when omitted).",
    )
    parser.add_argument(
        "--judge-provider",
        choices=["none", "anthropic"],
        default="none",
        help="Optional real response-quality judge (never enabled implicitly).",
    )
    parser.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--judge-budget-usd", type=float, default=15.0)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-serve", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.target_units is not None and args.target_units < 2:
        print("ERROR: --target-units must be at least 2")
        return 2
    if not 1 <= args.port <= 65535:
        print("ERROR: --port must be between 1 and 65535")
        return 2
    if args.judge_budget_usd <= 0:
        print("ERROR: --judge-budget-usd must be positive")
        return 2
    if args.judge_provider == "anthropic" and args.judge_model != "claude-haiku-4-5-20251001":
        print("ERROR: budgeted local judging currently supports claude-haiku-4-5-20251001")
        return 2
    try:
        summaries = capture_local_history_url(
            args.storage,
            codex_root=args.codex_root,
            claude_root=args.claude_root,
            source=args.source,
            tenant_id=args.tenant_id,
        )
        storage = _resolve_storage(args.storage)
        try:
            traces = storage.list_traces(limit=100_000)
            from verdict_eval.clustering import FrozenMiniLMEmbedder, resolve_frozen_minilm_path
            from verdict_eval.semantic_monitoring import (
                assign_active_semantic,
                fit_semantic_bootstrap,
                has_active_semantic_registry,
            )

            embedder = FrozenMiniLMEmbedder(
                resolve_frozen_minilm_path(args.semantic_model_path)
            )
            if has_active_semantic_registry(storage, traces):
                semantic = assign_active_semantic(storage, traces, embedder=embedder)
            else:
                semantic = fit_semantic_bootstrap(
                    storage,
                    traces,
                    embedder=embedder,
                    actor="verdict-local",
                    config=FitConfig(
                        strategy="semantic",
                        min_cluster_size=2,
                        max_semantic_clusters=12,
                    ),
                )
            judge_summary = None
            if args.judge_provider == "anthropic":
                from verdict_eval.live_judging import judge_with_budget
                from verdict_eval.providers import AnthropicAdapter

                judge_summary = judge_with_budget(
                    storage,
                    traces,
                    provider=AnthropicAdapter(max_retries=1),
                    model=args.judge_model,
                    budget_usd=args.judge_budget_usd,
                    input_usd_per_million=1.0,
                    output_usd_per_million=5.0,
                )
            judgments = storage.list_judgments(limit=max(1_000, len(traces) * 10))
            active = create_series_from_history(
                storage,
                traces,
                target_units=args.target_units,
                state="active",
                judgments=judgments,
                assignments=semantic.assignments,
                registry_references=semantic.registry_references,
            )
            reports = analyze_traces(
                traces,
                judgments=judgments,
                assignments=semantic.assignments,
                registry_references=semantic.registry_references,
            )
            scheduled = run_scheduled(
                storage,
                traces,
                judgments=judgments,
                assignments=semantic.assignments,
            )
        finally:
            storage.close()
    except Exception as exc:
        error = (
            str(exc)
            if isinstance(exc, (ImportRunError, JudgeBudgetError))
            else f"local analysis failed ({type(exc).__name__})"
        )
        print(f"ERROR: {error}")
        return 2

    source_payload = {name: asdict(summary) for name, summary in summaries.items()}
    payload = {
        "schema": "verdict-local-v1",
        "capture": {
            "stored": sum(summary.stored for summary in summaries.values()),
            "skipped": sum(summary.skipped for summary in summaries.values()),
            "sources": source_payload,
        },
        "analysis": {
            "schema": "verdict-count-monitor-v1",
            "scopes": [report.to_dict() for report in reports],
            "active_series_ids": [series.series_id for series in active],
            "cluster_registry_version_ids": semantic.version_ids,
            "judge": asdict(judge_summary) if judge_summary is not None else None,
            "scheduled": scheduled,
        },
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Imported {payload['capture']['stored']} completed local agent turn(s).")
        if not reports:
            print("No historical comparison is available yet.")
        for report in reports:
            print(
                f"{report.scope.workload}/{report.scope.granularity}: "
                f"{report.status.value} "
                f"({report.baseline_units} baseline, {report.current_units} current sessions)"
            )
        if judge_summary is not None:
            print(
                f"Real judge: {judge_summary.completed} completed, "
                f"{judge_summary.reused} reused, {judge_summary.errors} errors; "
                f"token-accounted spend ${judge_summary.spent_usd:.4f}."
            )
        if not args.no_serve:
            print(f"Verdict local dashboard -> http://127.0.0.1:{args.port}")
    if args.no_serve:
        return 0

    import uvicorn
    from verdict.dashboard import create_app

    uvicorn.run(create_app(storage=args.storage), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
