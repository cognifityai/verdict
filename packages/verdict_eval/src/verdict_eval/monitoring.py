"""Durable scheduled monitoring over frozen count-cohort baselines."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from verdict.schema import (
    DriftDirection,
    DriftRun,
    DriftSignal,
    Judgment,
    MonitorMember,
    MonitorResult,
    MonitorSeries,
    Trace,
)
from verdict.storage.monitoring import MonitorStorage

from verdict_eval.clustering import HashingEmbedder
from verdict_eval.count_monitor import (
    AnalysisStatus,
    BootstrapBundle,
    HistoryUnit,
    MatchedReport,
    ScopeKey,
    _quality_scores,
    build_bootstrap_bundles,
    compare_cohorts,
    scope_for_trace,
)
from verdict_eval.stable_clustering import ClusterRegistry, StableIntentClusterer

STRUCTURAL_EVALUATOR = "deterministic-structural-count-v1"


def persist_matched_report(
    storage: MonitorStorage,
    report: MatchedReport,
    traces: list[Trace],
    *,
    baseline_model: str,
    current_model: str,
) -> DriftRun | None:
    """Persist a controlled matched comparison for dashboard consumption."""
    if report.matched_pairs < 2:
        return None
    replace_run = getattr(storage, "replace_drift_run", None)
    if not callable(replace_run):
        return None
    eligible = [
        trace
        for trace in traces
        if (trace.request_model or trace.response_model) in {baseline_model, current_model}
        and trace.tags.get("verdict.intent_key")
    ]
    if not eligible:
        return None
    comparison_hash = hashlib.sha256(f"{baseline_model}|{current_model}".encode()).hexdigest()[:16]
    evaluator = f"deterministic-matched-count-v1:{comparison_hash}"
    run_id = uuid5(
        NAMESPACE_URL,
        "|".join(
            [
                "verdict-matched-run-v1",
                evaluator,
                *(sorted(trace.trace_id for trace in eligible)),
            ]
        ),
    ).hex
    analysis_time = max(_event_time(trace) for trace in eligible)
    examples = [
        trace.trace_id
        for trace in sorted(eligible, key=lambda row: (_event_time(row), row.trace_id))
        if (trace.request_model or trace.response_model) == current_model
    ][:5]
    signals = [
        DriftSignal(
            signal_id=uuid5(
                NAMESPACE_URL,
                f"verdict-matched-signal-v1|{run_id}|{result.metric}",
            ).hex,
            detected_at=analysis_time,
            cluster_id="matched",
            dimension=result.metric,
            direction=DriftDirection.CHANGE,
            statistic_name="wilcoxon_paired",
            statistic_value=result.effect_size,
            p_value=result.p_value,
            p_value_adjusted=result.p_value_adjusted,
            sample_size_current=result.current_n,
            sample_size_baseline=result.baseline_n,
            contributing_layers=["matched_structural"],
            example_trace_ids=examples,
            recommended_action=(
                f"Review the matched {baseline_model} versus {current_model} responses."
            ),
            evaluator_fingerprint=evaluator,
            run_id=run_id,
        )
        for result in report.results
        if result.status is AnalysisStatus.DRIFT_DETECTED
    ]
    run = DriftRun(
        run_id=run_id,
        analysis_time=analysis_time,
        completed_at=datetime.now(timezone.utc),
        evaluator_fingerprint=evaluator,
        signal_count=len(signals),
    )
    replace_run(run, signals)
    return run


def create_series_from_history(
    storage: MonitorStorage,
    traces: list[Trace],
    *,
    target_units: int | None,
    state: str,
    judgments: list[Judgment] | tuple[Judgment, ...] = (),
    assignments: dict[str, str] | None = None,
    registry_references: dict[str | None, str] | None = None,
) -> list[MonitorSeries]:
    if target_units is not None and target_units < 2:
        raise ValueError("target_units must be at least 2")
    if state not in {"active", "candidate"}:
        raise ValueError("monitor series state must be active or candidate")
    created: list[MonitorSeries] = []
    for bundle in build_bootstrap_bundles(
        traces,
        judgments=judgments,
        assignments=assignments,
        registry_references=registry_references,
    ):
        if not bundle.plan.baseline or not bundle.plan.current:
            continue
        scope_payload = {
            **bundle.report.scope.to_dict(),
            "boundary_method": "session_first_event_v2",
        }
        scope_json = json.dumps(scope_payload, sort_keys=True, separators=(",", ":"))
        scope_identity = {
            "tenant_id": bundle.report.scope.tenant_id,
            "workload": bundle.report.scope.workload,
            "granularity": bundle.report.scope.granularity,
        }
        if bundle.report.scope.evidence_layer == "quality":
            scope_identity.update(
                {
                    "evidence_layer": "quality",
                    "evaluator_fingerprint": bundle.report.scope.evaluator_fingerprint,
                }
            )
        scope_key = hashlib.sha256(
            json.dumps(scope_identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        active = storage.get_active_monitor_series(scope_key)
        if state == "active" and active is not None:
            created.append(active)
            continue
        if state == "candidate" and active is None:
            continue

        all_rows = [
            trace for unit in (*bundle.plan.baseline, *bundle.plan.current) for trace in unit.traces
        ]
        resolved_target = target_units or (
            active.target_units
            if state == "candidate" and active
            else _derived_target_units(bundle)
        )
        boundary = max(
            (*bundle.plan.baseline, *bundle.plan.current),
            key=lambda unit: (unit.event_time, unit.unit_id),
        )
        identity = "|".join(
            [
                "verdict-monitor-series-v1",
                scope_key,
                bundle.registry_json,
                str(resolved_target),
                *(sorted(trace.trace_id for trace in all_rows)),
            ]
        )
        series_id = uuid5(NAMESPACE_URL, identity).hex
        now = datetime.now(timezone.utc)
        series = MonitorSeries(
            series_id=series_id,
            scope_key=scope_key,
            scope_json=scope_json,
            state=state,
            generation=0,
            parent_series_id=active.series_id if active else None,
            registry_json=bundle.registry_json,
            boundary_time=boundary.event_time,
            boundary_trace_id=boundary.unit_id,
            target_units=resolved_target,
            late_arrival_count=0,
            created_at=now,
            updated_at=now,
        )
        members = _bootstrap_members(series, bundle)
        snapshot = _bootstrap_snapshot(series, bundle) if state == "active" else None
        storage.create_monitor_series(series, members, snapshot=snapshot)
        created.append(storage.get_monitor_series(series_id) or series)
    if state == "candidate" and not created:
        raise ValueError("cannot refit without eligible active monitor history")
    return created


def _derived_target_units(bundle: BootstrapBundle) -> int:
    units_by_cluster: dict[str, set[str]] = defaultdict(set)
    for unit in bundle.plan.baseline:
        for trace in unit.traces:
            if (cluster_id := bundle.assignments.get(trace.trace_id)) not in {
                None,
                "not_evaluable",
                "new_intent",
            }:
                units_by_cluster[cluster_id].add(unit.unit_id)
    counts = sorted(len(units) for units in units_by_cluster.values() if len(units) >= 2)
    return min(10, counts[len(counts) // 2]) if counts else 2


def _bootstrap_snapshot(
    series: MonitorSeries, bundle: BootstrapBundle
) -> tuple[DriftRun, list[DriftSignal]]:
    analysis_time = max(unit.event_time for unit in bundle.plan.current)
    run_id = uuid5(NAMESPACE_URL, f"verdict-monitor-bootstrap-v1|{series.series_id}").hex
    signals = _drift_signals(
        bundle.report.results,
        run_id=run_id,
        detected_at=analysis_time,
        episode="historical bootstrap",
        current_units=bundle.plan.current,
        assignments=bundle.assignments,
        evaluator_fingerprint=_evaluator_for_scope(bundle.report.scope),
        evidence_layer=bundle.report.scope.evidence_layer,
    )
    evaluator_fingerprint = _evaluator_for_scope(bundle.report.scope)
    return (
        DriftRun(
            run_id=run_id,
            analysis_time=analysis_time,
            completed_at=datetime.now(timezone.utc),
            evaluator_fingerprint=evaluator_fingerprint,
            signal_count=len(signals),
        ),
        signals,
    )


def build_series_bootstrap_snapshot(
    storage: MonitorStorage,
    series: MonitorSeries,
    traces: list[Trace],
    *,
    judgments: list[Judgment] | tuple[Judgment, ...] = (),
) -> tuple[DriftRun, list[DriftSignal]]:
    """Build an activation snapshot from one candidate's frozen membership."""
    traces, missing = _load_required_traces(
        storage, traces, series.series_id, roles={"baseline", "bootstrap"}
    )
    if missing:
        raise ValueError("activated monitor bootstrap trace is missing")
    members = storage.list_monitor_members(series.series_id)
    trace_by_id = {trace.trace_id: trace for trace in traces}
    baseline = _units_for_members(
        [member for member in members if member.role == "baseline"], trace_by_id
    )
    current = _units_for_members(
        [member for member in members if member.role == "bootstrap"], trace_by_id
    )
    if not baseline or not current:
        raise ValueError("activated monitor bootstrap evidence is unavailable")
    assignments = {
        member.trace_id: member.cluster_id
        for member in members
        if member.role in {"baseline", "bootstrap"}
    }
    scope = _scope_from_json(series.scope_json)
    quality_scores = (
        _quality_scores(judgments, set(trace_by_id)).get(scope.evaluator_fingerprint or "")
        if scope.evidence_layer == "quality"
        else None
    )
    results, _ = compare_cohorts(baseline, current, assignments, quality_scores=quality_scores)
    analysis_time = max(unit.event_time for unit in current)
    run_id = uuid5(NAMESPACE_URL, f"verdict-monitor-bootstrap-v1|{series.series_id}").hex
    signals = _drift_signals(
        results,
        run_id=run_id,
        detected_at=analysis_time,
        episode="historical bootstrap",
        current_units=current,
        assignments=assignments,
        evaluator_fingerprint=_evaluator_for_scope(scope),
        evidence_layer=scope.evidence_layer,
    )
    evaluator_fingerprint = _evaluator_for_scope(scope)
    run = DriftRun(
        run_id=run_id,
        analysis_time=analysis_time,
        completed_at=datetime.now(timezone.utc),
        evaluator_fingerprint=evaluator_fingerprint,
        signal_count=len(signals),
    )
    return run, signals


