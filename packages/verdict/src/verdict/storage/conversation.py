"""Shared row mapping for built-in conversation-drift storage adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime

from verdict.schema import ConversationDriftRun, ConversationDriftSample, ConversationDriftSignal

RUN_COLUMNS = (
    "tenant_id,run_id,registry_version,method,analysis_policy_json,"
    "analysis_policy_fingerprint,evaluator_definition_json,evaluator_fingerprint,"
    "target_workload,baseline_start,baseline_end,current_start,current_end,"
    "analysis_cutoff,status,unavailable_reason,coverage_json,signal_count,"
    "sample_count,started_at,completed_at,actor"
)
SAMPLE_COLUMNS = (
    'tenant_id,run_id,registry_version,cluster_id,session_ordinal,"window",trace_id,'
    "event_time,attempt_terminal_at,attempt_status,error_category,judgment_id,"
    "legacy_write_status,outcomes_json"
)
SIGNAL_COLUMNS = (
    "tenant_id,run_id,signal_id,registry_version,cluster_id,dimension,direction,"
    "statistic_name,statistic_value,p_value,p_value_adjusted,effect_size,"
    "sample_size_current,sample_size_baseline,examples_json,recommended_action"
)


def native_time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("conversation timestamp is invalid")
    return value


def _json(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def run_from_row(
    row: Sequence[object], parse_time: Callable[[object], datetime]
) -> ConversationDriftRun:
    return ConversationDriftRun(
        tenant_id=str(row[0]), run_id=str(row[1]), registry_version=str(row[2]), method=str(row[3]),
        analysis_policy_json=_json(row[4]), analysis_policy_fingerprint=str(row[5]),
        evaluator_definition_json=_json(row[6]), evaluator_fingerprint=str(row[7]),
        target_workload=str(row[8]), baseline_start=parse_time(row[9]),
        baseline_end=parse_time(row[10]), current_start=parse_time(row[11]),
        current_end=parse_time(row[12]), analysis_cutoff=parse_time(row[13]), status=str(row[14]),
        unavailable_reason=None if row[15] is None else str(row[15]), coverage_json=_json(row[16]),
        signal_count=int(row[17]), sample_count=int(row[18]), started_at=parse_time(row[19]),
        completed_at=parse_time(row[20]), actor=str(row[21]),
    )


def sample_from_row(
    row: Sequence[object], parse_time: Callable[[object], datetime]
) -> ConversationDriftSample:
    return ConversationDriftSample(
        tenant_id=str(row[0]), run_id=str(row[1]), registry_version=str(row[2]),
        cluster_id=str(row[3]), session_ordinal=int(row[4]), window=str(row[5]), trace_id=str(row[6]),
        event_time=parse_time(row[7]), attempt_terminal_at=parse_time(row[8]),
        attempt_status=str(row[9]), error_category=None if row[10] is None else str(row[10]),
        judgment_id=None if row[11] is None else str(row[11]), legacy_write_status=str(row[12]),
        outcomes_json=_json(row[13]),
    )


def signal_from_row(row: Sequence[object]) -> ConversationDriftSignal:
    return ConversationDriftSignal(
        tenant_id=str(row[0]), run_id=str(row[1]), signal_id=str(row[2]),
        registry_version=str(row[3]), cluster_id=str(row[4]), dimension=str(row[5]),
        direction=str(row[6]), statistic_name=str(row[7]), statistic_value=float(row[8]),
        p_value=float(row[9]), p_value_adjusted=float(row[10]), effect_size=float(row[11]),
        sample_size_current=int(row[12]), sample_size_baseline=int(row[13]),
        examples_json=_json(row[14]), recommended_action=str(row[15]),
    )


def run_values(run: ConversationDriftRun, encode_time: Callable[[datetime], object]) -> tuple[object, ...]:
    return (
        run.tenant_id, run.run_id, run.registry_version, run.method, run.analysis_policy_json,
        run.analysis_policy_fingerprint, run.evaluator_definition_json, run.evaluator_fingerprint,
        run.target_workload, encode_time(run.baseline_start), encode_time(run.baseline_end),
        encode_time(run.current_start), encode_time(run.current_end), encode_time(run.analysis_cutoff),
        run.status, run.unavailable_reason, run.coverage_json, run.signal_count, run.sample_count,
        encode_time(run.started_at), encode_time(run.completed_at), run.actor,
    )


def sample_values(
    item: ConversationDriftSample, encode_time: Callable[[datetime], object]
) -> tuple[object, ...]:
    return (
        item.tenant_id, item.run_id, item.registry_version, item.cluster_id, item.session_ordinal,
        item.window, item.trace_id, encode_time(item.event_time), encode_time(item.attempt_terminal_at),
        item.attempt_status, item.error_category, item.judgment_id, item.legacy_write_status,
        item.outcomes_json,
    )


def signal_values(item: ConversationDriftSignal) -> tuple[object, ...]:
    return (
        item.tenant_id, item.run_id, item.signal_id, item.registry_version, item.cluster_id,
        item.dimension, item.direction, item.statistic_name, item.statistic_value, item.p_value,
        item.p_value_adjusted, item.effect_size, item.sample_size_current,
        item.sample_size_baseline, item.examples_json, item.recommended_action,
    )
