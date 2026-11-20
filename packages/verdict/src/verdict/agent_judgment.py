"""Native Agent Turn evaluation identity and bounded, redacted results."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import pairwise
from typing import Any, ClassVar, NamedTuple

from verdict.evidence import AgentTurn, EvidenceState, ExecutionStatus
from verdict.redaction import redact, redact_structure
from verdict.schema import DimensionScore, JudgmentStatus, Verdict

_MAX_RESULT_BYTES = 65_536
MAX_TURN_SCAN = 100
MAX_TOOL_EVIDENCE_EVENTS = 64
TOOL_EVIDENCE_MODE = "counts_v1"
NEXT_MESSAGE_MODE = "next_user_message_v1"


class JudgeCallsHalted(Exception):
    """A provider wrapper refused to send any further judge call in this run."""


class AgentTurnJudgmentStoreError(RuntimeError):
    """A persisted Turn result cannot be safely interpreted."""


_DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class TurnToolCounts:
    """Metadata-only projection; no event content or identifiers can escape."""

    MODE: ClassVar[str] = TOOL_EVIDENCE_MODE
    PROMPT_TEMPLATE: ClassVar[str] = (
        "Recorded tool calls: {calls}\n"
        "Recorded tool results: {results}\n"
        "Recorded error results: {error_results}\n"
        "Results with unknown error status: {unknown_results}\n"
        "Directly recorded MCP dispatches: {mcp_calls}\n"
        "Directly recorded application dispatches: {application_calls}\n"
        "Directly recorded provider-hosted dispatches: {provider_hosted_calls}\n"
        "Origin not captured: {origin_not_captured_calls}\n"
        "Origin unusable: {origin_unusable_calls}\n"
        "Origin labels describe only the producer-recorded outer dispatch boundary. "
        "They do not establish whether a tool used an unobserved downstream service or protocol. "
        "Calls with origin not captured or unusable may have any origin."
    )

    event_count: int = 0
    calls: int = 0
    results: int = 0
    error_results: int = 0
    unknown_results: int = 0
    mcp_calls: int = 0
    application_calls: int = 0
    provider_hosted_calls: int = 0
    origin_not_captured_calls: int = 0
    origin_unusable_calls: int = 0

    def __post_init__(self) -> None:
        values = (
            self.event_count,
            self.calls,
            self.results,
            self.error_results,
            self.unknown_results,
            self.mcp_calls,
            self.application_calls,
            self.provider_hosted_calls,
            self.origin_not_captured_calls,
            self.origin_unusable_calls,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values):
            raise ValueError("tool counts must be non-negative integers")
        origin_total = (
            self.mcp_calls
            + self.application_calls
            + self.provider_hosted_calls
            + self.origin_not_captured_calls
            + self.origin_unusable_calls
        )
        # Preserve source compatibility for callers constructing the pre-origin
        # counts shape while representing the missing classification honestly.
        if self.calls and origin_total == 0:
            object.__setattr__(self, "origin_not_captured_calls", self.calls)
        elif origin_total != self.calls:
            raise ValueError("tool origin counts must partition recorded calls")

    @property
    def unavailable_reason(self) -> str | None:
        if self.event_count > MAX_TOOL_EVIDENCE_EVENTS:
            return "tool_evidence_limit"
        if self.calls + self.results == 0:
            return "tool_evidence_unavailable"
        return None

    def prompt_block(self) -> str:
        if self.unavailable_reason is not None:
            raise ValueError("tool evidence is not evaluable")
        return self.PROMPT_TEMPLATE.format(
            calls=self.calls, results=self.results,
            error_results=self.error_results, unknown_results=self.unknown_results,
            mcp_calls=self.mcp_calls,
            application_calls=self.application_calls,
            provider_hosted_calls=self.provider_hosted_calls,
            origin_not_captured_calls=self.origin_not_captured_calls,
            origin_unusable_calls=self.origin_unusable_calls,
        )

    def fingerprint_values(self) -> list[int]:
        # The total event count bounds eligibility; only these nine counts
        # are visible to the judge and therefore bind score currentness.
        return [
            self.calls, self.results, self.error_results, self.unknown_results,
            self.mcp_calls, self.application_calls, self.provider_hosted_calls,
            self.origin_not_captured_calls, self.origin_unusable_calls,
        ]


@dataclass(frozen=True)
class NextUserMessage:
    """The user's next request in the same session, already redacted at capture.

    What the user wrote after reading a final response is evidence of how the
    response landed: understood, corrected, or questioned. The next request is
    the next Turn of the same run or, after a run's last Turn, the first Turn of
    the session's next top-level run. A sub-agent run's requests come from
    another agent, so its Turns are not evaluable in this mode.
    """

    MODE: ClassVar[str] = NEXT_MESSAGE_MODE
    PROMPT_TEMPLATE: ClassVar[str] = "{message}"

    text: str | None = None
    state: EvidenceState = EvidenceState.MISSING
    truncated: bool = False
    sub_agent: bool = False
    # False when no later request exists at all, as opposed to one not captured.
    has_next: bool = False
    # The next request was sent before this reply finished, or either time is
    # unknown, so it cannot be the user's reaction to the reply.
    order_unclear: bool = False

    @property
    def not_applicable(self) -> bool:
        """No user reaction can exist, so the Turn is outside this measure."""
        return self.sub_agent or self.order_unclear or not self.has_next

    @property
    def unavailable_reason(self) -> str | None:
        if self.sub_agent:
            return "next_message_from_agent"
        if self.order_unclear:
            return "next_message_order_unclear"
        if self.truncated:
            return "next_message_truncated"
        if self.state is not EvidenceState.PRESENT or not (self.text or "").strip():
            return "next_message_unavailable"
        return None

    def prompt_block(self) -> str:
        if self.unavailable_reason is not None:
            raise ValueError("next user message is not evaluable")
        return self.PROMPT_TEMPLATE.format(message=self.text)

    def fingerprint_values(self) -> list[str | None]:
        return [self.text]


TurnEvidence = TurnToolCounts | NextUserMessage
TURN_EVIDENCE_MODES = (TOOL_EVIDENCE_MODE, NEXT_MESSAGE_MODE)
# Evaluator configuration keys that record which projection the judge saw.
_MODE_CONFIG_KEYS = {
    TOOL_EVIDENCE_MODE: ("tool_evidence_mode", "tool_evidence_template"),
    NEXT_MESSAGE_MODE: ("next_message_mode", "next_message_template"),
}


def turn_evidence_mode(config: Mapping[str, Any]) -> str | None:
    """Return the one extra evidence projection a validated evaluator saw."""
    for mode, (key, _template) in _MODE_CONFIG_KEYS.items():
        if config.get(key) == mode:
            return mode
    return None


def requested_turn_evidence_mode(*, tool_evidence: bool, next_message: bool) -> str | None:
    """Map the storage read flags to one mode; a judge sees at most one projection."""
    if tool_evidence and next_message:
        raise ValueError("an Agent Turn evaluator uses at most one evidence projection")
    return TOOL_EVIDENCE_MODE if tool_evidence else NEXT_MESSAGE_MODE if next_message else None


class RunPlacement(NamedTuple):
    """Where a run sits in its session, for pairing a Turn with the next request."""

    session_id: str | None
    parent_run_id: str | None
    started_at: datetime


def _utc(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def paired_next_message(
    text: str | None, state: EvidenceState | str, truncated: bool, *,
    reply_ended_at: datetime | str | None, next_started_at: datetime | str | None,
) -> NextUserMessage:
    """Project a following request, kept only if it was sent after the reply ended.

    Python pairing and every storage backend build the result here, so the
    order rule cannot differ between the Monitor and the judge.
    """
    ended, started = _utc(reply_ended_at), _utc(next_started_at)
    if ended is None or started is None or started < ended:
        return NextUserMessage(has_next=True, order_unclear=True)
    return NextUserMessage(text=text, state=EvidenceState(state),
                           truncated=bool(truncated), has_next=True)


def next_user_message(turn: AgentTurn | None, *,
                      reply_ended_at: datetime | None) -> NextUserMessage:
    """Project the following Turn's request; ``None`` means no following Turn."""
    if turn is None:
        return NextUserMessage()
    return paired_next_message(
        turn.user_request_redacted, turn.request_state, bool(turn.request_truncated),
        reply_ended_at=reply_ended_at, next_started_at=turn.started_at,
    )