def run_scheduled(
    storage: MonitorStorage,
    traces: list[Trace],
    *,
    judgments: list[Judgment] | tuple[Judgment, ...] = (),
    assignments: dict[str, str] | None = None,
) -> dict[str, object]:
    closed: list[dict[str, object]] = []
    blocked: list[dict[str, object]] = []
    late_total = 0
    changed_series = 0
    quality_by_evaluator = _quality_scores(judgments, {trace.trace_id for trace in traces})
    for series in storage.list_monitor_series():
        if series.state != "active":
            continue
        series_traces, missing = _load_required_traces(
            storage, traces, series.series_id, roles={"baseline"}
        )
        if missing:
            blocked.append(
                {
                    "series_id": series.series_id,
                    "reason": "baseline_evidence_missing",
                    "missing_traces": missing,
                }
            )
            continue
        for attempt in range(2):
            scope = _scope_from_json(series.scope_json)
            quality_scores = (
                quality_by_evaluator.get(scope.evaluator_fingerprint or "", {})
                if scope.evidence_layer == "quality"
                else None
            )
            scoped = [
                trace
                for trace in series_traces
                if _trace_matches_scope(trace, scope)
                and trace.started_at.tzinfo is not None
                and (trace.prompt_redacted or "").strip()
                and trace.tags.get("verdict.workload") != "judge"
                and (quality_scores is None or trace.trace_id in quality_scores)
            ]
            cycle = _plan_cycle(
                storage,
                series,
                scoped,
                quality_scores=quality_scores,
                assignments=assignments,
            )
            if not cycle["members"] and not cycle["results"]:
                break
            try:
                storage.commit_monitor_cycle(
                    series_id=series.series_id,
                    expected_generation=series.generation,
                    members=cycle["members"],
                    results=cycle["results"],
                    snapshots=cycle["snapshots"],
                    late_arrival_delta=cycle["late_arrivals"],
                )
            except ValueError as exc:
                if "generation conflict" not in str(exc) or attempt == 1:
                    raise
                refreshed = storage.get_monitor_series(series.series_id)
                if refreshed is None or refreshed.state != "active":
                    break
                series = refreshed
                continue
            changed_series += 1
            late_total += cycle["late_arrivals"]
            closed.extend(
                {
                    "series_id": result.series_id,
                    "cluster_id": result.cluster_id,
                    "bucket_index": result.bucket_index,
                    "episode_status": result.status,
                }
                for result in cycle["results"]
            )
            break
    return {
        "status": "updated" if changed_series else ("blocked" if blocked else "no_op"),
        "updated_series": changed_series,
        "closed_cohorts": len(closed),
        "late_arrivals": late_total,
        "blocked_series": blocked,
        "results": closed,
    }


