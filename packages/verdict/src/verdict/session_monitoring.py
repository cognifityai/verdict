"""Retrospective descriptive monitoring over native logical Agent sessions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from verdict.agent_judgment import (
    AgentTurnJudgment,
    TurnToolCounts,
    agent_turn_judgment_to_json,
    turn_evidence_fingerprint,
    turn_evidence_reason,
)
from verdict.evidence import (
    AgentRunBundle,
    AgentTurn,
    ExecutionStatus,
)
from verdict.monitoring import judgment_metric_states

MAX_LOGICAL_SESSION_MONITOR_RUNS = 1_000
MAX_LOGICAL_SESSION_MONITOR_TURNS = 10_000
MAX_LOGICAL_SESSION_MONITOR_EVENTS = 100_000
MAX_LOGICAL_SESSION_MONITOR_TURN_TEXT_BYTES = 16_777_216
MAX_LOGICAL_SESSION_MONITOR_JUDGMENT_BYTES = 16_777_216
MAX_AGENT_EVALUATOR_IDENTITY_CANDIDATES = 1_000
TEXT_STRIP_CHARACTERS = "".join(chr(value) for value in (
    *range(0x09, 0x0E),
    *range(0x1C, 0x21),
    0x85,
    0xA0,
    0x1680,
    *range(0x2000, 0x200B),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
))
_TERMINAL_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.TIMED_OUT,
    ExecutionStatus.CANCELLED,
}
_STATE_PRECEDENCE = ("fail", "error", "unclear", "missing", "pass")


@dataclass(frozen=True, slots=True)
class LogicalSessionTurnEvidence:
    run_id: str
    turn_id: str
    status: ExecutionStatus
    final_output_present: bool
    evidence_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class LogicalSessionRunEvidence:
    run_id: str
    tenant_id: str
    session_id: str | None
    started_at: datetime
    status: ExecutionStatus
    turns: tuple[LogicalSessionTurnEvidence, ...]


@dataclass(frozen=True, slots=True)
class AgentTurnEvaluatorIdentityPage:
    judgments: tuple[AgentTurnJudgment, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class LogicalSessionEvidence:
    """One bounded snapshot with no Turn text or Agent Event bodies."""

    runs: tuple[LogicalSessionRunEvidence, ...]
    judgments: tuple[AgentTurnJudgment, ...]

    def __post_init__(self) -> None:
        if len(self.runs) > MAX_LOGICAL_SESSION_MONITOR_RUNS:
            raise ValueError("logical-session preview exceeds bounded run limit")
        if sum(len(run.turns) for run in self.runs) > MAX_LOGICAL_SESSION_MONITOR_TURNS:
            raise ValueError("logical-session preview exceeds bounded Turn limit")
        if len(self.judgments) > MAX_LOGICAL_SESSION_MONITOR_TURNS:
            raise ValueError("logical-session preview exceeds bounded judgment limit")
        judgment_bytes = sum(
            len(agent_turn_judgment_to_json(judgment).encode("utf-8"))
            for judgment in self.judgments
        )
        if judgment_bytes > MAX_LOGICAL_SESSION_MONITOR_JUDGMENT_BYTES:
            raise ValueError("logical-session preview exceeds bounded judgment limit")


@dataclass(frozen=True, slots=True)
class LogicalSessionPreview:
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.payload


@dataclass(frozen=True, slots=True)
class _SessionUnit:
    unit_id: str
    event_time: datetime
    metrics: dict[str, bool]
    metric_states: dict[str, str]


def logical_session_turn_evidence(
    turn: AgentTurn,
    *,
    calculate_evidence: bool,
    tool_counts: TurnToolCounts | None = None,
) -> LogicalSessionTurnEvidence:
    fingerprint = None
    if calculate_evidence and turn_evidence_reason(turn) is None and (
        tool_counts is None or tool_counts.unavailable_reason is None
    ):
        fingerprint = turn_evidence_fingerprint(turn, tool_counts)
    return LogicalSessionTurnEvidence(
        run_id=turn.run_id,
        turn_id=turn.turn_id,
        status=turn.status,
        final_output_present=(
            turn.response_state.value == "present"
            and bool((turn.final_response_redacted or "").strip(TEXT_STRIP_CHARACTERS))
        ),
        evidence_fingerprint=fingerprint,
    )


def logical_session_run_evidence(
    bundle: AgentRunBundle,
    *,
    calculate_evidence: bool,
    tool_counts: dict[tuple[str, str], TurnToolCounts] | None = None,
) -> LogicalSessionRunEvidence:
    counts = tool_counts or {}
    return LogicalSessionRunEvidence(
        run_id=bundle.run.run_id,
        tenant_id=bundle.run.tenant_id,
        session_id=bundle.run.session_id,
        started_at=bundle.run.started_at,
        status=bundle.run.status,
        turns=tuple(
            logical_session_turn_evidence(
                turn,
                calculate_evidence=calculate_evidence,
                tool_counts=counts.get((turn.run_id, turn.turn_id)),
            )
            for turn in bundle.turns
        ),
    )


def _session_unit_id(tenant_id: str, session_id: str) -> str:
    encoded = json.dumps(
        ["monitor-logical-session-v1", tenant_id, session_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return f"logical_session:{hashlib.sha256(encoded).hexdigest()}"


def _rate_summary(units: list[_SessionUnit]) -> dict[str, Any]:
    return {"unitCount": len(units)}


def _metric_counts(units: list[_SessionUnit], metric: str) -> dict[str, int]:
    result = {
        "true": 0,
        "false": 0,
        "unclear": 0,
        "missing": 0,
        "error": 0,
    }
    for unit in units:
        if metric in unit.metrics:
            result["true" if unit.metrics[metric] else "false"] += 1
        else:
            result[unit.metric_states.get(metric, "missing")] += 1
    return result


def _metric_result(
    metric: str,
    reference: list[_SessionUnit],
    current: list[_SessionUnit],
) -> dict[str, Any]:
    reference_counts = _metric_counts(reference, metric)
    current_counts = _metric_counts(current, metric)
    reference_n = reference_counts["true"] + reference_counts["false"]
    current_n = current_counts["true"] + current_counts["false"]
    reference_value = reference_counts["true"] / reference_n if reference_n else None
    current_value = current_counts["true"] / current_n if current_n else None
    return {
        "metric": metric,
        "referenceEvaluable": reference_n,
        "referenceUnclear": reference_counts["unclear"],
        "referenceMissing": reference_counts["missing"],
        "referenceError": reference_counts["error"],
        "currentEvaluable": current_n,
        "currentUnclear": current_counts["unclear"],
        "currentMissing": current_counts["missing"],
        "currentError": current_counts["error"],
        "referenceValue": reference_value,
        "currentValue": current_value,
        "effect": (
            current_value - reference_value
            if reference_value is not None and current_value is not None
            else None
        ),
        "pValue": None,
        "alert": None,
    }


def _selected_judgments(
    evidence: LogicalSessionEvidence,
    evaluator_fingerprint: str | None,
    evaluator_dimensions: tuple[str, ...],
    tenant_id: str,
) -> dict[tuple[str, str], AgentTurnJudgment]:
    if evaluator_fingerprint is None:
        return {}
    selected = [
        judgment
        for judgment in evidence.judgments
        if judgment.evaluator_fingerprint == evaluator_fingerprint
    ]
    if any(judgment.tenant_id != tenant_id for judgment in selected):
        raise ValueError("logical-session judgments crossed tenant scope")
    if any(tuple(judgment.expected_dimensions) != evaluator_dimensions for judgment in selected):
        raise ValueError("selected Agent Turn evaluator dimensions are inconsistent")
    identities = {
        (
            judgment.evaluator_provider,
            json.dumps(
                judgment.evaluator_config,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            tuple(judgment.judge_models),
            tuple(judgment.expected_dimensions),
            judgment.rubric_name,
            judgment.rubric_version,
        )
        for judgment in selected
    }
    if len(identities) > 1:
        raise ValueError("selected Agent Turn evaluator identity is inconsistent")
    result: dict[tuple[str, str], AgentTurnJudgment] = {}
    for judgment in selected:
        identity = (judgment.run_id, judgment.turn_id)
        if identity in result:
            raise ValueError("duplicate Agent Turn judgment identity")
        result[identity] = judgment
    return result


def preview_logical_sessions(
    evidence: LogicalSessionEvidence,
    *,
    tenant_id: str,
    reference_ratio: float = 0.8,
    evaluator_fingerprint: str | None = None,
    evaluator_dimensions: tuple[str, ...] = (),
    reference_start: datetime | None = None,
    reference_end: datetime | None = None,
    current_start: datetime | None = None,
    current_end: datetime | None = None,
) -> LogicalSessionPreview:
    """Compare logical-session rates without inferential claims or persistence."""
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id.encode()) > 256:
        raise ValueError("tenant_id must be bounded text")
    if not 0.5 <= reference_ratio < 1:
        raise ValueError("reference_ratio must be between 0.5 and 1")
    explicit = any(
        value is not None
        for value in (
            reference_start,
            reference_end,
            current_start,
            current_end,
        )
    )
    if explicit:
        boundaries = (reference_start, reference_end, current_start, current_end)
        if any(value is None or value.tzinfo is None for value in boundaries):
            raise ValueError("explicit windows require four timezone-aware boundaries")
        assert all(value is not None for value in boundaries)
        if not reference_start < reference_end <= current_start < current_end:
            raise ValueError("explicit windows must be ordered and non-overlapping")
    if evaluator_fingerprint is None and evaluator_dimensions:
        raise ValueError("evaluator dimensions require an evaluator fingerprint")
    if evaluator_fingerprint is not None and (
        not isinstance(evaluator_fingerprint, str)
        or len(evaluator_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in evaluator_fingerprint)
        or not evaluator_dimensions
    ):
        raise ValueError("selected Agent Turn evaluator identity is invalid")

    judgments = _selected_judgments(
        evidence,
        evaluator_fingerprint,
        evaluator_dimensions,
        tenant_id,
    )
    sessions: dict[str, list[LogicalSessionRunEvidence]] = {}
    missing_session = 0
    for run in evidence.runs:
        if run.tenant_id != tenant_id:
            raise ValueError("logical-session evidence crossed tenant scope")
        session_id = run.session_id
        if session_id is None:
            missing_session += 1
            continue
        sessions.setdefault(session_id, []).append(run)

    units: list[_SessionUnit] = []
    in_progress = 0
    for session_id, runs in sessions.items():
        runs.sort(key=lambda item: (item.started_at, item.run_id))
        turns = [turn for run in runs for turn in run.turns]
        if any(run.status not in _TERMINAL_STATUSES for run in runs) or any(
            turn.status not in _TERMINAL_STATUSES for turn in turns
        ):
            in_progress += 1
            continue
        metrics = {
            "agent.execution_completed": all(
                run.status is ExecutionStatus.COMPLETED for run in runs
            )
            and all(turn.status is ExecutionStatus.COMPLETED for turn in turns),
        }
        states = {
            "agent.execution_completed": (
                "pass" if metrics["agent.execution_completed"] else "fail"
            ),
        }
        if turns:
            output_present = all(turn.final_output_present for turn in turns)
            metrics["agent.final_output_present"] = output_present
            states["agent.final_output_present"] = "pass" if output_present else "fail"
        else:
            states["agent.final_output_present"] = "missing"

        judged_turn_states: list[dict[str, str]] = []
        if evaluator_dimensions:
            for run in runs:
                for turn in run.turns:
                    current = judgments.get((turn.run_id, turn.turn_id))
                    if current is None or (
                        turn.evidence_fingerprint is None
                        or current.evidence_fingerprint != turn.evidence_fingerprint
                    ):
                        judged_turn_states.append(
                            {dimension: "missing" for dimension in evaluator_dimensions}
                        )
                    else:
                        judged_turn_states.append(
                            judgment_metric_states(
                                current,
                                evaluator_dimensions,
                            )
                        )

        for dimension in evaluator_dimensions:
            metric = f"judge.{dimension}.pass"
            turn_states = [states[dimension] for states in judged_turn_states]
            state = next(
                (candidate for candidate in _STATE_PRECEDENCE if candidate in turn_states),
                "missing",
            )
            states[metric] = state
            if state in {"pass", "fail"}:
                metrics[metric] = state == "pass"

        units.append(
            _SessionUnit(
                _session_unit_id(tenant_id, session_id),
                min(run.started_at for run in runs),
                metrics,
                states,
            )
        )
    units.sort(key=lambda item: (item.event_time, item.unit_id))

    if explicit:
        assert reference_start and reference_end and current_start and current_end
        reference = [unit for unit in units if reference_start <= unit.event_time < reference_end]
        current = [unit for unit in units if current_start <= unit.event_time < current_end]
        window_mode = "explicit"
    else:
        boundary = int(len(units) * reference_ratio)
        reference, current = units[:boundary], units[boundary:]
        window_mode = "count"
    metric_names = {
        "agent.execution_completed",
        "agent.final_output_present",
        *(f"judge.{dimension}.pass" for dimension in evaluator_dimensions),
    }
    return LogicalSessionPreview(
        {
            "state": "descriptive",
            "analysisUnit": "logical_session",
            "observationalUnitLabel": "logical sessions",
            "observationalUnits": len(units),
            "inferential": False,
            "activationAllowed": False,
            "samplingAssumption": "independence_not_established",
            "windowMode": window_mode,
            "asOf": datetime.now(timezone.utc).isoformat(),
            "reference": _rate_summary(reference),
            "current": _rate_summary(current),
            "metrics": [
                _metric_result(metric, reference, current)
                for metric in sorted(metric_names)
            ],
            "coverage": {
                "runs": len(evidence.runs),
                "runsMissingLogicalSession": missing_session,
                "sessionsInProgress": in_progress,
                "judgments": len(judgments),
            },
            "evaluatorFingerprint": evaluator_fingerprint,
            "evaluatorDimensions": list(evaluator_dimensions),
        }
    )
