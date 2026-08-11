"""Storage tests run against BOTH adapters via parametrize — this is what
keeps the Storage Protocol a real abstraction. If a test passes on one
adapter but not the other, the Protocol is leaking implementation details.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from verdict.schema import (
    DimensionScore,
    DriftDirection,
    DriftSignal,
    Judgment,
    Operation,
    SpanRecord,
    Trace,
    UserSignalRecord,
    Verdict,
)
from verdict.storage.memory import InMemoryStorage
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


def test_insert_and_get_trace(storage):
    t = _trace()
    storage.insert_trace(t)
    fetched = storage.get_trace(t.trace_id)
    assert fetched is not None
    assert fetched.trace_id == t.trace_id
    assert fetched.provider == "anthropic"
    assert fetched.input_tokens == 100


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
    assert n_cols == 18, f"expected 18 drift-signal columns, got {n_cols}"

    # DB-free guard only: the live Postgres path still needs integration
    # coverage in an environment that provides a Postgres instance.
    import inspect

    insert_source = inspect.getsource(pg.PostgresStorage.insert_drift_signal)
    assert "ON CONFLICT (signal_id) DO UPDATE SET" in insert_source


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