def monitor_status(storage: MonitorStorage) -> dict[str, object]:
    series = storage.list_monitor_series()
    active = [item.series_id for item in series if item.state == "active"]
    now = datetime.now(timezone.utc)
    return {
        "active_series_id": active[0] if len(active) == 1 else None,
        "active_series_ids": active,
        "series": [
            {
                "series_id": item.series_id,
                "scope": json.loads(item.scope_json),
                "state": item.state,
                "generation": item.generation,
                "target_units": item.target_units,
                "late_arrival_count": item.late_arrival_count,
                "baseline_age_days": max(0, (now - item.created_at.astimezone(timezone.utc)).days),
                "refit_recommended": (now - item.created_at.astimezone(timezone.utc)).days >= 90,
                "result_count": len(storage.list_monitor_results(item.series_id)),
            }
            for item in series
        ],
    }


def _bootstrap_members(series: MonitorSeries, bundle: BootstrapBundle) -> list[MonitorMember]:
    members: list[MonitorMember] = []
    for role, units in (("baseline", bundle.plan.baseline), ("bootstrap", bundle.plan.current)):
        for unit in units:
            for trace in unit.traces:
                members.append(
                    MonitorMember(
                        series_id=series.series_id,
                        trace_id=trace.trace_id,
                        role=role,
                        bucket_index=0,
                        cluster_id=bundle.assignments.get(trace.trace_id, "new_intent"),
                        unit_id=unit.unit_id,
                        event_time=_event_time(trace),
                    )
                )
    if bundle.plan.excluded_middle is not None:
        unit = bundle.plan.excluded_middle
        for trace in unit.traces:
            members.append(
                MonitorMember(
                    series_id=series.series_id,
                    trace_id=trace.trace_id,
                    role="excluded",
                    bucket_index=0,
                    cluster_id="",
                    unit_id=unit.unit_id,
                    event_time=_event_time(trace),
                )
            )
    return members


