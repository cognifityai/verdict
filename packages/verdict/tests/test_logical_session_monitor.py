from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from verdict.agent_judgment import (
    AgentTurnJudgment,
    tool_counts_from_rows,
    turn_evidence_fingerprint,
)
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    SourceSession,
)
from verdict.schema import DimensionScore, JudgmentStatus, Verdict
from verdict.session_monitoring import (
    LogicalSessionEvidence,
    logical_session_run_evidence,
    preview_logical_sessions,
)
from verdict.storage import BufferedStorage, InMemoryStorage, SQLiteStorage

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
TENANT = "tenant-a"
EVALUATOR = "a" * 64


def _bundle(
    *,
    run_id: str,
    source_id: str,
    logical_session_id: str | None,
    minute: int,
    response: str = "answer",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
) -> AgentRunBundle:
    started = NOW + timedelta(minutes=minute)
    ended = started + timedelta(seconds=1)
    turn = AgentTurn(
        f"turn-{run_id}",
        run_id,
        0,
        started,
        status,
        ended,
        "question",
        response,
        EvidenceState.PRESENT,
        EvidenceState.PRESENT,
    )
    return AgentRunBundle(
        SourceSession(source_id, TENANT, "test", "f" * 64, started, ended),
        AgentRun(
            run_id,
            source_id,
            TENANT,
            started,
            status,
            ended,
            session_id=logical_session_id,
        ),
        (turn,),
    )


def _judgment(bundle: AgentRunBundle, verdict: Verdict) -> AgentTurnJudgment:
    turn = bundle.turns[0]
    return AgentTurnJudgment(
        tenant_id=TENANT,
        run_id=turn.run_id,
        turn_id=turn.turn_id,
        evaluator_fingerprint=EVALUATOR,
        evidence_fingerprint=turn_evidence_fingerprint(turn),
        evaluator_provider="test",
        evaluator_config={},
        judge_models=["test-judge"],
        expected_dimensions=["quality"],
        rubric_name="quality",
        rubric_version="1",
        dimensions=[DimensionScore("quality", verdict, "bounded", "test-judge")],
    )


def _evidence(
    bundles: tuple[AgentRunBundle, ...],
    judgments: tuple[AgentTurnJudgment, ...] = (),
    *,
    counts_mode: bool = False,
) -> LogicalSessionEvidence:
    projected = []
    for bundle in bundles:
        rows_by_turn: dict[str, list[tuple[str, str, object]]] = {}
        for event in bundle.events:
            rows_by_turn.setdefault(event.turn_id, []).append((
                event.event_type.value,
                event.status.value,
                event.attributes.get("is_error"),
            ))
        counts = {
            (turn.run_id, turn.turn_id): tool_counts_from_rows(
                rows_by_turn.get(turn.turn_id, []),
            )
            for turn in bundle.turns
        } if counts_mode else None
        projected.append(logical_session_run_evidence(
            bundle,
            calculate_evidence=bool(judgments),
            tool_counts=counts,
        ))
    return LogicalSessionEvidence(tuple(projected), judgments)


def test_logical_session_preview_collapses_calls_runs_and_source_containers() -> None:
    first = _bundle(
        run_id="run-a",
        source_id="producer-one",
        logical_session_id="shared-session",
        minute=0,
    )
    child = _bundle(
        run_id="run-b",
        source_id="producer-two",
        logical_session_id="shared-session",
        minute=1,
    )
    other = _bundle(
        run_id="run-c",
        source_id="producer-one",
        logical_session_id="other-session",
        minute=2,
    )

    preview = preview_logical_sessions(
        _evidence((first, child, other)),
        tenant_id=TENANT,
        reference_ratio=0.5,
    ).as_dict()

    assert preview["analysisUnit"] == "logical_session"
    assert preview["inferential"] is False
    assert preview["activationAllowed"] is False
    assert preview["observationalUnits"] == 2
    assert preview["reference"]["unitCount"] == 1
    assert preview["current"]["unitCount"] == 1
    encoded = str(preview)
    assert "shared-session" not in encoded
    assert "other-session" not in encoded
    assert "producer-one" not in encoded