def next_user_messages(
    turns: Iterable[AgentTurn], runs: Mapping[str, RunPlacement],
) -> dict[tuple[str, str], NextUserMessage]:
    """Pair every Turn with the user's next request in the same session.

    Within a run the next request is the next Turn by sequence. After a run's
    last Turn it is the first Turn of the next top-level run of the same
    session, ordered by run start and run ID; storage pairs Turns the same way
    in SQL. Turns of sub-agent runs are marked, never paired, and a request
    sent before the reply ended is withheld.
    """
    by_run: dict[str, list[AgentTurn]] = {}
    for turn in turns:
        by_run.setdefault(turn.run_id, []).append(turn)
    for run_turns in by_run.values():
        run_turns.sort(key=lambda turn: (turn.sequence, turn.turn_id))
    by_session: dict[str, list[str]] = {}
    for run_id in by_run:
        placement = runs.get(run_id)
        if placement and placement.session_id is not None and placement.parent_run_id is None:
            by_session.setdefault(placement.session_id, []).append(run_id)
    first_turn_of_next_run: dict[str, AgentTurn] = {}
    for run_ids in by_session.values():
        run_ids.sort(key=lambda run_id: (runs[run_id].started_at, run_id))
        for current, following in pairwise(run_ids):
            first_turn_of_next_run[current] = by_run[following][0]
    result = {}
    for run_id, run_turns in by_run.items():
        placement = runs.get(run_id)
        if placement is not None and placement.parent_run_id is not None:
            for turn in run_turns:
                result[(run_id, turn.turn_id)] = NextUserMessage(sub_agent=True)
            continue
        following = [*run_turns[1:], first_turn_of_next_run.get(run_id)]
        for current, after in zip(run_turns, following, strict=True):
            result[(run_id, current.turn_id)] = next_user_message(
                after, reply_ended_at=current.ended_at)
    return result