def _plan_cycle(
    storage: MonitorStorage,
    series: MonitorSeries,
    traces: list[Trace],
    *,
    quality_scores: dict[str, dict[str, float]] | None = None,
    assignments: dict[str, str] | None = None,
) -> dict[str, object]:
    existing = storage.list_monitor_members(series.series_id)
    existing_ids = {member.trace_id for member in existing}
    existing_unit_ids = {member.unit_id for member in existing}
    unseen = [trace for trace in traces if trace.trace_id not in existing_ids]
    scope_payload = json.loads(series.scope_json)
    if scope_payload.get("boundary_method") == "session_first_event_v2":
        unseen_by_unit: dict[str, list[Trace]] = defaultdict(list)
        for trace in unseen:
            unseen_by_unit[trace.session_id or trace.trace_id].append(trace)
        bootstrap_late = [
            trace
            for unit_id, rows in unseen_by_unit.items()
            if unit_id in existing_unit_ids
            or (min(_event_time(trace) for trace in rows), unit_id)
            <= (series.boundary_time.astimezone(timezone.utc), series.boundary_trace_id)
            for trace in rows
        ]
    else:
        bootstrap_late = [
            trace
            for trace in unseen
            if (_event_time(trace), trace.trace_id)
            <= (series.boundary_time.astimezone(timezone.utc), series.boundary_trace_id)
        ]
    bootstrap_late_ids = {trace.trace_id for trace in bootstrap_late}
    prospective = [trace for trace in unseen if trace.trace_id not in bootstrap_late_ids]

    prospective.sort(key=lambda trace: (_event_time(trace), trace.trace_id))
    if assignments is None:
        registry = ClusterRegistry.from_json(series.registry_json)
        baseline_clusters = set(registry.ids)
        clusterer = StableIntentClusterer(
            HashingEmbedder(), threshold=0.5, freeze_after=1, registry=registry
        )
        cluster_ids = clusterer.assign([trace.prompt_redacted or "" for trace in prospective])
    else:
        baseline_clusters = {
            member.cluster_id
            for member in existing
            if member.role == "baseline" and member.cluster_id not in {"", "not_evaluable"}
        }
        cluster_ids = [assignments.get(trace.trace_id, "not_evaluable") for trace in prospective]
    results_so_far = storage.list_monitor_results(series.series_id)
    result_keys = {(result.cluster_id, result.bucket_index) for result in results_so_far}
    closed_unit_events: dict[tuple[str, str], datetime] = {}
    for member in existing:
        if member.role == "current" and (member.cluster_id, member.bucket_index) in result_keys:
            key = (member.cluster_id, member.unit_id)
            closed_unit_events[key] = min(
                closed_unit_events.get(key, datetime.max.replace(tzinfo=timezone.utc)),
                member.event_time.astimezone(timezone.utc),
            )
    closed_watermarks: dict[str, tuple[datetime, str]] = {}
    for (cluster_id, unit_id), event_time in closed_unit_events.items():
        closed_watermarks[cluster_id] = max(
            closed_watermarks.get(
                cluster_id, (datetime.min.replace(tzinfo=timezone.utc), "")
            ),
            (event_time, unit_id),
        )

    late = list(bootstrap_late)
    prospective_by_unit: dict[tuple[str, str], list[Trace]] = defaultdict(list)
    for trace, cluster_id in zip(prospective, cluster_ids, strict=True):
        cluster = (
            cluster_id
            if cluster_id in baseline_clusters or cluster_id == "not_evaluable"
            else "new_intent"
        )
        prospective_by_unit[(cluster, trace.session_id or trace.trace_id)].append(trace)
    for key, rows in list(prospective_by_unit.items()):
        cluster, unit_id = key
        watermark = closed_watermarks.get(cluster)
        if watermark is not None and (
            min(_event_time(trace) for trace in rows), unit_id
        ) <= watermark:
            late.extend(rows)
            prospective_by_unit.pop(key)

    new_members = [
        MonitorMember(
            series_id=series.series_id,
            trace_id=trace.trace_id,
            role="late",
            bucket_index=0,
            cluster_id="",
            unit_id=trace.session_id or trace.trace_id,
            event_time=_event_time(trace),
        )
        for trace in late
    ]
    current_members = [member for member in existing if member.role == "current"]
    by_bucket: dict[tuple[str, int], set[str]] = defaultdict(set)
    for member in current_members:
        by_bucket[(member.cluster_id, member.bucket_index)].add(member.unit_id)

    for cluster_id in sorted({cluster for cluster, _ in prospective_by_unit}):
        closed = [index for cluster, index in result_keys if cluster == cluster_id]
        bucket = max(closed, default=0) + 1
        while len(by_bucket[(cluster_id, bucket)]) >= series.target_units:
            bucket += 1
        units = sorted(
            (
                (unit_id, rows)
                for (cluster, unit_id), rows in prospective_by_unit.items()
                if cluster == cluster_id
            ),
            key=lambda item: (_event_time(item[1][0]), item[0]),
        )
        for unit_id, rows in units:
            if len(by_bucket[(cluster_id, bucket)]) >= series.target_units:
                bucket += 1
            by_bucket[(cluster_id, bucket)].add(unit_id)
            for trace in rows:
                new_members.append(
                    MonitorMember(
                        series_id=series.series_id,
                        trace_id=trace.trace_id,
                        role="current",
                        bucket_index=bucket,
                        cluster_id=cluster_id,
                        unit_id=unit_id,
                        event_time=_event_time(trace),
                    )
                )

    all_members = [*existing, *new_members]
    trace_by_id = {trace.trace_id: trace for trace in traces}
    snapshots: list[tuple[DriftRun, list[DriftSignal]]] = []
    results: list[MonitorResult] = []
    ready_keys = sorted(
        key
        for key, units in by_bucket.items()
        if key[0] != "not_evaluable"
        and len(units) >= series.target_units
        and key not in result_keys
    )
    for cluster_id, bucket_index in ready_keys:
        if cluster_id == "new_intent":
            current_units = _units_for_members(
                [
                    member
                    for member in all_members
                    if member.role == "current"
                    and member.cluster_id == cluster_id
                    and member.bucket_index == bucket_index
                ],
                trace_by_id,
            )
            completed_at = datetime.now(timezone.utc)
            run_id = uuid5(
                NAMESPACE_URL,
                f"verdict-monitor-run-v1|{series.series_id}|{cluster_id}|{bucket_index}",
            ).hex
            scope = _scope_from_json(series.scope_json)
            signal = _new_intent_signal(
                run_id,
                current_units,
                evaluator_fingerprint=_evaluator_for_scope(scope),
            )
            snapshots.append(
                (
                    DriftRun(
                        run_id=run_id,
                        analysis_time=signal.detected_at,
                        completed_at=completed_at,
                        evaluator_fingerprint=_evaluator_for_scope(scope),
                        signal_count=1,
                    ),
                    [signal],
                )
            )
            results.append(
                MonitorResult(
                    series_id=series.series_id,
                    cluster_id=cluster_id,
                    bucket_index=bucket_index,
                    run_id=run_id,
                    status="new_intent",
                    direction_key="new_intent:+",
                    completed_at=completed_at,
                )
            )
            continue
        baseline_units = _units_for_members(
            [
                member
                for member in all_members
                if member.role == "baseline" and member.cluster_id == cluster_id
            ],
            trace_by_id,
        )
        current_units = _units_for_members(
            [
                member
                for member in all_members
                if member.role == "current"
                and member.cluster_id == cluster_id
                and member.bucket_index == bucket_index
            ],
            trace_by_id,
        )
        assignments = {
            trace.trace_id: cluster_id
            for unit in (*baseline_units, *current_units)
            for trace in unit.traces
        }
        metric_results, status = compare_cohorts(
            baseline_units,
            current_units,
            assignments,
            quality_scores=quality_scores,
        )
        direction_key = "|".join(
            f"{result.metric}:{'+' if result.effect_size > 0 else '-'}"
            for result in metric_results
            if result.status is AnalysisStatus.DRIFT_DETECTED
        )
        previous = max(
            (
                result
                for result in results_so_far
                if result.cluster_id == cluster_id and result.bucket_index < bucket_index
            ),
            key=lambda result: result.bucket_index,
            default=None,
        )
        if direction_key:
            episode = (
                "confirmed"
                if previous
                and previous.status in {"candidate", "confirmed"}
                and previous.direction_key == direction_key
                else "candidate"
            )
        else:
            episode = status.value
        completed_at = datetime.now(timezone.utc)
        run_id = uuid5(
            NAMESPACE_URL,
            f"verdict-monitor-run-v1|{series.series_id}|{cluster_id}|{bucket_index}",
        ).hex
        signals = _drift_signals(
            metric_results,
            run_id=run_id,
            detected_at=max(unit.event_time for unit in current_units),
            episode=episode,
            current_units=current_units,
            assignments=assignments,
            evaluator_fingerprint=_evaluator_for_scope(_scope_from_json(series.scope_json)),
            evidence_layer=_scope_from_json(series.scope_json).evidence_layer,
        )
        run = DriftRun(
            run_id=run_id,
            analysis_time=max(unit.event_time for unit in current_units),
            completed_at=completed_at,
            evaluator_fingerprint=_evaluator_for_scope(_scope_from_json(series.scope_json)),
            signal_count=len(signals),
        )
        snapshots.append((run, signals))
        results.append(
            MonitorResult(
                series_id=series.series_id,
                cluster_id=cluster_id,
                bucket_index=bucket_index,
                run_id=run_id,
                status=episode,
                direction_key=direction_key,
                completed_at=completed_at,
            )
        )

    return {
        "members": new_members,
        "results": results,
        "snapshots": snapshots,
        "late_arrivals": len(late),
    }


