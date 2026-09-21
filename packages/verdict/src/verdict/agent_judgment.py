"""Native Agent Turn evaluation identity and bounded, redacted results."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from verdict.evidence import AgentTurn, EvidenceState, ExecutionStatus
from verdict.redaction import redact, redact_structure
from verdict.schema import DimensionScore, JudgmentStatus, Verdict

_MAX_RESULT_BYTES = 65_536
MAX_TURN_SCAN = 100


class AgentTurnJudgmentStoreError(RuntimeError):
    """A persisted Turn result cannot be safely interpreted."""


_DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def turn_evidence_fingerprint(turn: AgentTurn) -> str:
    """Bind a result to the exact redacted evidence and eligibility state."""
    payload = [
        turn.status.value, turn.request_state.value, turn.response_state.value,
        turn.user_request_redacted, turn.final_response_redacted,
        turn.request_truncated, turn.response_truncated,
    ]
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def turn_evidence_reason(turn: AgentTurn) -> str | None:
    if turn.status is not ExecutionStatus.COMPLETED:
        return "turn_not_completed"
    if turn.request_truncated or turn.response_truncated:
        return "evidence_truncated"
    if turn.request_state is not EvidenceState.PRESENT or not (
        turn.user_request_redacted or ""
    ).strip():
        return "request_unavailable"
    if turn.response_state is not EvidenceState.PRESENT or not (
        turn.final_response_redacted or ""
    ).strip():
        return "response_unavailable"
    return None


@dataclass
class AgentTurnJudgment:
    tenant_id: str
    run_id: str
    turn_id: str
    evaluator_fingerprint: str
    evidence_fingerprint: str
    evaluator_provider: str
    evaluator_config: dict[str, Any]
    judge_models: list[str]
    expected_dimensions: list[str]
    rubric_name: str
    rubric_version: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    status: JudgmentStatus = JudgmentStatus.COMPLETED
    error: str | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not isinstance(self.status, JudgmentStatus):
            self.status = JudgmentStatus(self.status)
        for value in (self.tenant_id, self.run_id, self.turn_id):
            if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
                raise ValueError("invalid Agent Turn judgment identity")
        for digest in (self.evaluator_fingerprint, self.evidence_fingerprint):
            if not isinstance(digest, str) or len(digest) != 64 or any(
                char not in "0123456789abcdef" for char in digest
            ):
                raise ValueError("invalid Agent Turn judgment fingerprint")
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluation time must have a timezone")
        if self.status is JudgmentStatus.ERROR and self.dimensions:
            raise ValueError("error judgment cannot contain scores")
        if (
            not isinstance(self.expected_dimensions, list)
            or len(self.expected_dimensions) > 12
            or any(not isinstance(name, str) or not _DIMENSION_NAME.fullmatch(name)
                   for name in self.expected_dimensions)
            or len(set(self.expected_dimensions)) != len(self.expected_dimensions)
        ):
            raise ValueError("invalid expected dimensions")
        if not isinstance(self.dimensions, list) or len(self.dimensions) > 12 or any(
            not isinstance(dimension, DimensionScore)
            or dimension.name not in self.expected_dimensions
            for dimension in self.dimensions
        ):
            raise ValueError("invalid scored dimensions")
        if self.status is JudgmentStatus.COMPLETED and (
            not self.expected_dimensions
            or len(self.dimensions) != len(self.expected_dimensions)
            or {dimension.name for dimension in self.dimensions} != set(self.expected_dimensions)
        ):
            raise ValueError("completed Turn judgment needs one score per expected dimension")
        if not isinstance(self.rubric_name, str) or not _DIMENSION_NAME.fullmatch(self.rubric_name):
            raise ValueError("invalid rubric name")
        if not isinstance(self.rubric_version, str) or len(self.rubric_version.encode("utf-8")) > 64:
            raise ValueError("invalid rubric version")
        if not isinstance(self.judge_models, list) or len(self.judge_models) > 8 or any(
            not isinstance(model, str) or not model or len(model.encode("utf-8")) > 256
            for model in self.judge_models
        ):
            raise ValueError("invalid judge models")
        self.evaluator_config = redact_structure(self.evaluator_config)
        if not isinstance(self.evaluator_config, dict):
            raise ValueError("invalid evaluator config")
        self.evaluator_provider = redact(self.evaluator_provider) or ""
        self.rubric_version = redact(self.rubric_version) or ""
        self.judge_models = [redact(model) or "" for model in self.judge_models]
        self.error = redact(self.error)
        for dimension in self.dimensions:
            dimension.reasoning = redact(dimension.reasoning) or ""
            dimension.judge_model = redact(dimension.judge_model) or ""
        if len(agent_turn_judgment_to_json(self).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("Agent Turn judgment exceeds size limit")


def agent_turn_judgment_to_json(result: AgentTurnJudgment) -> str:
    payload = asdict(result)
    payload["status"] = result.status.value
    payload["evaluated_at"] = result.evaluated_at.isoformat()
    for dimension in payload["dimensions"]:
        dimension["verdict"] = Verdict(dimension["verdict"]).value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def agent_turn_judgment_from_json(raw: str | dict[str, Any]) -> AgentTurnJudgment:
    payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    payload["dimensions"] = [DimensionScore(**dimension) for dimension in payload["dimensions"]]
    payload["evaluated_at"] = datetime.fromisoformat(payload["evaluated_at"])
    return AgentTurnJudgment(**payload)


def sanitized_turn_judgment(result: AgentTurnJudgment) -> tuple[AgentTurnJudgment, str]:
    """Revalidate mutable score fields at the final persistence boundary."""
    clean = agent_turn_judgment_from_json(agent_turn_judgment_to_json(result))
    return clean, agent_turn_judgment_to_json(clean)


def parse_stored_turn_judgment(raw: str) -> AgentTurnJudgment:
    try:
        return agent_turn_judgment_from_json(raw)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AgentTurnJudgmentStoreError("invalid persisted Turn result") from exc


def trusted_turn_judgment(
    raw: str | None, *, tenant_id: str, run_id: str, turn_id: str,
    evaluator_fingerprint: str, evidence_fingerprint: str | None = None,
    status: str | None = None,
) -> AgentTurnJudgment | None:
    """Ignore a malformed or mismatched stored slot; it is not a completed score."""
    if raw is None:
        return None
    try:
        result = parse_stored_turn_judgment(raw)
    except AgentTurnJudgmentStoreError:
        return None
    if (
        (result.tenant_id, result.run_id, result.turn_id, result.evaluator_fingerprint)
        != (tenant_id, run_id, turn_id, evaluator_fingerprint)
        or (evidence_fingerprint is not None and result.evidence_fingerprint != evidence_fingerprint)
        or (status is not None and result.status.value != status)
    ):
        return None
    return result


def validate_turn_scan(
    tenant_id: str, evaluator_fingerprint: str, limit: int,
    before: tuple[datetime, str, str] | None,
) -> None:
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id.encode("utf-8")) > 256:
        raise ValueError("invalid tenant")
    if not isinstance(evaluator_fingerprint, str) or len(evaluator_fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in evaluator_fingerprint
    ):
        raise ValueError("invalid evaluator fingerprint")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TURN_SCAN:
        raise ValueError("turn scan limit must be 1-100")
    if before is not None and (
        not isinstance(before, tuple) or len(before) != 3
        or not isinstance(before[0], datetime) or before[0].tzinfo is None
        or any(not isinstance(v, str) or not v or len(v.encode("utf-8")) > 256 for v in before[1:])
    ):
        raise ValueError("invalid turn scan cursor")


def turn_judgment_write_decision(
    turn: AgentTurn | None, incoming: AgentTurnJudgment,
    previous: AgentTurnJudgment | None,
) -> str:
    if turn is None or turn_evidence_reason(turn) is not None or (
        turn_evidence_fingerprint(turn) != incoming.evidence_fingerprint
    ):
        return "stale"
    if previous is not None and previous.evidence_fingerprint == incoming.evidence_fingerprint:
        if previous.status is JudgmentStatus.COMPLETED:
            return "already_completed"
        if incoming.status is JudgmentStatus.ERROR:
            return "already_error"
    return "saved"