def test_missing_logical_session_identity_is_reported_and_never_guessed() -> None:
    missing = _bundle(
        run_id="run-missing",
        source_id="source",
        logical_session_id=None,
        minute=0,
    )

    preview = preview_logical_sessions(
        _evidence((missing,)),
        tenant_id=TENANT,
        reference_ratio=0.5,
    ).as_dict()

    assert preview["observationalUnits"] == 0
    assert preview["coverage"]["runsMissingLogicalSession"] == 1
    assert [metric["metric"] for metric in preview["metrics"]] == [
        "agent.execution_completed",
        "agent.final_output_present",
    ]
    assert all(metric["referenceValue"] is None for metric in preview["metrics"])


def test_preview_is_descriptive_and_recomputes_an_extended_session_as_of_each_read() -> None:
    passing = _bundle(
        run_id="run-pass",
        source_id="source-a",
        logical_session_id="session-a",
        minute=0,
    )
    failing = _bundle(
        run_id="run-fail",
        source_id="source-b",
        logical_session_id="session-a",
        minute=1,
        status=ExecutionStatus.FAILED,
    )

    before = preview_logical_sessions(
        _evidence((passing,)),
        tenant_id=TENANT,
        reference_ratio=0.5,
    ).as_dict()
    after = preview_logical_sessions(
        _evidence((passing, failing)),
        tenant_id=TENANT,
        reference_ratio=0.5,
    ).as_dict()

    assert before["metrics"][0]["pValue"] is None
    assert all(metric["alert"] is None for metric in before["metrics"])
    assert after["observationalUnits"] == 1
    assert any(
        metric["metric"] == "agent.execution_completed" and metric["currentValue"] == 0.0
        for metric in after["metrics"]
    )


def test_preview_excludes_nonterminal_sessions_and_reports_missing_final_output() -> None:
    running = _bundle(
        run_id="run-running",
        source_id="source-running",
        logical_session_id="session-running",
        minute=0,
        status=ExecutionStatus.UNKNOWN,
    )
    missing_output = _bundle(
        run_id="run-missing-output",
        source_id="source-missing-output",
        logical_session_id="session-missing-output",
        minute=1,
        response="",
    )
    missing_output = replace(
        missing_output,
        turns=(replace(
            missing_output.turns[0],
            final_response_redacted=None,
            response_state=EvidenceState.MISSING,
        ),),
    )

    preview = preview_logical_sessions(
        _evidence((running, missing_output)),
        tenant_id=TENANT,
        reference_ratio=0.5,
    ).as_dict()

    assert preview["observationalUnits"] == 1
    assert preview["coverage"]["sessionsInProgress"] == 1
    final_output = next(
        metric
        for metric in preview["metrics"]
        if metric["metric"] == "agent.final_output_present"
    )
    assert final_output["currentValue"] == 0.0


def test_explicit_windows_use_the_first_observed_run_and_exclude_the_gap() -> None:
    reference = _bundle(
        run_id="run-reference",
        source_id="source-reference",
        logical_session_id="session-reference",
        minute=0,
    )
    gap = _bundle(
        run_id="run-gap",
        source_id="source-gap",
        logical_session_id="session-gap",
        minute=10,
    )
    current = _bundle(
        run_id="run-current",
        source_id="source-current",
        logical_session_id="session-current",
        minute=20,
    )

    preview = preview_logical_sessions(
        _evidence((reference, gap, current)),
        tenant_id=TENANT,
        reference_start=NOW,
        reference_end=NOW + timedelta(minutes=5),
        current_start=NOW + timedelta(minutes=15),
        current_end=NOW + timedelta(minutes=25),
    ).as_dict()

    assert preview["windowMode"] == "explicit"
    assert preview["observationalUnits"] == 3
    assert preview["reference"]["unitCount"] == 1
    assert preview["current"]["unitCount"] == 1


def test_session_judge_pass_requires_every_turn_and_fail_has_a_causal_witness() -> None:
    passing = _bundle(
        run_id="run-pass",
        source_id="source-a",
        logical_session_id="session-a",
        minute=0,
    )
    failing = _bundle(
        run_id="run-fail",
        source_id="source-b",
        logical_session_id="session-a",
        minute=1,
    )
    other = _bundle(
        run_id="run-other",
        source_id="source-c",
        logical_session_id="session-b",
        minute=2,
    )
    judgments = (
        _judgment(passing, Verdict.PASS),
        _judgment(failing, Verdict.FAIL),
        _judgment(other, Verdict.PASS),
    )

    preview = preview_logical_sessions(
        _evidence((passing, failing, other), judgments),
        tenant_id=TENANT,
        reference_ratio=0.5,
        evaluator_fingerprint=EVALUATOR,
        evaluator_dimensions=("quality",),
    ).as_dict()

    metric = next(item for item in preview["metrics"] if item["metric"] == "judge.quality.pass")
    assert metric["referenceValue"] == 0.0
    assert metric["currentValue"] == 1.0
    assert metric["effect"] == 1.0
    assert metric["pValue"] is None
    assert metric["alert"] is None


