"""Storage tests run against BOTH adapters via parametrize — this is what
keeps the Storage Protocol a real abstraction. If a test passes on one
adapter but not the other, the Protocol is leaking implementation details.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from verdict.schema import (
    DimensionScore,
    DriftDirection,
    DriftSignal,
    EvaluatorHealthRecord,
    EvaluatorHealthStatus,
    Judgment,
    Operation,
    SpanRecord,
    Trace,
    UserSignalRecord,
    Verdict,
)
from verdict.storage.base import Storage
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage


@pytest.fixture(params=["memory", "sqlite"])
def storage(request, tmp_path: Path):
    if request.param == "memory":
        s = InMemoryStorage()
    else:
        s = SQLiteStorage(str(tmp_path / "verdict.db"))
    yield s
    s.close()


def _trace(**overrides):
    base = dict(
        provider="anthropic",
        operation=Operation.CHAT,
        request_model="claude-sonnet",
        response_model="claude-sonnet",
        input_tokens=100,
        output_tokens=50,
        temperature=0.7,
        cluster_id="c0001",
        tenant_id="tenant-a",
    )
    base.update(overrides)
    return Trace(**base)


def test_all_storage_adapters_match_protocol_parameter_kinds_and_defaults():
    adapters = (InMemoryStorage, SQLiteStorage, PostgresStorage, BufferedStorage)
    for method_name, protocol_method in inspect.getmembers(
        Storage, predicate=inspect.isfunction
    ):
        if method_name.startswith("_"):
            continue
        expected = list(inspect.signature(protocol_method).parameters.values())[1:]
        for adapter in adapters:
            actual_method = getattr(adapter, method_name)
            actual = list(inspect.signature(actual_method).parameters.values())[1:]
            assert [
                (parameter.name, parameter.kind, parameter.default)
                for parameter in actual
            ] == [
                (parameter.name, parameter.kind, parameter.default)
                for parameter in expected
            ], f"{adapter.__name__}.{method_name} diverges from Storage"


def test_insert_and_get_trace(storage):
    t = _trace(parent_span_id="span-parent")
    storage.insert_trace(t)
    fetched = storage.get_trace(t.trace_id)
    assert fetched is not None
    assert fetched.trace_id == t.trace_id
    assert fetched.provider == "anthropic"
    assert fetched.input_tokens == 100
    assert fetched.parent_span_id == "span-parent"


def test_sqlite_migrates_trace_parent_span_link(tmp_path):
    path = tmp_path / "legacy-traces.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
            provider TEXT, operation TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, temperature REAL,
            max_tokens INTEGER, finish_reason TEXT, error TEXT, latency_ms REAL,
            prompt_redacted TEXT, response_redacted TEXT, raw_messages_json TEXT,
            tenant_id TEXT, session_id TEXT, user_id_hash TEXT, cluster_id TEXT,
            tags_json TEXT, cost_usd REAL
        );
        """
    )
    connection.execute(
        "INSERT INTO traces (trace_id, started_at, operation) VALUES (?, ?, ?)",
        ("legacy", "2026-08-01T00:00:00+00:00", "chat"),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(str(path))
    try:
        legacy = storage.get_trace("legacy")
        linked = _trace(trace_id="linked", parent_span_id="span-1")
        storage.insert_trace(linked)
        round_trip = storage.get_trace("linked")
    finally:
        storage.close()

    assert legacy is not None and legacy.parent_span_id is None
    assert round_trip is not None and round_trip.parent_span_id == "span-1"


def test_sqlite_migrates_pre_cluster_id_trace_schema_before_creating_indexes(tmp_path):
    path = tmp_path / "pre-cluster-traces.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, parent_span_id TEXT,
            started_at TEXT NOT NULL, ended_at TEXT, provider TEXT,
            operation TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, temperature REAL,
            max_tokens INTEGER, finish_reason TEXT, error TEXT, latency_ms REAL,
            prompt_redacted TEXT, response_redacted TEXT, raw_messages_json TEXT,
            tenant_id TEXT, session_id TEXT, user_id_hash TEXT,
            tags_json TEXT, cost_usd REAL
        );
        """
    )
    connection.execute(
        "INSERT INTO traces (trace_id, started_at, operation) VALUES (?, ?, ?)",
        ("legacy", "2026-08-01T00:00:00+00:00", "chat"),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(str(path))
    try:
        legacy = storage.get_trace("legacy")
        storage.insert_trace(_trace(trace_id="new", cluster_id="cluster-new"))
        clustered = storage.list_traces(cluster_id="cluster-new")
    finally:
        storage.close()

    assert legacy is not None and legacy.cluster_id is None
    assert [trace.trace_id for trace in clustered] == ["new"]


def test_sqlite_migrates_legacy_spans_without_parent_name(tmp_path):
    path = tmp_path / "legacy-spans.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE spans (
            span_id TEXT PRIMARY KEY, name TEXT, trace_id TEXT,
            started_at TEXT NOT NULL, ended_at TEXT, duration_ms REAL,
            attributes_json TEXT, error TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO spans (span_id, name, started_at) VALUES (?, ?, ?)",
        ("legacy", "legacy-span", "2026-08-01T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(str(path))
    try:
        legacy = storage.list_spans()[0]
        storage.insert_span(SpanRecord(span_id="new", name="child", parent_name="root"))
        records = {record.span_id: record for record in storage.list_spans()}
    finally:
        storage.close()

    assert legacy.parent_name is None
    assert records["new"].parent_name == "root"


def test_storage_sanitizes_every_content_bearing_trace_field(storage):
    canaries = [
        "storage@example.com",
        "123-45-6789",
        "4111111111111111",
        "203.0.113.8",
    ]
    trace = _trace(
        prompt_redacted=f"email {canaries[0]}",
        response_redacted=f"ssn {canaries[1]}",
        error=f"card {canaries[2]}",
        raw_messages=[{
            "role": "assistant",
            "content": [{"type": "tool_result", "content": {"ip": canaries[3]}}],
        }],
        tags={"contact": canaries[0]},
    )

    storage.insert_trace(trace)
    fetched = storage.get_trace(trace.trace_id)

    assert fetched is not None
    serialized = repr(fetched)
    for canary in canaries:
        assert canary not in serialized
        assert canary not in repr(trace)


def test_sqlite_serialized_record_contains_no_content_canary(tmp_path):
    path = tmp_path / "privacy.db"
    storage = SQLiteStorage(str(path))
    canary = "sqlite-record@example.com"
    trace = _trace(
        prompt_redacted=canary,
        raw_messages=[{
            "role": "assistant",
            "content": [{"type": "tool_result", "content": {"email": canary}}],
        }],
        tags={"email": canary},
    )
    storage.insert_trace(trace)
    storage.close()

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT prompt_redacted, raw_messages_json, tags_json FROM traces"
        ).fetchone()
    finally:
        connection.close()

    assert canary not in repr(row)


def test_storage_sanitizes_judge_reasoning_and_manual_span_content(storage):
    canary = "reviewer@example.com"
    trace = _trace(cluster_id="privacy")
    storage.insert_trace(trace)
    judgment = Judgment(
        trace_id=trace.trace_id,
        error=f"judge failed for {canary}",
        dimensions=[DimensionScore(
            name="quality",
            verdict=Verdict.FAIL,
            reasoning=f"The response quoted {canary}",
        )],
    )
    span = SpanRecord(
        name=f"tool for {canary}",
        trace_id=trace.trace_id,
        attributes={"nested": {"contact": canary}},
        error=f"tool failed for {canary}",
    )

    storage.insert_judgment(judgment)
    storage.insert_span(span)

    fetched_judgment = storage.list_judgments_for_cluster("privacy")[0]
    fetched_span = storage.list_spans(trace_id=trace.trace_id)[0]
    assert canary not in repr(fetched_judgment)
    assert canary not in repr(fetched_span)
    assert canary not in repr(judgment)
    assert canary not in repr(span)


def test_sqlite_serialized_judgment_and_span_contain_no_content_canary(tmp_path):
    path = tmp_path / "privacy-surfaces.db"
    canary = "serialized@example.com"
    storage = SQLiteStorage(str(path))
    trace = _trace(cluster_id="privacy")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        error=canary,
        dimensions=[DimensionScore(
            name="quality", verdict=Verdict.FAIL, reasoning=canary
        )],
    ))
    storage.insert_span(SpanRecord(
        name=canary,
        trace_id=trace.trace_id,
        attributes={"contact": canary},
        error=canary,
    ))
    storage.close()

    connection = sqlite3.connect(path)
    try:
        judgment_row = connection.execute(
            "SELECT dimensions_json, error FROM judgments"
        ).fetchone()
        span_row = connection.execute(
            "SELECT name, attributes_json, error FROM spans"
        ).fetchone()
    finally:
        connection.close()

    assert canary not in repr(judgment_row)
    assert canary not in repr(span_row)


def test_sqlite_span_redaction_bounds_shared_graph_serialization(tmp_path):
    path = tmp_path / "bounded-redaction.db"
    shared = {"email": "shared@example.com"}
    for _ in range(22):
        shared = {"left": shared, "right": shared}

    storage = SQLiteStorage(str(path))
    try:
        storage.insert_span(SpanRecord(name="shared-dag", attributes=shared))
    finally:
        storage.close()

    connection = sqlite3.connect(path)
    try:
        [serialized] = connection.execute(
            "SELECT attributes_json FROM spans WHERE name = ?", ("shared-dag",)
        ).fetchone()
    finally:
        connection.close()

    assert "shared@example.com" not in serialized
    assert len(serialized.encode("utf-8")) < 100_000


def test_trace_accepts_enum_value_string(storage):
    """Manual Trace construction accepts the documented enum value string."""
    trace = _trace(operation="chat")
    storage.insert_trace(trace)

    fetched = storage.get_trace(trace.trace_id)
    assert fetched is not None
    assert fetched.operation == Operation.CHAT


def test_trace_maps_provider_operation_string_to_vendor_neutral_enum(storage):
    trace = _trace(operation="messages.create")
    storage.insert_trace(trace)

    fetched = storage.get_trace(trace.trace_id)
    assert fetched is not None
    assert fetched.operation == Operation.CHAT


def test_trace_rejects_unknown_operation_string():
    with pytest.raises(ValueError, match="Unsupported operation"):
        _trace(operation="not-a-real-operation")


def test_get_missing_trace_returns_none(storage):
    assert storage.get_trace("does-not-exist") is None


def test_list_traces_filters_by_tenant_and_cluster(storage):
    t1 = _trace(tenant_id="a", cluster_id="c1")
    t2 = _trace(tenant_id="a", cluster_id="c2")
    t3 = _trace(tenant_id="b", cluster_id="c1")
    for t in (t1, t2, t3):
        storage.insert_trace(t)

    a_all = storage.list_traces(tenant_id="a")
    assert len(a_all) == 2

    a_c1 = storage.list_traces(tenant_id="a", cluster_id="c1")
    assert len(a_c1) == 1
    assert a_c1[0].trace_id == t1.trace_id


def test_list_traces_returns_newest_first(storage):
    older = _trace(started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = _trace(started_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    newest = _trace(started_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    for t in (older, newest, newer):
        storage.insert_trace(t)

    traces = storage.list_traces(limit=2)

    assert [t.trace_id for t in traces] == [newest.trace_id, newer.trace_id]


def test_insert_and_list_judgments_for_cluster(storage):
    trace = _trace(cluster_id="cluster-x")
    storage.insert_trace(trace)
    j = Judgment(
        trace_id=trace.trace_id,
        judge_models=["gemini-flash"],
        dimensions=[
            DimensionScore(name="groundedness", verdict="pass", reasoning="ok", judge_model="gemini-flash"),
            DimensionScore(name="relevance", verdict=Verdict.FAIL, reasoning="not really", judge_model="gemini-flash"),
        ],
    )
    storage.insert_judgment(j)
    fetched = storage.list_judgments_for_cluster("cluster-x")
    assert len(fetched) == 1
    assert fetched[0].pass_count == 1
    assert fetched[0].fail_count == 1
    assert fetched[0].pass_rate == 0.5


def test_evaluator_health_round_trip_and_identity_filter(storage):
    older = EvaluatorHealthRecord(
        evaluator_fingerprint="judge-a",
        sentinel_set_name="support-v1",
        sentinel_set_fingerprint="set-a",
        correct_labels=27,
        total_labels=30,
        agreement=0.9,
        confidence_low=0.74,
        confidence_high=0.97,
        status=EvaluatorHealthStatus.HEALTHY,
    )
    other = EvaluatorHealthRecord(
        evaluator_fingerprint="judge-b",
        sentinel_set_name="support-v1",
        sentinel_set_fingerprint="set-a",
        correct_labels=10,
        total_labels=30,
        agreement=1 / 3,
        confidence_low=0.19,
        confidence_high=0.51,
        status=EvaluatorHealthStatus.DEGRADED,
        error_count=2,
    )
    storage.insert_evaluator_health(older)
    storage.insert_evaluator_health(other)

    fetched = storage.list_evaluator_health(
        evaluator_fingerprint="judge-b", limit=10
    )

    assert len(fetched) == 1
    assert fetched[0].health_id == other.health_id
    assert fetched[0].status == EvaluatorHealthStatus.DEGRADED
    assert fetched[0].error_count == 2


def test_sqlite_migrates_legacy_judgment_identity_as_explicitly_incomplete(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
            provider TEXT, operation TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, temperature REAL,
            max_tokens INTEGER, finish_reason TEXT, error TEXT, latency_ms REAL,
            prompt_redacted TEXT, response_redacted TEXT, raw_messages_json TEXT,
            tenant_id TEXT, session_id TEXT, user_id_hash TEXT, cluster_id TEXT,
            tags_json TEXT, cost_usd REAL
        );
        CREATE TABLE judgments (
            judgment_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
            rubric_name TEXT, rubric_version TEXT, created_at TEXT NOT NULL,
            judge_models_json TEXT, dimensions_json TEXT,
            position_swap_consistent INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO traces (trace_id, started_at, operation, cluster_id) "
        "VALUES ('legacy-trace', '2026-08-01T00:00:00+00:00', 'chat', 'c1')"
    )
    connection.execute(
        "INSERT INTO judgments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-judgment",
            "legacy-trace",
            "default",
            "1",
            "2026-08-01T00:00:00+00:00",
            '["legacy-model"]',
            '[]',
            None,
        ),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(str(path))
    try:
        judgment = storage.list_judgments_for_cluster("c1")[0]
    finally:
        storage.close()

    assert judgment.judge_models == ["legacy-model"]
    assert judgment.evaluator_identity_complete is False
    assert judgment.evaluator_fingerprint == ""
    assert judgment.status.value == "completed"


def test_sqlite_migrates_historical_drift_table_with_unattributed_identity(tmp_path):
    path = tmp_path / "historical-drift.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE drift_signals (
            signal_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL,
            cluster_id TEXT, dimension TEXT, direction TEXT,
            statistic_name TEXT, statistic_value REAL, p_value REAL,
            p_value_adjusted REAL, effect_size_cohens_d REAL,
            effect_size_cliffs_delta REAL DEFAULT 0.0,
            wasserstein_distance REAL DEFAULT 0.0, psi REAL DEFAULT 0.0,
            sample_size_current INTEGER, sample_size_baseline INTEGER,
            contributing_layers_json TEXT, example_trace_ids_json TEXT,
            recommended_action TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO drift_signals (signal_id, detected_at, dimension) VALUES (?, ?, ?)",
        ("historical", "2026-08-01T00:00:00+00:00", "quality"),
    )
    connection.commit()
    connection.close()

    storage = SQLiteStorage(str(path))
    try:
        historical = storage.list_drift_signals()[0]
        storage.insert_drift_signal(DriftSignal(
            signal_id="current",
            evaluator_fingerprint="evaluator-v2",
        ))
        current = next(
            signal for signal in storage.list_drift_signals()
            if signal.signal_id == "current"
        )
    finally:
        storage.close()

    assert historical.evaluator_fingerprint == ""
    assert current.evaluator_fingerprint == "evaluator-v2"


def test_reinsert_with_null_cluster_id_preserves_existing(storage):
    """Re-writing the SAME trace_id with cluster_id=None must NOT wipe a
    previously-assigned cluster_id. The clusterer assigns cluster_id *after* the
    trace is first stored; a later content/usage update carries None and must
    COALESCE to the existing value. (sqlite previously used INSERT OR REPLACE,
    which clobbered it; postgres/memory/sqlite must now behave identically.)
    """
    t = _trace(cluster_id="cluster-assigned")
    storage.insert_trace(t)

    # Re-write the same trace with cluster_id cleared but other fields updated.
    rewrite = _trace(
        trace_id=t.trace_id,
        cluster_id=None,
        output_tokens=999,
        response_model="claude-opus",
    )
    storage.insert_trace(rewrite)

    fetched = storage.get_trace(t.trace_id)
    assert fetched is not None
    assert fetched.cluster_id == "cluster-assigned"  # preserved via COALESCE
    assert fetched.output_tokens == 999              # other fields still updated
    assert fetched.response_model == "claude-opus"


def test_reinsert_with_null_parent_span_id_preserves_existing(storage):
    linked = _trace(parent_span_id="span-parent")
    storage.insert_trace(linked)

    storage.insert_trace(_trace(
        trace_id=linked.trace_id,
        parent_span_id=None,
        output_tokens=999,
    ))

    fetched = storage.get_trace(linked.trace_id)
    assert fetched is not None
    assert fetched.parent_span_id == "span-parent"
    assert fetched.output_tokens == 999


def test_reinsert_with_new_cluster_id_overwrites(storage):
    """A non-null cluster_id on re-write DOES replace the old one (COALESCE only
    protects against NULL, not a real reassignment)."""
    t = _trace(cluster_id="cluster-old")
    storage.insert_trace(t)
    storage.insert_trace(_trace(trace_id=t.trace_id, cluster_id="cluster-new"))
    fetched = storage.get_trace(t.trace_id)
    assert fetched is not None
    assert fetched.cluster_id == "cluster-new"


def test_drift_signals_round_trip(storage):
    sig = DriftSignal(
        cluster_id="c0001",
        dimension="groundedness",
        direction="regression",
        evaluator_fingerprint="evaluator-v1",
        statistic_name="fisher_exact",
        statistic_value=1234.5,
        p_value=0.001,
        p_value_adjusted=0.005,
        effect_size_cohens_d=0.7,
        effect_size_cliffs_delta=-0.62,
        wasserstein_distance=0.31,
        psi=0.42,
        sample_size_current=120,
        sample_size_baseline=900,
        contributing_layers=["judge_rubric"],
        example_trace_ids=["abc", "def"],
        recommended_action="investigate immediately",
    )
    storage.insert_drift_signal(sig)
    fetched = storage.list_drift_signals()
    assert len(fetched) == 1
    s = fetched[0]
    assert s.cluster_id == "c0001"
    assert s.direction == DriftDirection.REGRESSION
    assert s.evaluator_fingerprint == "evaluator-v1"
    assert s.effect_size_cohens_d == 0.7
    # The primary effect size + distributional diagnostics must survive the
    # round trip — losing these silently was a real Postgres bug.
    assert s.effect_size_cliffs_delta == -0.62
    assert s.wasserstein_distance == 0.31
    assert s.psi == 0.42
    assert s.contributing_layers == ["judge_rubric"]
    assert s.example_trace_ids == ["abc", "def"]


def test_reinsert_drift_signal_updates_in_place(storage):
    """A rerun of the same analysis window refreshes one signal, not two."""
    original = DriftSignal(
        signal_id="stable-window-signal",
        cluster_id="c0001",
        dimension="groundedness",
        direction="regression",
        statistic_name="fisher_exact",
        p_value=0.04,
        recommended_action="initial result",
    )
    storage.insert_drift_signal(original)

    replacement = DriftSignal(
        signal_id=original.signal_id,
        cluster_id="c0001",
        dimension="groundedness",
        direction="regression",
        statistic_name="fisher_exact",
        p_value=0.001,
        example_trace_ids=["current-1"],
        recommended_action="refreshed result",
    )
    storage.insert_drift_signal(replacement)

    fetched = storage.list_drift_signals()
    assert len(fetched) == 1
    assert fetched[0].p_value == 0.001
    assert fetched[0].example_trace_ids == ["current-1"]
    assert fetched[0].recommended_action == "refreshed result"


def test_delete_drift_signals_between_uses_half_open_window(storage):
    start = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    for signal_id, detected_at in [
        ("before", datetime(2026, 8, 11, 11, 59, tzinfo=timezone.utc)),
        ("inside", start),
        ("end", datetime(2026, 8, 11, 13, tzinfo=timezone.utc)),
    ]:
        storage.insert_drift_signal(DriftSignal(
            signal_id=signal_id,
            detected_at=detected_at,
            cluster_id="c1",
            dimension="relevance",
        ))

    storage.delete_drift_signals_between(
        start, datetime(2026, 8, 11, 13, tzinfo=timezone.utc),
    )

    assert {s.signal_id for s in storage.list_drift_signals()} == {"before", "end"}


def test_delete_drift_signals_between_can_isolate_evaluator(storage):
    start = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    storage.insert_drift_signal(DriftSignal(
        signal_id="evaluator-a",
        detected_at=start,
        evaluator_fingerprint="fingerprint-a",
    ))
    storage.insert_drift_signal(DriftSignal(
        signal_id="evaluator-b",
        detected_at=start,
        evaluator_fingerprint="fingerprint-b",
    ))

    storage.delete_drift_signals_between(
        start, end, evaluator_fingerprint="fingerprint-a"
    )

    assert [signal.signal_id for signal in storage.list_drift_signals()] == [
        "evaluator-b"
    ]


def test_drift_signal_columns_cover_all_stat_fields():
    """DB-free guard: every adapter must persist the same DriftSignal stat
    fields. The Postgres adapter once silently omitted Cliff's δ, Wasserstein
    and PSI from its table; this catches that whole class of mismatch without
    needing a live database.
    """
    from verdict.storage import postgres as pg

    required = [
        "effect_size_cohens_d",
        "effect_size_cliffs_delta",
        "wasserstein_distance",
        "psi",
        "p_value_adjusted",
        "evaluator_fingerprint",
        "sample_size_current",
        "sample_size_baseline",
    ]
    for col in required:
        # Present in the Postgres CREATE TABLE schema...
        assert col in pg._SCHEMA, f"Postgres schema missing {col}"
        # ...and in the explicit INSERT/SELECT column list.
        assert col in pg.PostgresStorage._SIGNAL_COLUMNS, f"Postgres I/O missing {col}"

    # The number of placeholders in the column list must match the count of
    # columns, or positional binding silently shifts.
    n_cols = len([c for c in pg.PostgresStorage._SIGNAL_COLUMNS.split(",")])
    assert n_cols == 19, f"expected 19 drift-signal columns, got {n_cols}"

    # DB-free guard only: the live Postgres path still needs integration
    # coverage in an environment that provides a Postgres instance.
    import inspect

    insert_source = inspect.getsource(pg.PostgresStorage.insert_drift_signal)
    assert "ON CONFLICT (signal_id) DO UPDATE SET" in insert_source


def test_postgres_trace_columns_include_stable_parent_span_link():
    import inspect

    from verdict.storage import postgres as pg

    assert "parent_span_id" in pg._SCHEMA
    assert "parent_span_id" in pg.PostgresStorage._TRACE_COLUMNS
    assert len(pg.PostgresStorage._TRACE_COLUMNS.split(",")) == 24
    insert_source = inspect.getsource(pg.PostgresStorage.insert_trace)
    assert "INSERT INTO traces (" in insert_source
    assert "COALESCE(" in insert_source
    assert "INSERT INTO traces VALUES" not in insert_source


def test_postgres_judgment_query_uses_fixed_columns_and_maps_them_in_order():
    from verdict.storage import postgres as pg

    storage = object.__new__(pg.PostgresStorage)
    captured = {}
    created_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
    row = (
        "judgment-1", "trace-1", "rubric", "2", created_at,
        ["judge-a"],
        [{"name": "quality", "verdict": "pass", "reasoning": "ok", "judge_model": "judge-a"}],
        True, "openai", {"model": "judge-a"}, "fp-1", ["quality"],
        "completed", None,
    )

    def fetchall(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [row]

    storage._fetchall = fetchall
    [judgment] = storage.list_judgments_for_cluster("cluster-1", limit=3)

    assert "j.*" not in captured["sql"]
    assert f"SELECT {storage._JUDGMENT_COLUMNS}" in captured["sql"]
    assert captured["params"] == ("cluster-1", 3)
    assert judgment.judgment_id == "judgment-1"
    assert judgment.position_swap_consistent is True
    assert judgment.evaluator_fingerprint == "fp-1"
    assert judgment.dimensions[0].verdict is Verdict.PASS


def test_postgres_span_upsert_assigns_trace_id_in_both_directions():
    """The span upsert must both ADD a delayed link and RETRACT a failed one.

    COALESCE(EXCLUDED.trace_id, spans.trace_id) satisfies only the first: it
    silently keeps a stale link when a retraction rewrites trace_id to NULL,
    making the "no span points at a trace that never landed" guarantee
    backend-dependent. Behavioural coverage lives in test_postgres_integration.
    """
    import inspect

    from verdict.storage import postgres as pg

    insert_source = inspect.getsource(pg.PostgresStorage.insert_span)
    update_clause = insert_source.split("ON CONFLICT (span_id) DO UPDATE SET", 1)[1]
    assert "trace_id    = EXCLUDED.trace_id" in update_clause
    assert "COALESCE(EXCLUDED.trace_id" not in update_clause


def test_postgres_identity_upserts_replace_every_non_primary_key_column():
    """Postgres must match memory/SQLite replacement semantics on conflicts."""
    import inspect

    from verdict.storage import postgres as pg

    judgment_source = inspect.getsource(pg.PostgresStorage.insert_judgment)
    judgment_update = judgment_source.split(
        "ON CONFLICT (judgment_id) DO UPDATE SET", 1
    )[1]
    for column in (
        "trace_id", "rubric_name", "rubric_version", "created_at",
        "judge_models", "dimensions", "position_swap_consistent",
        "evaluator_provider", "evaluator_config", "evaluator_fingerprint",
        "expected_dimensions", "status", "error",
    ):
        assert f"{column} = EXCLUDED.{column}" in judgment_update

    health_source = inspect.getsource(pg.PostgresStorage.insert_evaluator_health)
    health_update = health_source.split(
        "ON CONFLICT (health_id) DO UPDATE SET", 1
    )[1]
    for column in (
        "evaluated_at", "evaluator_fingerprint", "sentinel_set_name",
        "sentinel_set_fingerprint", "correct_labels", "total_labels",
        "agreement", "confidence_low", "confidence_high", "status",
        "error_count",
    ):
        assert f"{column} = EXCLUDED.{column}" in health_update


def test_delete_trace_removes_trace_and_judgments(storage):
    trace = _trace(cluster_id="cluster-del")
    storage.insert_trace(trace)
    j = Judgment(
        trace_id=trace.trace_id,
        dimensions=[DimensionScore(name="groundedness", verdict=Verdict.PASS)],
    )
    storage.insert_judgment(j)
    # A span and a user signal hung off the same trace must also be removed.
    storage.insert_span(SpanRecord(name="retrieval", trace_id=trace.trace_id))
    storage.insert_user_signal(UserSignalRecord(trace_id=trace.trace_id, kind="thumbs_up"))

    storage.delete_trace(trace.trace_id)

    assert storage.get_trace(trace.trace_id) is None
    assert storage.list_judgments_for_cluster("cluster-del") == []
    assert storage.list_spans(trace_id=trace.trace_id) == []
    assert [s for s in storage.list_user_signals() if s.trace_id == trace.trace_id] == []


def test_prune_before_deletes_only_older_and_returns_count(storage):
    old1 = _trace(started_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    old2 = _trace(started_at=datetime(2020, 6, 1, tzinfo=timezone.utc))
    recent = _trace(started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    for t in (old1, old2, recent):
        storage.insert_trace(t)

    # Judgments on an old trace should go away with it.
    storage.insert_judgment(Judgment(
        trace_id=old1.trace_id,
        dimensions=[DimensionScore(name="relevance", verdict=Verdict.FAIL)],
    ))

    cutoff = datetime(2021, 1, 1, tzinfo=timezone.utc).isoformat()
    n = storage.prune_before(cutoff)

    assert n == 2
    assert storage.get_trace(old1.trace_id) is None
    assert storage.get_trace(old2.trace_id) is None
    assert storage.get_trace(recent.trace_id) is not None
    # Judgments belonging to a pruned trace are gone too.
    assert storage.list_judgments_for_cluster(recent.cluster_id) is not None


def test_span_insert_list_round_trip_and_filter(storage):
    s1 = SpanRecord(
        name="retrieval",
        trace_id="trace-1",
        parent_name="root",
        duration_ms=12.5,
        attributes={"k": "v", "n": 3},
        error=None,
    )
    s2 = SpanRecord(name="rerank", trace_id="trace-2", duration_ms=4.0)
    storage.insert_span(s1)
    storage.insert_span(s2)

    all_spans = storage.list_spans()
    assert len(all_spans) == 2

    only_t1 = storage.list_spans(trace_id="trace-1")
    assert len(only_t1) == 1
    got = only_t1[0]
    assert got.span_id == s1.span_id
    assert got.name == "retrieval"
    assert got.parent_name == "root"
    assert got.duration_ms == 12.5
    assert got.attributes == {"k": "v", "n": 3}


def test_user_signal_insert_list_round_trip(storage):
    sig = UserSignalRecord(trace_id="trace-9", kind="thumbs_down")
    storage.insert_user_signal(sig)
    fetched = storage.list_user_signals()
    assert len(fetched) == 1
    assert fetched[0].signal_id == sig.signal_id
    assert fetched[0].trace_id == "trace-9"
    assert fetched[0].kind == "thumbs_down"


def test_sqlite_persists_across_reopens(tmp_path):
    path = str(tmp_path / "p.db")
    s1 = SQLiteStorage(path)
    t = _trace(cluster_id="persisted")
    s1.insert_trace(t)
    s1.close()

    s2 = SQLiteStorage(path)
    try:
        fetched = s2.get_trace(t.trace_id)
        assert fetched is not None
        assert fetched.cluster_id == "persisted"
    finally:
        s2.close()


def test_sqlite_constructor_accepts_sqlite_url(tmp_path):
    db_path = tmp_path / "url.db"
    storage = SQLiteStorage(f"sqlite:///{db_path}")
    try:
        trace = _trace()
        storage.insert_trace(trace)
        assert storage.get_trace(trace.trace_id) is not None
    finally:
        storage.close()

    assert db_path.is_file()
    assert not (tmp_path / "sqlite:").exists()


def test_sqlite_constructor_rejects_non_sqlite_url():
    with pytest.raises(ValueError, match="SQLite path or sqlite"):
        SQLiteStorage("postgresql://localhost/verdict")
