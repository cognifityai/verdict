"""CLI for count-based historical analysis and prospective monitoring."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from verdict_eval.count_monitor import analyze_matched, analyze_traces
from verdict_eval.monitoring import (
    build_series_bootstrap_snapshot,
    create_series_from_history,
    monitor_status,
    persist_matched_report,
    run_scheduled,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verdict-monitor")
    parser.add_argument(
        "--storage",
        default=os.environ.get("VERDICT_STORAGE", "sqlite:///./verdict.db"),
        help="Verdict SQLite path/URL or PostgreSQL URL.",
    )
    parser.add_argument("--limit", type=int, default=100_000, help="Maximum traces to analyze.")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser(
        "bootstrap", help="Compare equal older/newer historical count cohorts."
    )
    bootstrap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    bootstrap.add_argument(
        "--activate", action="store_true", help="Persist and activate frozen baselines."
    )
    bootstrap.add_argument(
        "--target-units",
        type=int,
        default=None,
        help="Units per prospective cohort (default: derive from baseline, capped at 10).",
    )
    bootstrap.add_argument("--from", dest="from_time", help="Inclusive ISO-8601 event time.")
    bootstrap.add_argument("--through", help="Exclusive ISO-8601 event time.")
    matched = commands.add_parser(
        "matched", help="Compare repeated prompt IDs under two model configurations."
    )
    matched.add_argument("--baseline-model", required=True)
    matched.add_argument("--current-model", required=True)
    matched.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    run = commands.add_parser("run", help="Process new traces for every active monitor.")
    run.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    refit = commands.add_parser(
        "refit", help="Build a candidate baseline without interrupting the active one."
    )
    refit.add_argument("--target-units", type=int, default=None)
    refit.add_argument("--from", dest="from_time", help="Inclusive ISO-8601 event time.")
    refit.add_argument("--through", help="Exclusive ISO-8601 event time.")
    refit.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    activate = commands.add_parser("activate", help="Atomically activate a candidate baseline.")
    activate.add_argument("--series-id", required=True)
    activate.add_argument("--expected-active", required=True)
    activate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status = commands.add_parser("status", help="Show durable monitoring state.")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        print("ERROR: --limit must be positive")
        return 2
    if (
        args.command in {"bootstrap", "refit"}
        and args.target_units is not None
        and args.target_units < 2
    ):
        print("ERROR: --target-units must be at least 2")
        return 2

    from verdict.client import _resolve_storage

    try:
        storage = _resolve_storage(args.storage)
    except Exception:
        print("ERROR: cannot open configured storage")
        return 2
    traces = storage.list_traces(limit=args.limit)
    if args.command in {"bootstrap", "refit"}:
        try:
            from_time = _parse_time(args.from_time) if args.from_time else None
            through = _parse_time(args.through) if args.through else None
        except ValueError:
            storage.close()
            print("ERROR: --from and --through require timezone-aware ISO-8601 values")
            return 2
        if from_time and through and from_time >= through:
            storage.close()
            print("ERROR: --from must be earlier than --through")
            return 2
        traces = [
            trace
            for trace in traces
            if (from_time is None or trace.started_at.astimezone(timezone.utc) >= from_time)
            and (through is None or trace.started_at.astimezone(timezone.utc) < through)
        ]

    if args.command == "run":
        payload = {
            "schema": "verdict-count-monitor-v1",
            "mode": "run",
            **run_scheduled(storage, traces),
        }
        storage.close()
        return _emit(args, payload)
    if args.command == "status":
        payload = {
            "schema": "verdict-count-monitor-v1",
            "mode": "status",
            **monitor_status(storage),
        }
        storage.close()
        return _emit(args, payload)
    if args.command == "activate":
        candidate = storage.get_monitor_series(args.series_id)
        if candidate is None:
            storage.close()
            print("ERROR: monitor candidate not found")
            return 2
        snapshot = build_series_bootstrap_snapshot(storage, candidate, traces)
        try:
            activated = storage.activate_monitor_series(
                args.series_id,
                expected_active_series_id=args.expected_active,
                snapshot=snapshot,
            )
        except ValueError:
            storage.close()
            print("ERROR: monitor activation conflict")
            return 2
        payload = {
            "schema": "verdict-count-monitor-v1",
            "mode": "activate",
            "active_series_id": activated.series_id,
            "bootstrap_run_id": snapshot[0].run_id,
        }
        storage.close()
        return _emit(args, payload)
    if args.command == "refit":
        active = [series for series in storage.list_monitor_series() if series.state == "active"]
        if not active:
            storage.close()
            print("ERROR: no active monitor to refit")
            return 2
        try:
            candidates = create_series_from_history(
                storage, traces, target_units=args.target_units, state="candidate"
            )
        except ValueError:
            storage.close()
            print("ERROR: no eligible active monitor history to refit")
            return 2
        payload = {
            "schema": "verdict-count-monitor-v1",
            "mode": "refit",
            "candidate_series_id": candidates[0].series_id if len(candidates) == 1 else None,
            "candidate_series_ids": [series.series_id for series in candidates],
        }
        storage.close()
        return _emit(args, payload)

    if args.command == "matched":
        matched = analyze_matched(
            traces,
            baseline_model=args.baseline_model,
            current_model=args.current_model,
        )
        payload = {
            "schema": "verdict-count-monitor-v1",
            "mode": args.command,
            **matched.to_dict(),
        }
        run = persist_matched_report(
            storage,
            matched,
            traces,
            baseline_model=args.baseline_model,
            current_model=args.current_model,
        )
        payload["persisted_run_id"] = run.run_id if run else None
        reports = ()
    else:
        reports = analyze_traces(traces)
        payload = {
            "schema": "verdict-count-monitor-v1",
            "mode": args.command,
            "scopes": [report.to_dict() for report in reports],
        }
        if args.activate:
            activated = create_series_from_history(
                storage, traces, target_units=args.target_units, state="active"
            )
            payload["active_series_id"] = activated[0].series_id if len(activated) == 1 else None
            payload["active_series_ids"] = [series.series_id for series in activated]
    storage.close()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        if args.command == "matched":
            print(
                f"controlled_comparison: {payload['status']} "
                f"({payload['matched_pairs']} matched prompts)"
            )
        elif not reports:
            print("No eligible traces found.")
        for report in reports:
            print(
                f"{report.scope.workload}/{report.scope.granularity}: "
                f"{report.status.value} "
                f"({report.baseline_units} baseline, {report.current_units} current units)"
            )
            for result in report.results:
                if result.status.value == "drift_detected":
                    print(
                        f"  {result.cluster_id} {result.metric}: drift_detected "
                        f"effect={result.effect_size:+.3f} p_adj={result.p_value_adjusted:.4g}"
                    )
    return 0


def _emit(args: argparse.Namespace, payload: dict[str, object]) -> int:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