def test_stale_turn_judgment_cannot_make_a_session_pass() -> None:
    bundle = _bundle(
        run_id="run-a",
        source_id="source-a",
        logical_session_id="session-a",
        minute=0,
    )
    stale = replace(_judgment(bundle, Verdict.PASS), evidence_fingerprint="b" * 64)

    preview = preview_logical_sessions(
        _evidence((bundle,), (stale,)),
        tenant_id=TENANT,
        reference_ratio=0.5,
        evaluator_fingerprint=EVALUATOR,
        evaluator_dimensions=("quality",),
    ).as_dict()

    metric = next(item for item in preview["metrics"] if item["metric"] == "judge.quality.pass")
    assert metric["currentEvaluable"] == 0
    assert metric["currentMissing"] == 1


def test_counts_only_turn_judgment_is_bound_to_bounded_tool_evidence() -> None:
    bundle = _bundle(
        run_id="run-tools",
        source_id="source-tools",
        logical_session_id="session-tools",
        minute=0,
    )
    turn = bundle.turns[0]
    call = AgentEvent(
        "call",
        turn.turn_id,
        0,
        turn.started_at,
        AgentEventType.TOOL_CALL,
        ExecutionStatus.COMPLETED,
        "test",
        {"tool_name": "private", "call_id": "private-id"},
    )
    bundle = replace(bundle, events=(call,))
    from verdict.agent_judgment import TOOL_EVIDENCE_MODE, TurnToolCounts

    counts = TurnToolCounts(event_count=1, calls=1)
    judgment = replace(
        _judgment(bundle, Verdict.PASS),
        evidence_fingerprint=turn_evidence_fingerprint(turn, counts),
        evaluator_config={
            "tool_evidence_mode": TOOL_EVIDENCE_MODE,
            "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
        },
    )
    current = preview_logical_sessions(
        _evidence((bundle,), (judgment,), counts_mode=True),
        tenant_id=TENANT,
        reference_ratio=0.5,
        evaluator_fingerprint=EVALUATOR,
        evaluator_dimensions=("quality",),
    ).as_dict()
    result = AgentEvent(
        "result",
        turn.turn_id,
        1,
        turn.started_at,
        AgentEventType.TOOL_RESULT,
        ExecutionStatus.COMPLETED,
        "test",
        {"tool_name": "private", "call_id": "private-id", "is_error": False},
    )
    changed = preview_logical_sessions(
        _evidence(
            (replace(bundle, events=(call, result)),),
            (judgment,),
            counts_mode=True,
        ),
        tenant_id=TENANT,
        reference_ratio=0.5,
        evaluator_fingerprint=EVALUATOR,
        evaluator_dimensions=("quality",),
    ).as_dict()

    current_metric = next(
        item for item in current["metrics"] if item["metric"] == "judge.quality.pass"
    )
    changed_metric = next(
        item for item in changed["metrics"] if item["metric"] == "judge.quality.pass"
    )
    assert current_metric["currentEvaluable"] == 1
    assert changed_metric["currentEvaluable"] == 0
    assert changed_metric["currentMissing"] == 1


def test_counts_only_turn_judgment_fails_closed_above_event_limit() -> None:
    bundle = _bundle(
        run_id="run-many-events",
        source_id="source-many-events",
        logical_session_id="session-many-events",
        minute=0,
    )
    turn = bundle.turns[0]
    events = tuple(
        AgentEvent(
            f"call-{index}",
            turn.turn_id,
            index,
            turn.started_at,
            AgentEventType.TOOL_CALL,
            ExecutionStatus.COMPLETED,
            "test",
            {},
        )
        for index in range(65)
    )
    from verdict.agent_judgment import TOOL_EVIDENCE_MODE, TurnToolCounts

    judgment = replace(
        _judgment(bundle, Verdict.PASS),
        evidence_fingerprint=turn_evidence_fingerprint(
            turn,
            TurnToolCounts(event_count=1, calls=1),
        ),
        evaluator_config={
            "tool_evidence_mode": TOOL_EVIDENCE_MODE,
            "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
        },
    )

    preview = preview_logical_sessions(
        _evidence((replace(bundle, events=events),), (judgment,), counts_mode=True),
        tenant_id=TENANT,
        reference_ratio=0.5,
        evaluator_fingerprint=EVALUATOR,
        evaluator_dimensions=("quality",),
    ).as_dict()

    metric = next(item for item in preview["metrics"] if item["metric"] == "judge.quality.pass")
    assert metric["currentEvaluable"] == 0
    assert metric["currentMissing"] == 1