def _units_for_members(
    members: list[MonitorMember], trace_by_id: dict[str, Trace]
) -> tuple[HistoryUnit, ...]:
    grouped: dict[str, list[Trace]] = defaultdict(list)
    for member in members:
        if trace := trace_by_id.get(member.trace_id):
            grouped[member.unit_id].append(trace)
    units = [
        HistoryUnit(
            unit_id,
            min(_event_time(trace) for trace in rows),
            tuple(sorted(rows, key=lambda trace: (_event_time(trace), trace.trace_id))),
        )
        for unit_id, rows in grouped.items()
    ]
    return tuple(sorted(units, key=lambda unit: (unit.event_time, unit.unit_id)))


def _load_required_traces(
    storage: MonitorStorage,
    traces: list[Trace],
    series_id: str,
    *,
    roles: set[str],
) -> tuple[list[Trace], int]:
    """Restore frozen evidence omitted by a caller's normal trace limit."""
    by_id = {trace.trace_id: trace for trace in traces}
    required = {
        member.trace_id
        for member in storage.list_monitor_members(series_id)
        if member.role in roles
    }
    get_trace = getattr(storage, "get_trace", None)
    if callable(get_trace):
        for trace_id in sorted(required - by_id.keys()):
            if trace := get_trace(trace_id):
                by_id[trace_id] = trace
    missing = len(required - by_id.keys())
    return list(by_id.values()), missing