_TOOL_ORIGIN_CODES = frozenset(
    {"mcp", "application", "provider_hosted", "not_captured", "unusable"}
)


def classify_tool_origin(attributes: object) -> str:
    """Return one fixed classification code without exposing the raw value."""
    if not isinstance(attributes, Mapping):
        return "unusable"
    if "tool_origin" not in attributes:
        return "not_captured"
    value = attributes["tool_origin"]
    if isinstance(value, str) and value in {"mcp", "application", "provider_hosted"}:
        return value
    return "unusable"


def tool_counts_from_rows(rows: list[tuple]) -> TurnToolCounts:
    """Reduce no more than N+1 ordered event metadata rows to bounded counts."""
    if len(rows) > MAX_TOOL_EVIDENCE_EVENTS:
        return TurnToolCounts(event_count=len(rows))
    calls = results = errors = unknown = 0
    origins = {code: 0 for code in _TOOL_ORIGIN_CODES}
    for row in rows:
        if len(row) not in (3, 4):
            raise ValueError("invalid tool evidence row")
        event_type, status, is_error = row[:3]
        origin = row[3] if len(row) == 4 else "not_captured"
        if event_type == "tool_call":
            calls += 1
            code = origin if isinstance(origin, str) and origin in _TOOL_ORIGIN_CODES else "unusable"
            origins[code] += 1
        elif event_type == "tool_result":
            results += 1
            if is_error is True or status in ("failed", "timed_out", "cancelled"):
                errors += 1
            elif is_error is not False or status != "completed":
                unknown += 1
    return TurnToolCounts(
        len(rows), calls, results, errors, unknown,
        mcp_calls=origins["mcp"],
        application_calls=origins["application"],
        provider_hosted_calls=origins["provider_hosted"],
        origin_not_captured_calls=origins["not_captured"],
        origin_unusable_calls=origins["unusable"],
    )


def turn_evidence_fingerprint(turn: AgentTurn, evidence: TurnEvidence | None = None) -> str:
    """Bind a result to the exact redacted evidence and eligibility state."""
    payload = [
        turn.status.value, turn.request_state.value, turn.response_state.value,
        turn.user_request_redacted, turn.final_response_redacted,
        turn.request_truncated, turn.response_truncated,
    ]
    if evidence is not None:
        payload.extend([evidence.MODE, evidence.fingerprint_values()])
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
        configured = 0
        for expected, (mode_key, template_key) in _MODE_CONFIG_KEYS.items():
            mode = self.evaluator_config.get(mode_key)
            template = self.evaluator_config.get(template_key)
            if mode is None:
                if mode_key in self.evaluator_config or template is not None:
                    raise ValueError("invalid Turn evidence configuration")
            elif mode != expected or not isinstance(template, str) or not template:
                raise ValueError("invalid Turn evidence configuration")
            else:
                configured += 1
        if configured > 1:
            raise ValueError("invalid Turn evidence configuration")
        self.evaluator_provider = redact(self.evaluator_provider) or ""
        self.rubric_version = redact(self.rubric_version) or ""
        self.judge_models = [redact(model) or "" for model in self.judge_models]
        self.error = redact_structure(self.error)
        for dimension in self.dimensions:
            dimension.reasoning = redact_structure(dimension.reasoning) or ""
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
    evidence: TurnEvidence | None = None,
) -> str:
    mode = turn_evidence_mode(incoming.evaluator_config)
    if mode is not None and (
        evidence is None or evidence.MODE != mode or evidence.unavailable_reason is not None
    ):
        return "stale"
    if turn is None or turn_evidence_reason(turn) is not None or (
        turn_evidence_fingerprint(turn, evidence if mode else None)
        != incoming.evidence_fingerprint
    ):
        return "stale"
    if previous is not None and previous.evaluator_config != incoming.evaluator_config:
        previous = None
    if previous is not None and previous.evidence_fingerprint == incoming.evidence_fingerprint:
        if previous.status is JudgmentStatus.COMPLETED:
            return "already_completed"
        if incoming.status is JudgmentStatus.ERROR:
            return "already_error"
    return "saved"