def test_selected_evaluator_identity_must_be_consistent() -> None:
    bundle = _bundle(
        run_id="run-a",
        source_id="source-a",
        logical_session_id="session-a",
        minute=0,
    )
    inconsistent = replace(_judgment(bundle, Verdict.PASS), rubric_version="2")

    with pytest.raises(ValueError, match="evaluator identity is inconsistent"):
        preview_logical_sessions(
            _evidence((bundle,), (_judgment(bundle, Verdict.PASS), inconsistent)),
            tenant_id=TENANT,
            reference_ratio=0.5,
            evaluator_fingerprint=EVALUATOR,
            evaluator_dimensions=("quality",),
        )


def test_selected_evaluator_fingerprint_must_be_bounded_text() -> None:
    with pytest.raises(ValueError, match="evaluator identity is invalid"):
        preview_logical_sessions(
            LogicalSessionEvidence((), ()),
            tenant_id=TENANT,
            evaluator_fingerprint=42,  # type: ignore[arg-type]
            evaluator_dimensions=("quality",),
        )


def test_sqlite_logical_session_read_is_batched_and_content_free(tmp_path) -> None:
    storage = SQLiteStorage(str(tmp_path / "batched-session-monitor.db"))
    try:
        for index in range(50):
            bundle = _bundle(
                run_id=f"run-{index:02d}",
                source_id=f"source-{index:02d}",
                logical_session_id=f"session-{index // 2:02d}",
                minute=index,
            )
            storage.replace_agent_run_bundle(
                replace(
                    bundle,
                    session=replace(bundle.session, source_locator_hash=f"{index:064x}"),
                )
            )
        statements: list[str] = []
        storage._conn.set_trace_callback(statements.append)

        evidence = storage.load_logical_session_monitor_evidence(TENANT)

        reads = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        assert len(evidence.runs) == 50
        assert len(reads) == 3
        assert "question" not in repr(evidence)
        assert "answer" not in repr(evidence)
    finally:
        storage._conn.set_trace_callback(None)
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_evaluator_projection_rejects_excess_turn_text_before_return(
    tmp_path,
    monkeypatch,
    adapter: str,
) -> None:
    if adapter == "memory":
        import verdict.storage.memory as storage_module

        storage = InMemoryStorage()
    else:
        import verdict.storage.sqlite as storage_module

        storage = SQLiteStorage(str(tmp_path / "bounded-turn-text.db"))
    monkeypatch.setattr(storage_module, "MAX_LOGICAL_SESSION_MONITOR_TURN_TEXT_BYTES", 8)
    try:
        bundle = _bundle(
            run_id="run-large",
            source_id="source-large",
            logical_session_id="session-large",
            minute=0,
            response="response",
        )
        storage.replace_agent_run_bundle(bundle)

        with pytest.raises(ValueError, match="bounded Turn text limit"):
            storage.load_logical_session_monitor_evidence(
                TENANT,
                evaluator_fingerprint=EVALUATOR,
            )
    finally:
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_malformed_newest_judgment_does_not_hide_older_valid_evaluator(
    tmp_path,
    adapter: str,
) -> None:
    storage = (
        InMemoryStorage()
        if adapter == "memory"
        else SQLiteStorage(str(tmp_path / "evaluator-discovery.db"))
    )
    try:
        valid_bundle = _bundle(
            run_id="run-valid",
            source_id="source-valid",
            logical_session_id="session-valid",
            minute=0,
        )
        corrupt_bundle = _bundle(
            run_id="run-corrupt",
            source_id="source-corrupt",
            logical_session_id="session-corrupt",
            minute=1,
        )
        corrupt_bundle = replace(
            corrupt_bundle,
            session=replace(corrupt_bundle.session, source_locator_hash="e" * 64),
        )
        storage.replace_agent_run_bundle(valid_bundle)
        storage.replace_agent_run_bundle(corrupt_bundle)
        assert storage.save_agent_turn_judgment_if_current(
            _judgment(valid_bundle, Verdict.PASS),
        ) == "saved"
        corrupt_turn = corrupt_bundle.turns[0]
        if adapter == "memory":
            storage._agent_turn_judgments[(
                TENANT, corrupt_turn.run_id, corrupt_turn.turn_id, EVALUATOR,
            )] = "{malformed"
        else:
            storage._conn.execute(
                "INSERT INTO agent_turn_judgments ("
                "tenant_id,run_id,turn_id,evaluator_fingerprint,evidence_fingerprint,"
                "status,evaluated_at,result_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    TENANT,
                    corrupt_turn.run_id,
                    corrupt_turn.turn_id,
                    EVALUATOR,
                    "b" * 64,
                    "completed",
                    (NOW + timedelta(days=1)).isoformat(),
                    "{malformed",
                ),
            )
            storage._conn.commit()

        page = storage.list_agent_turn_evaluator_identities(TENANT)

        assert [item.evaluator_fingerprint for item in page.judgments] == [EVALUATOR]
        assert page.truncated is False
        if adapter == "sqlite":
            indexes = {
                row[1]
                for row in storage._conn.execute(
                    "PRAGMA index_list('agent_turn_judgments')"
                ).fetchall()
            }
            assert "idx_agent_turn_judgments_tenant_evaluated" in indexes
            plan = storage._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM agent_turn_judgments "
                "WHERE tenant_id=? ORDER BY evaluated_at DESC,evaluator_fingerprint,"
                "run_id,turn_id LIMIT ?",
                (TENANT, 1_001),
            ).fetchall()
            assert any(
                "idx_agent_turn_judgments_tenant_evaluated" in row[3]
                for row in plan
            )
    finally:
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_evaluator_discovery_bounds_raw_slots_before_deduplicating(
    tmp_path,
    monkeypatch,
    adapter: str,
) -> None:
    if adapter == "memory":
        import verdict.storage.memory as storage_module

        storage = InMemoryStorage()
    else:
        import verdict.storage.sqlite as storage_module

        storage = SQLiteStorage(str(tmp_path / "evaluator-slot-bound.db"))
    monkeypatch.setattr(storage_module, "MAX_AGENT_EVALUATOR_IDENTITY_CANDIDATES", 2)
    try:
        bundles = tuple(
            _bundle(
                run_id=f"run-slot-{index}",
                source_id=f"source-slot-{index}",
                logical_session_id=f"session-slot-{index}",
                minute=index,
            )
            for index in range(3)
        )
        for index, bundle in enumerate(bundles):
            storage.replace_agent_run_bundle(replace(
                bundle,
                session=replace(bundle.session, source_locator_hash=f"{index + 1:064x}"),
            ))
            judgment = replace(
                _judgment(bundle, Verdict.PASS),
                evaluator_fingerprint=("a" if index == 0 else "b") * 64,
                evaluated_at=NOW + timedelta(minutes=index),
            )
            assert storage.save_agent_turn_judgment_if_current(judgment) == "saved"

        page = storage.list_agent_turn_evaluator_identities(TENANT)

        assert [item.evaluator_fingerprint for item in page.judgments] == ["b" * 64]
        assert page.truncated is True
    finally:
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_evaluator_discovery_reorders_a_repaired_existing_slot(
    tmp_path,
    monkeypatch,
    adapter: str,
) -> None:
    if adapter == "memory":
        import verdict.storage.memory as storage_module

        storage = InMemoryStorage()
    else:
        import verdict.storage.sqlite as storage_module

        storage = SQLiteStorage(str(tmp_path / "evaluator-repair-order.db"))
    monkeypatch.setattr(storage_module, "MAX_AGENT_EVALUATOR_IDENTITY_CANDIDATES", 1)
    try:
        first = _bundle(
            run_id="run-repaired",
            source_id="source-repaired",
            logical_session_id="session-repaired",
            minute=0,
        )
        second = _bundle(
            run_id="run-second",
            source_id="source-second",
            logical_session_id="session-second",
            minute=1,
        )
        second = replace(
            second,
            session=replace(second.session, source_locator_hash="2" * 64),
        )
        storage.replace_agent_run_bundle(first)
        storage.replace_agent_run_bundle(second)
        completed = replace(
            _judgment(first, Verdict.PASS),
            evaluator_fingerprint="a" * 64,
            evaluated_at=NOW + timedelta(minutes=3),
        )
        error = replace(
            completed,
            status=JudgmentStatus.ERROR,
            dimensions=[],
            error="provider failed",
            evaluated_at=NOW,
        )
        other = replace(
            _judgment(second, Verdict.PASS),
            evaluator_fingerprint="b" * 64,
            evaluated_at=NOW + timedelta(minutes=2),
        )
        assert storage.save_agent_turn_judgment_if_current(error) == "saved"
        assert storage.save_agent_turn_judgment_if_current(other) == "saved"
        assert storage.save_agent_turn_judgment_if_current(completed) == "saved"

        page = storage.list_agent_turn_evaluator_identities(TENANT)

        assert [item.evaluator_fingerprint for item in page.judgments] == ["a" * 64]
        assert page.truncated is True
    finally:
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite", "buffered"])
def test_storage_projects_counts_only_evidence_without_event_content(
    tmp_path,
    adapter: str,
) -> None:
    if adapter == "memory":
        storage = InMemoryStorage()
    elif adapter == "sqlite":
        storage = SQLiteStorage(str(tmp_path / "counts-projection.db"))
    else:
        storage = BufferedStorage(InMemoryStorage())
    try:
        bundle = _bundle(
            run_id="run-counts",
            source_id="source-counts",
            logical_session_id="session-counts",
            minute=0,
        )
        turn = bundle.turns[0]
        bundle = replace(bundle, events=(
            AgentEvent(
                "call", turn.turn_id, 0, turn.started_at,
                AgentEventType.TOOL_CALL, ExecutionStatus.COMPLETED, "test",
                {"tool_name": "private-tool", "call_id": "private-call"},
            ),
            AgentEvent(
                "result", turn.turn_id, 1, turn.started_at,
                AgentEventType.TOOL_RESULT, ExecutionStatus.COMPLETED, "test",
                {
                    "tool_name": "private-tool",
                    "call_id": "private-call",
                    "is_error": False,
                },
            ),
            AgentEvent(
                "unknown-result", turn.turn_id, 2, turn.started_at,
                AgentEventType.TOOL_RESULT, ExecutionStatus.COMPLETED, "test",
                {"tool_name": "private-tool", "call_id": "private-call-2"},
            ),
        ))
        storage.replace_agent_run_bundle(bundle)
        from verdict.agent_judgment import TOOL_EVIDENCE_MODE, TurnToolCounts

        counts = TurnToolCounts(
            event_count=3,
            calls=1,
            results=2,
            unknown_results=1,
        )
        judgment = replace(
            _judgment(bundle, Verdict.PASS),
            evidence_fingerprint=turn_evidence_fingerprint(turn, counts),
            evaluator_config={
                "tool_evidence_mode": TOOL_EVIDENCE_MODE,
                "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
            },
        )
        assert storage.save_agent_turn_judgment_if_current(judgment) == "saved"

        evidence = storage.load_logical_session_monitor_evidence(
            TENANT,
            evaluator_fingerprint=EVALUATOR,
            tool_evidence_mode=TOOL_EVIDENCE_MODE,
        )
        preview = preview_logical_sessions(
            evidence,
            tenant_id=TENANT,
            reference_ratio=0.5,
            evaluator_fingerprint=EVALUATOR,
            evaluator_dimensions=("quality",),
        ).as_dict()

        metric = next(
            item for item in preview["metrics"] if item["metric"] == "judge.quality.pass"
        )
        assert metric["currentEvaluable"] == 1
        assert "private-tool" not in repr(evidence)
        assert "private-call" not in repr(evidence)
    finally:
        storage.close()