def _drift_signals(
    results,
    *,
    run_id: str,
    detected_at: datetime,
    episode: str,
    current_units: tuple[HistoryUnit, ...],
    assignments: dict[str, str],
    evaluator_fingerprint: str,
    evidence_layer: str,
) -> list[DriftSignal]:
    example_ids = [unit.traces[0].trace_id for unit in current_units[:5]]
    signals: list[DriftSignal] = []
    for result in results:
        if result.status is not AnalysisStatus.DRIFT_DETECTED:
            continue
        if result.metric == "new_intent_traffic":
            new_intent_units = tuple(
                unit
                for unit in current_units
                if any(assignments.get(trace.trace_id) == "new_intent" for trace in unit.traces)
            )
            if new_intent_units:
                signals.append(
                    _new_intent_signal(
                        run_id,
                        new_intent_units,
                        evaluator_fingerprint=evaluator_fingerprint,
                    )
                )
            continue
        signal_id = uuid5(
            NAMESPACE_URL,
            f"verdict-monitor-signal-v1|{run_id}|{result.cluster_id}|{result.metric}",
        ).hex
        signals.append(
            DriftSignal(
                signal_id=signal_id,
                detected_at=detected_at,
                cluster_id=result.cluster_id,
                dimension=result.metric,
                direction=_direction_for_metric(result.metric, result.effect_size),
                statistic_name="mann_whitney_u",
                statistic_value=result.effect_size,
                p_value=result.p_value,
                p_value_adjusted=result.p_value_adjusted,
                effect_size_cliffs_delta=result.effect_size,
                sample_size_current=result.current_n,
                sample_size_baseline=result.baseline_n,
                contributing_layers=[evidence_layer],
                example_trace_ids=example_ids,
                recommended_action=f"{episode} count-cohort structural change",
                evaluator_fingerprint=evaluator_fingerprint,
                run_id=run_id,
            )
        )
    return signals


