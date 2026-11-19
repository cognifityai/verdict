"""Next-user-message evidence for Agent Turn evaluation across storage adapters."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from verdict.agent_judgment import (
    NEXT_MESSAGE_MODE,
    TOOL_EVIDENCE_MODE,
    AgentTurnJudgment,
    NextUserMessage,
    RunPlacement,
    TurnToolCounts,
    next_user_messages,
    turn_evidence_fingerprint,
    turn_judgment_write_decision,
)
from verdict.evidence import (
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.schema import DimensionScore, JudgmentStatus, Verdict
from verdict.storage import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)
EVALUATOR = "e" * 64


def _turn(turn_id, sequence, request, response="Done.", *, run_id="run-1", **changes):
    turn = AgentTurn(
        turn_id=turn_id, run_id=run_id, sequence=sequence,
        started_at=NOW + timedelta(minutes=sequence), ended_at=NOW + timedelta(minutes=sequence),
        status=ExecutionStatus.COMPLETED,
        user_request_redacted=request, final_response_redacted=response,
        request_state=EvidenceState.PRESENT, response_state=EvidenceState.PRESENT,
    )
    return replace(turn, **changes)


def _bundle(tenant, turns, *, run_id="run-1", started_at=NOW, **run_fields):
    return AgentRunBundle(
        session=SourceSession(
            source_session_id=f"session-{run_id}", tenant_id=tenant,
            source_kind="pydantic-ai",
            source_locator_hash=hashlib.sha256(run_id.encode()).hexdigest(),
            started_at=NOW, observed_at=NOW,
        ),
        run=AgentRun(
            run_id=run_id, source_session_id=f"session-{run_id}", tenant_id=tenant,
            started_at=started_at, ended_at=started_at, status=ExecutionStatus.COMPLETED,
            **run_fields,
        ),
        turns=tuple(turns),
    )


def _run_one(prefix=""):
    return [
        _turn("turn-a", 0, f"{prefix}Add a retry to the upload.", "Added a retry. Tests pass."),
        _turn("turn-b", 1, f"{prefix}That broke the upload, revert it.", "Reverted."),
        _turn("turn-c", 2, f"{prefix}Thanks, ship it.", "Shipped."),
    ]


def _judgment(tenant, turn, evidence, *, config=None, status=JudgmentStatus.COMPLETED):
    return AgentTurnJudgment(
        tenant_id=tenant, run_id=turn.run_id, turn_id=turn.turn_id,
        evaluator_fingerprint=EVALUATOR,
        evidence_fingerprint=turn_evidence_fingerprint(turn, evidence),
        evaluator_provider="openai",
        evaluator_config=config or {
            "next_message_mode": NEXT_MESSAGE_MODE, "next_message_template": "{message}",
        },
        judge_models=["judge-model"], expected_dimensions=["no_correction_needed"],
        rubric_name="human_reaction", rubric_version="1",
        dimensions=[] if status is JudgmentStatus.ERROR else [DimensionScore(
            name="no_correction_needed", verdict=Verdict.FAIL,
            reasoning="The user asked for a revert.", judge_model="judge-model",
        )],
        status=status, error="judge error" if status is JudgmentStatus.ERROR else None,
    )


def _storage(backend, tmp_path):
    if backend == "memory":
        return InMemoryStorage(), "local"
    if backend == "sqlite":
        return SQLiteStorage(str(tmp_path / "next.db")), "local"
    dsn = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("disposable Postgres required")
    from verdict.storage.postgres import PostgresStorage

    return PostgresStorage(dsn), f"next-message-{uuid4().hex}"


# Pinned from the release before next-message evidence existed: adding a mode
# must not change the identity of results saved without it or with tool counts.
def test_existing_turn_fingerprints_are_unchanged():
    turn = _turn("t", 0, "Q é", "A", run_id="r")
    counts = TurnToolCounts(event_count=5, calls=2, results=2, error_results=1,
                            unknown_results=0, mcp_calls=1, application_calls=1)

    assert turn_evidence_fingerprint(turn) == (
        "af686fabd0ab6534e24293a72f41bdb31f873ee29c591b085737e6f8551bf46f")
    assert turn_evidence_fingerprint(turn, counts) == (
        "b917c8215f015dd13f1743e048b7306e81d82e68aa3b3211cc20301e109316d4")


def test_next_message_binds_only_its_text_and_differs_from_other_modes():
    turn = _turn("t", 0, "Q", "A")
    message = NextUserMessage("No, use the other file.", EvidenceState.PRESENT)

    with_message = turn_evidence_fingerprint(turn, message)
    assert with_message != turn_evidence_fingerprint(turn)
    assert with_message != turn_evidence_fingerprint(
        turn, NextUserMessage("No, use this file.", EvidenceState.PRESENT))
    assert with_message == turn_evidence_fingerprint(
        turn, NextUserMessage("No, use the other file.", EvidenceState.PRESENT))
    # A different projection with the same visible value is a different identity.
    assert turn_evidence_fingerprint(turn, TurnToolCounts()) != turn_evidence_fingerprint(
        turn, NextUserMessage(None, EvidenceState.PRESENT))


@pytest.mark.parametrize(("message", "reason"), [
    (NextUserMessage(), "next_message_unavailable"),
    (NextUserMessage("text", EvidenceState.MISSING), "next_message_unavailable"),
    (NextUserMessage("text", EvidenceState.NOT_CAPTURED), "next_message_unavailable"),
    (NextUserMessage(" \n\t", EvidenceState.PRESENT), "next_message_unavailable"),
    (NextUserMessage("", EvidenceState.PRESENT), "next_message_unavailable"),
    (NextUserMessage("partial", EvidenceState.PRESENT, truncated=True),
     "next_message_truncated"),
    (NextUserMessage("ok", EvidenceState.PRESENT, sub_agent=True), "next_message_from_agent"),
    (NextUserMessage("ok", EvidenceState.PRESENT), None),
])
def test_next_message_eligibility(message, reason):
    assert message.unavailable_reason == reason
    if reason is None:
        assert message.prompt_block() == "ok"
    else:
        with pytest.raises(ValueError):
            message.prompt_block()


def test_pairing_without_a_session_stays_within_each_run():
    turns = [
        _turn("b", 5, "second"), _turn("a", 1, "first"),
        _turn("x", 2, "other run", run_id="run-2"),
    ]

    paired = next_user_messages(turns, {
        "run-1": RunPlacement(None, None, NOW),
        "run-2": RunPlacement(None, None, NOW + timedelta(hours=1)),
    })

    assert paired[("run-1", "a")].text == "second"
    assert paired[("run-1", "b")].unavailable_reason == "next_message_unavailable"
    assert paired[("run-2", "x")].unavailable_reason == "next_message_unavailable"


def test_pairing_continues_into_the_sessions_next_top_level_run():
    # A request-per-run application: every user message opens a new run.
    turns = [
        _turn("a", 0, "first", run_id="r-1"), _turn("b", 0, "second", run_id="r-2"),
        _turn("tie", 0, "same start", run_id="r-3"), _turn("child", 0, "from agent",
                                                           run_id="c-1"),
        _turn("elsewhere", 0, "other session", run_id="r-4"),
    ]
    runs = {
        "r-1": RunPlacement("s", None, NOW),
        "c-1": RunPlacement("s", "r-1", NOW + timedelta(minutes=1)),
        "r-2": RunPlacement("s", None, NOW + timedelta(minutes=2)),
        "r-3": RunPlacement("s", None, NOW + timedelta(minutes=2)),
        "r-4": RunPlacement("t", None, NOW + timedelta(minutes=3)),
    }

    paired = next_user_messages(turns, runs)

    assert paired[("r-1", "a")].text == "second"
    assert paired[("r-2", "b")].text == "same start"
    assert paired[("r-3", "tie")].unavailable_reason == "next_message_unavailable"
    assert paired[("c-1", "child")].unavailable_reason == "next_message_from_agent"
    assert paired[("r-4", "elsewhere")].unavailable_reason == "next_message_unavailable"


@pytest.mark.parametrize("config", [
    {"next_message_mode": NEXT_MESSAGE_MODE},
    {"next_message_mode": "next_user_message_v2", "next_message_template": "{message}"},
    {"next_message_mode": None, "next_message_template": "{message}"},
    {"next_message_mode": NEXT_MESSAGE_MODE, "next_message_template": "{message}",
     "tool_evidence_mode": TOOL_EVIDENCE_MODE, "tool_evidence_template": "x"},
])
def test_invalid_next_message_evaluator_configuration_is_rejected(config):
    turn = _turn("t", 0, "Q")
    with pytest.raises(ValueError, match="Turn evidence configuration"):
        _judgment("local", turn, NextUserMessage("ok", EvidenceState.PRESENT), config=config)


def test_write_decision_requires_the_matching_current_projection():
    turn = _turn("t", 0, "Q")
    message = NextUserMessage("Revert that.", EvidenceState.PRESENT)
    result = _judgment("local", turn, message)

    assert turn_judgment_write_decision(turn, result, None, message) == "saved"
    assert turn_judgment_write_decision(turn, result, None, None) == "stale"
    assert turn_judgment_write_decision(turn, result, None, TurnToolCounts(1, 1)) == "stale"
    assert turn_judgment_write_decision(
        turn, result, None, NextUserMessage("Different.", EvidenceState.PRESENT)) == "stale"
    assert turn_judgment_write_decision(
        turn, result, None,
        NextUserMessage("Revert that.", EvidenceState.PRESENT, truncated=True)) == "stale"


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_storage_pairs_scores_and_invalidates_next_message_evidence(tmp_path, backend):
    storage, tenant = _storage(backend, tmp_path)
    try:
        run_one = _run_one()
        storage.replace_agent_run_bundle(_bundle(tenant, run_one))
        storage.replace_agent_run_bundle(
            _bundle(tenant, [_turn("solo", 0, "Only turn", run_id="run-2")], run_id="run-2"))
        # Same run and Turn IDs in another tenant must never supply a next message.
        other = f"{tenant}-other"
        storage.replace_agent_run_bundle(_bundle(other, _run_one("OTHER TENANT ")))

        with pytest.raises(ValueError, match="at most one"):
            storage.list_agent_turn_evaluation_candidates(
                tenant, EVALUATOR, tool_evidence=True, next_message=True)

        rows, has_more = storage.list_agent_turn_evaluation_candidates(
            tenant, EVALUATOR, next_message=True)
        assert not has_more
        by_turn = {turn.turn_id: (status, message) for turn, status, message in rows}
        assert by_turn["turn-a"][1].text == "That broke the upload, revert it."
        assert by_turn["turn-b"][1].text == "Thanks, ship it."
        assert by_turn["turn-c"][1].unavailable_reason == "next_message_unavailable"
        assert by_turn["solo"][1].unavailable_reason == "next_message_unavailable"
        assert all(status is None for status, _ in by_turn.values())
        single = storage.get_agent_turn_evaluation_candidate(
            tenant, "run-1", "turn-a", EVALUATOR, next_message=True)
        assert single[2] == by_turn["turn-a"][1]

        message = by_turn["turn-a"][1]
        assert storage.save_agent_turn_judgment_if_current(
            _judgment(tenant, run_one[0], message)) == "saved"
        assert storage.save_agent_turn_judgment_if_current(
            _judgment(tenant, run_one[2], NextUserMessage())) == "stale"

        def status_of(turn_id, **mode):
            rows, _ = storage.list_agent_turn_evaluation_candidates(tenant, EVALUATOR, **mode)
            return {item[0].turn_id: item[1] for item in rows}[turn_id]

        assert status_of("turn-a", next_message=True) is JudgmentStatus.COMPLETED
        # The same evaluator slot is not current for a judge that saw other evidence.
        assert status_of("turn-a") is None
        assert status_of("turn-a", tool_evidence=True) is None
        assert storage.get_agent_turn_evaluation_candidate(
            tenant, "run-1", "turn-a", EVALUATOR, next_message=True,
        )[1] is JudgmentStatus.COMPLETED

        # A score about a next message other than the one on record is refused.
        assert storage.save_agent_turn_judgment_if_current(_judgment(
            tenant, run_one[1], NextUserMessage("Looks good.", EvidenceState.PRESENT),
        )) == "stale"

        # A live run's last Turn becomes evaluable once the user writes again.
        storage.replace_agent_run_bundle(_bundle(tenant, [
            *run_one, _turn("turn-d", 3, "One more thing."),
        ]))
        rows, _ = storage.list_agent_turn_evaluation_candidates(
            tenant, EVALUATOR, next_message=True)
        assert {turn.turn_id: msg.text for turn, _, msg in rows}["turn-c"] == "One more thing."
        assert status_of("turn-a", next_message=True) is JudgmentStatus.COMPLETED

        storage.replace_agent_run_bundle(_bundle(tenant, [
            _turn("first", 0, "Q", run_id="run-3"),
            _turn("cut", 1, "A very long pasted log", run_id="run-3", request_truncated=True),
        ], run_id="run-3"))
        rows, _ = storage.list_agent_turn_evaluation_candidates(
            tenant, EVALUATOR, next_message=True)
        assert {turn.turn_id: msg for turn, _, msg in rows}["first"].unavailable_reason == (
            "next_message_truncated")

        other_rows, _ = storage.list_agent_turn_evaluation_candidates(
            other, EVALUATOR, next_message=True)
        assert {turn.turn_id: msg.text for turn, _, msg in other_rows}["turn-a"] == (
            "OTHER TENANT That broke the upload, revert it.")
    finally:
        storage.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_session_monitor_evidence_matches_next_message_scores(tmp_path, backend):
    storage, tenant = _storage(backend, tmp_path)
    try:
        run_one = _run_one()
        storage.replace_agent_run_bundle(_bundle(tenant, run_one))
        message = NextUserMessage("That broke the upload, revert it.", EvidenceState.PRESENT)
        assert storage.save_agent_turn_judgment_if_current(
            _judgment(tenant, run_one[0], message)) == "saved"

        evidence = storage.load_logical_session_monitor_evidence(
            tenant, evaluator_fingerprint=EVALUATOR, tool_evidence_mode=NEXT_MESSAGE_MODE)
        fingerprints = {turn.turn_id: turn.evidence_fingerprint
                        for run in evidence.runs for turn in run.turns}
        assert fingerprints["turn-a"] == turn_evidence_fingerprint(run_one[0], message)
        assert fingerprints["turn-c"] is None
        assert [item.turn_id for item in evidence.judgments] == ["turn-a"]

        plain = storage.load_logical_session_monitor_evidence(
            tenant, evaluator_fingerprint=EVALUATOR)
        assert {turn.turn_id: turn.evidence_fingerprint
                for run in plain.runs for turn in run.turns}["turn-a"] != fingerprints["turn-a"]
        with pytest.raises(ValueError):
            storage.load_logical_session_monitor_evidence(
                tenant, tool_evidence_mode=NEXT_MESSAGE_MODE)
    finally:
        storage.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_storage_pairs_across_runs_of_a_session_like_the_monitor(tmp_path, backend):
    storage, tenant = _storage(backend, tmp_path)
    try:
        def run(run_id, minutes, requests, **fields):
            storage.replace_agent_run_bundle(_bundle(
                tenant,
                [_turn(f"{run_id}-{index}", index, text, run_id=run_id)
                 for index, text in enumerate(requests)],
                run_id=run_id, started_at=NOW + timedelta(minutes=minutes), **fields,
            ))

        run("r-1", 0, ["Add a retry."], session_id="s")
        run("c-1", 1, ["Coordinator: research retries."], session_id="s",
            parent_run_id="r-1")
        run("r-2", 2, ["No, that broke uploads.", "Thanks."], session_id="s")
        run("r-3", 3, ["Unrelated."], session_id="other")
        run("r-4", 4, ["No session."])
        # Same start: run IDs break the tie in byte order on every backend.
        run("a-tie", 5, ["Lowercase first in the database collation."], session_id="tie")
        run("B-tie", 5, ["Uppercase first in byte order."], session_id="tie")

        rows, _ = storage.list_agent_turn_evaluation_candidates(
            tenant, EVALUATOR, next_message=True)
        messages = {turn.turn_id: message for turn, _, message in rows}
        assert messages["r-1-0"].text == "No, that broke uploads."
        assert messages["r-2-0"].text == "Thanks."
        assert messages["r-2-1"].unavailable_reason == "next_message_unavailable"
        assert messages["c-1-0"].unavailable_reason == "next_message_from_agent"
        assert messages["r-3-0"].unavailable_reason == "next_message_unavailable"
        assert messages["r-4-0"].unavailable_reason == "next_message_unavailable"
        assert messages["B-tie-0"].text == "Lowercase first in the database collation."
        assert messages["a-tie-0"].unavailable_reason == "next_message_unavailable"
        assert storage.get_agent_turn_evaluation_candidate(
            tenant, "r-1", "r-1-0", EVALUATOR, next_message=True,
        )[2] == messages["r-1-0"]

        # The Monitor pairs in Python; it must agree with the SQL pairing.
        turns = {turn.turn_id: turn for turn, _, _ in rows}
        evidence = storage.load_logical_session_monitor_evidence(
            tenant, evaluator_fingerprint=EVALUATOR, tool_evidence_mode=NEXT_MESSAGE_MODE)
        for item in (turn for run_evidence in evidence.runs for turn in run_evidence.turns):
            message = messages[item.turn_id]
            expected = (
                None if message.unavailable_reason
                else turn_evidence_fingerprint(turns[item.turn_id], message)
            )
            assert item.evidence_fingerprint == expected, item.turn_id
    finally:
        storage.close()


@pytest.mark.parametrize(("message", "applicable"), [
    (NextUserMessage(), False),
    (NextUserMessage(sub_agent=True), False),
    # A later request exists but was not captured: a capture gap, still counted.
    (NextUserMessage(None, EvidenceState.MISSING, has_next=True), True),
    (NextUserMessage("ok", EvidenceState.PRESENT, has_next=True), True),
])
def test_only_turns_with_no_possible_reaction_leave_the_monitor_roll_up(message, applicable):
    from verdict.session_monitoring import logical_session_turn_evidence

    evidence = logical_session_turn_evidence(
        _turn("t", 0, "Q"), calculate_evidence=True, tool_counts=message)
    assert evidence.judge_applicable is applicable
    assert logical_session_turn_evidence(
        _turn("t", 0, "Q"), calculate_evidence=True).judge_applicable