def test_sqlite_counts_projection_treats_malformed_event_attributes_as_unknown(
    tmp_path,
) -> None:
    storage = SQLiteStorage(str(tmp_path / "malformed-event-json.db"))
    try:
        bundle = _bundle(
            run_id="run-malformed-event",
            source_id="source-malformed-event",
            logical_session_id="session-malformed-event",
            minute=0,
        )
        turn = bundle.turns[0]
        bundle = replace(bundle, events=(AgentEvent(
            "result-malformed",
            turn.turn_id,
            0,
            turn.started_at,
            AgentEventType.TOOL_RESULT,
            ExecutionStatus.COMPLETED,
            "test",
            {},
        ),))
        storage.replace_agent_run_bundle(bundle)
        from verdict.agent_judgment import TOOL_EVIDENCE_MODE, TurnToolCounts

        counts = TurnToolCounts(event_count=1, results=1, unknown_results=1)
        judgment = replace(
            _judgment(bundle, Verdict.PASS),
            evidence_fingerprint=turn_evidence_fingerprint(turn, counts),
            evaluator_config={
                "tool_evidence_mode": TOOL_EVIDENCE_MODE,
                "tool_evidence_template": TurnToolCounts.PROMPT_TEMPLATE,
            },
        )
        assert storage.save_agent_turn_judgment_if_current(judgment) == "saved"
        storage._conn.execute(
            "UPDATE agent_events SET attributes_json='{malformed' "
            "WHERE tenant_id=? AND event_id=?",
            (TENANT, "result-malformed"),
        )
        storage._conn.commit()

        evidence = storage.load_logical_session_monitor_evidence(
            TENANT,
            evaluator_fingerprint=EVALUATOR,
            tool_evidence_mode=TOOL_EVIDENCE_MODE,
        )
        preview = preview_logical_sessions(
            evidence,
            tenant_id=TENANT,
            reference_ratio=0.5,
            evaluator_fingerprint=EVALUATOR,
            evaluator_dimensions=("quality",),
        ).as_dict()

        metric = next(
            item for item in preview["metrics"] if item["metric"] == "judge.quality.pass"
        )
        assert metric["currentEvaluable"] == 1
        assert "{malformed" not in repr(evidence)
    finally:
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite", "buffered"])
def test_storage_reads_one_bounded_tenant_snapshot(tmp_path, adapter: str) -> None:
    if adapter == "memory":
        storage = InMemoryStorage()
    elif adapter == "sqlite":
        storage = SQLiteStorage(str(tmp_path / "session-monitor.db"))
    else:
        storage = BufferedStorage(InMemoryStorage())
    try:
        first = _bundle(
            run_id="run-a",
            source_id="source-a",
            logical_session_id="session-a",
            minute=0,
        )
        other_base = _bundle(
            run_id="run-other",
            source_id="source-other",
            logical_session_id="session-other",
            minute=1,
        )
        other = replace(
            other_base,
            session=replace(other_base.session, tenant_id="tenant-b"),
            run=replace(other_base.run, tenant_id="tenant-b"),
        )
        storage.replace_agent_run_bundle(first)
        storage.replace_agent_run_bundle(other)
        judgment = _judgment(first, Verdict.PASS)
        assert storage.save_agent_turn_judgment_if_current(judgment) == "saved"

        evidence = storage.load_logical_session_monitor_evidence(
            TENANT,
            evaluator_fingerprint=EVALUATOR,
        )
        identities = storage.list_agent_turn_evaluator_identities(TENANT)

        assert [run.run_id for run in evidence.runs] == ["run-a"]
        assert [item.turn_id for item in evidence.judgments] == ["turn-run-a"]
        assert [item.evaluator_fingerprint for item in identities.judgments] == [EVALUATOR]
        assert storage.load_logical_session_monitor_evidence("tenant-b").runs[0].run_id == (
            "run-other"
        )
    finally:
        storage.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite", "buffered"])
def test_storage_treats_ascii_whitespace_only_final_output_as_missing(
    tmp_path,
    adapter: str,
) -> None:
    if adapter == "memory":
        storage = InMemoryStorage()
    elif adapter == "sqlite":
        storage = SQLiteStorage(str(tmp_path / "whitespace-output.db"))
    else:
        storage = BufferedStorage(InMemoryStorage())
    try:
        bundle = _bundle(
            run_id="run-whitespace",
            source_id="source-whitespace",
            logical_session_id="session-whitespace",
            minute=0,
            response=" \t\n\v\f\r\u00a0\u2003\u3000",
        )
        storage.replace_agent_run_bundle(bundle)

        preview = preview_logical_sessions(
            storage.load_logical_session_monitor_evidence(TENANT),
            tenant_id=TENANT,
            reference_ratio=0.5,
        ).as_dict()

        metric = next(
            item
            for item in preview["metrics"]
            if item["metric"] == "agent.final_output_present"
        )
        assert metric["currentValue"] == 0.0
    finally:
        storage.close()