def _new_intent_signal(
    run_id: str,
    current_units: tuple[HistoryUnit, ...],
    *,
    evaluator_fingerprint: str = STRUCTURAL_EVALUATOR,
) -> DriftSignal:
    detected_at = max(unit.event_time for unit in current_units)
    return DriftSignal(
        signal_id=uuid5(NAMESPACE_URL, f"verdict-monitor-signal-v1|{run_id}|new-intent").hex,
        detected_at=detected_at,
        cluster_id="new_intent",
        dimension="new_intent_traffic",
        direction=DriftDirection.CHANGE,
        statistic_name="novel_intent_count",
        statistic_value=float(len(current_units)),
        p_value=1.0,
        p_value_adjusted=1.0,
        effect_size_cliffs_delta=0.0,
        sample_size_current=len(current_units),
        sample_size_baseline=0,
        contributing_layers=["cluster_coverage"],
        example_trace_ids=[unit.traces[0].trace_id for unit in current_units[:5]],
        recommended_action=(
            "Review these unmatched intents and refit the baseline if they are expected traffic."
        ),
        evaluator_fingerprint=evaluator_fingerprint,
        run_id=run_id,
    )


def _scope_from_json(payload: str) -> ScopeKey:
    values = json.loads(payload)
    return ScopeKey(
        values.get("tenant_id"),
        values["workload"],
        values["granularity"],
        values.get("evidence_layer", "structural"),
        values.get("evaluator_fingerprint"),
    )


def _trace_matches_scope(trace: Trace, scope: ScopeKey) -> bool:
    trace_scope = scope_for_trace(trace)
    return (
        trace_scope.tenant_id,
        trace_scope.workload,
        trace_scope.granularity,
    ) == (scope.tenant_id, scope.workload, scope.granularity)


def _evaluator_for_scope(scope: ScopeKey) -> str:
    if scope.evidence_layer == "quality":
        if not scope.evaluator_fingerprint:
            raise ValueError("quality monitor scope is missing evaluator identity")
        return scope.evaluator_fingerprint
    return STRUCTURAL_EVALUATOR


def _direction_for_metric(metric: str, effect_size: float) -> DriftDirection:
    if metric.endswith(".pass_rate"):
        return DriftDirection.REGRESSION if effect_size < 0 else DriftDirection.IMPROVEMENT
    if metric.endswith((".missing_rate", ".unclear_rate")):
        return DriftDirection.REGRESSION if effect_size > 0 else DriftDirection.IMPROVEMENT
    if metric in {"error_rate", "refusal_rate", "latency_ms", "output_tokens"}:
        return DriftDirection.REGRESSION if effect_size > 0 else DriftDirection.IMPROVEMENT
    return DriftDirection.CHANGE


def _event_time(trace: Trace) -> datetime:
    if trace.started_at.tzinfo is None:
        raise ValueError(f"trace {trace.trace_id!r} has no event-time offset")
    return trace.started_at.astimezone(timezone.utc)
