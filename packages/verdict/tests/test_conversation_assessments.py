"""Conversation grading contracts, exercised through current snapshot evidence."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.conversation_assessments import (
    assessment_coverage,
    evaluation_targets,
    validate_assessment,
    validate_rubric,
)
from verdict.conversations import validate_conversation
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage


def snapshot(messages, *, end_status="complete", input_issues=None):
    return validate_conversation({
        "id": "a" * 32,
        "tenant_id": "alpha",
        "source_scope": "b" * 16,
        "messages": messages,
        "event_at": "2026-09-01T12:00:00Z",
        "end_status": end_status,
        "input_issues": input_issues or [],
    })


def rubric(target="response"):
    return validate_rubric({
        "name": "sample_quality",
        "version": "1",
        "target": target,
        "dimensions": [{"name": "helpful", "description": "Addresses the request."}],
    })


def grade_for(row):
    rule = rubric()
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"],
                "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    return validate_assessment({
        "tenant_id": row["tenant_id"], "conversation_id": row["id"],
        "revision": row["revision"], "target_position": 1, "rubric": rule,
        "evaluator": identity, "status": "completed",
        "dimensions": {"helpful": {"state": "pass", "reason": "Good."}},
        "findings": [], "evaluated_at": "2026-09-01T12:01:00Z",
    }, row)


@pytest.mark.parametrize(("messages", "ending", "issues", "expected", "reason"), [
    ([{"role": "user", "content": "Question."}], "complete", [], (), "no_completed_reply"),
    ([{"role": "assistant", "content": "Answer."}], "complete", [], (), "no_completed_reply"),
    ([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Partial.", "status": "interrupted"}], "complete", [], (), "no_completed_reply"),
    ([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Answer."}, {"role": "user", "content": "Follow up."}], "complete", [], (1,), None),
    ([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Answer."}], "open", [], (), "not_closed"),
    ([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Answer."}], "complete", ["truncated_transcript"], (), "incomplete_evidence"),
])
def test_targets_share_one_terminal_eligibility_rule(messages, ending, issues, expected, reason):
    row = snapshot(messages, end_status=ending, input_issues=issues)
    assert evaluation_targets(row, rubric()) == (expected, reason)
    assert evaluation_targets(row, rubric("conversation")) == (
        (None,) if expected else (), reason
    )


def test_partial_coverage_never_claims_fully_graded():
    row = snapshot([
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
        {"role": "user", "content": "Second question."},
        {"role": "assistant", "content": "Second answer."},
    ])
    rule = rubric()
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"], "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    first = validate_assessment({
        "tenant_id": "alpha", "conversation_id": row["id"],
        "revision": row["revision"], "target_position": 1,
        "rubric": rule, "evaluator": identity,
        "status": "completed", "dimensions": {"helpful": {"state": "pass", "reason": "Good."}},
        "findings": [], "evaluated_at": "2026-09-01T12:01:00Z",
    }, row)
    coverage = assessment_coverage(row, rule, first["evaluator_fingerprint"], [first])
    assert coverage["targets"] == 2
    assert coverage["completed"] == 1
    assert coverage["missing"] == 1
    assert coverage["fullyGraded"] is False
    assert coverage["dimensions"]["helpful"] == {"pass": 1, "fail": 0, "unclear": 0}


def test_changed_reply_rejects_old_assessment():
    row = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Answer."}])
    changed = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Changed answer."}])
    rule = rubric()
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"], "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    raw = {"tenant_id": "alpha", "conversation_id": row["id"], "revision": row["revision"], "target_position": 1, "rubric": rule, "evaluator": identity, "status": "completed", "dimensions": {"helpful": {"state": "pass", "reason": "Good."}}, "findings": [], "evaluated_at": "2026-09-01T12:01:00Z"}
    validate_assessment(raw, row)
    with pytest.raises(ValueError, match="revision"):
        validate_assessment(raw, changed)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_storage_correction_invalidates_grade_and_late_result(adapter, tmp_path):
    store = InMemoryStorage() if adapter == "memory" else SQLiteStorage(str(tmp_path / "grades.db"))
    try:
        row = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Answer."}])
        changed = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Corrected answer."}])
        rule = rubric()
        identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"], "prompt_version": "conversation_v1", "max_output_tokens": 2048}
        raw = {"tenant_id": "alpha", "conversation_id": row["id"], "revision": row["revision"], "target_position": 1, "rubric": rule, "evaluator": identity, "status": "completed", "dimensions": {"helpful": {"state": "pass", "reason": "Good."}}, "findings": [], "evaluated_at": "2026-09-01T12:01:00Z"}
        result = validate_assessment(raw, row)
        store.save_conversation(row)
        assert store.save_conversation_assessment(result) is True
        assert store.save_conversation_assessment(result) is False
        assert len(store.list_conversation_assessments("alpha", row["id"], result["evaluator_fingerprint"])) == 1
        store.save_conversation(changed)
        assert store.list_conversation_assessments("alpha", row["id"], result["evaluator_fingerprint"]) == []
        with pytest.raises(ValueError, match="revision"):
            store.save_conversation_assessment(result)
    finally:
        store.close()


def test_live_postgres_assessment_revision_and_retention():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
    if not dsn:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        store = PostgresStorage(isolated, min_pool=1, max_pool=2)
        try:
            row = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Answer."}])
            changed = snapshot([{"role": "user", "content": "Changed question."}, {"role": "assistant", "content": "Answer."}])
            rule = rubric()
            identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"], "prompt_version": "conversation_v1", "max_output_tokens": 2048}
            result = validate_assessment({"tenant_id": "alpha", "conversation_id": row["id"], "revision": row["revision"], "target_position": 1, "rubric": rule, "evaluator": identity, "status": "completed", "dimensions": {"helpful": {"state": "pass", "reason": "Good."}}, "findings": [], "evaluated_at": "2026-09-01T12:01:00Z"}, row)
            store.save_conversation(row)
            assert store.save_conversation_assessment(result)
            assert not store.save_conversation_assessment(result)
            store.save_conversation(changed)
            assert store.list_conversation_assessments("alpha", row["id"], result["evaluator_fingerprint"]) == []
            with pytest.raises(ValueError, match="revision"):
                store.save_conversation_assessment(result)
            revised = validate_assessment({**result, "revision": changed["revision"], "id": None}, changed)
            assert store.save_conversation_assessment(revised)
            assert store.prune_before("2026-09-02T00:00:00Z") == 0
            assert store.get_conversation("alpha", row["id"]) is None
            assert store.list_conversation_assessments("alpha", row["id"], result["evaluator_fingerprint"]) == []
        finally:
            store.close()


def test_postgres_first_insert_race_cannot_leave_stale_grade():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
    if not dsn:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        first_store = PostgresStorage(isolated, min_pool=1, max_pool=2)
        second_store = PostgresStorage(isolated, min_pool=1, max_pool=2)
        paused, resume = threading.Event(), threading.Event()

        class CursorProxy:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                self.cursor.__enter__()
                return self

            def __exit__(self, *args):
                return self.cursor.__exit__(*args)

            def execute(self, sql, params=None):
                if "INSERT INTO conversation_snapshots" in sql:
                    paused.set()
                    if not resume.wait(10):
                        raise TimeoutError("test did not release snapshot UPSERT")
                return self.cursor.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def cursor(self):
                return CursorProxy(self.connection.cursor())

            def transaction(self):
                return self.connection.transaction()

        class PoolProxy:
            def __init__(self, pool):
                self.pool = pool

            @contextmanager
            def connection(self):
                with self.pool.connection() as connection:
                    yield ConnectionProxy(connection)

            def close(self):
                self.pool.close()

        second_store._pool = PoolProxy(second_store._pool)
        original = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "First answer."}])
        changed = snapshot([{"role": "user", "content": "Question."}, {"role": "assistant", "content": "Corrected answer."}])
        rule = rubric()
        identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"], "prompt_version": "conversation_v1", "max_output_tokens": 2048}
        grade = validate_assessment({"tenant_id": "alpha", "conversation_id": original["id"], "revision": original["revision"], "target_position": 1, "rubric": rule, "evaluator": identity, "status": "completed", "dimensions": {"helpful": {"state": "pass", "reason": "Good."}}, "findings": [], "evaluated_at": "2026-09-01T12:01:00Z"}, original)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(second_store.save_conversation, changed)
                assert paused.wait(10), "changed snapshot did not reach its UPSERT"
                first_store.save_conversation(original)
                assert first_store.save_conversation_assessment(grade)
                resume.set()
                future.result(timeout=10)
            assert first_store.get_conversation("alpha", original["id"])["revision"] == changed["revision"]
            assert first_store.list_conversation_assessments("alpha", original["id"], grade["evaluator_fingerprint"]) == []
        finally:
            resume.set()
            first_store.close()
            second_store.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite", "postgres"])
def test_assessment_lifecycle_is_revision_and_tenant_scoped(adapter, tmp_path):
    if adapter == "postgres":
        dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
        if not dsn:
            pytest.skip(reason)
        context = isolated_test_dsn(dsn)
    else:
        context = nullcontext()
    with context as isolated:
        store = (InMemoryStorage() if adapter == "memory" else
                 SQLiteStorage(str(tmp_path / "lifecycle.db")) if adapter == "sqlite" else
                 PostgresStorage(isolated, min_pool=1, max_pool=2))
        try:
            with pytest.raises(ValueError, match="assessment"):
                store.save_conversation_assessment(None)
            original = snapshot([{"role": "user", "content": "Question."},
                                 {"role": "assistant", "content": "Answer A."}])
            changed = snapshot([{"role": "user", "content": "Question."},
                                {"role": "assistant", "content": "Answer B."}])
            other_tenant = validate_conversation({**original, "tenant_id": "beta", "revision": None})
            first, second, other = grade_for(original), grade_for(changed), grade_for(other_tenant)
            store.save_conversation(original)
            store.save_conversation(other_tenant)
            assert store.save_conversation_assessment(first)
            assert store.save_conversation_assessment(other)
            store.save_conversation(original)
            assert [a["revision"] for a in store.list_conversation_assessments(
                "alpha", original["id"], first["evaluator_fingerprint"])] == [original["revision"]]
            store.save_conversation(changed)
            assert store.list_conversation_assessments("alpha", original["id"], first["evaluator_fingerprint"]) == []
            assert len(store.list_conversation_assessments("beta", original["id"], other["evaluator_fingerprint"])) == 1
            assert store.save_conversation_assessment(second)
            store.save_conversation(original)
            assert store.list_conversation_assessments("alpha", original["id"], first["evaluator_fingerprint"]) == []
            store.delete_conversation("alpha", original["id"])
            assert store.get_conversation("alpha", original["id"]) is None
            assert store.list_conversation_assessments("alpha", original["id"], first["evaluator_fingerprint"]) == []
            assert store.get_conversation("beta", original["id"]) is not None
            store.save_conversation(original)
            assert store.list_conversation_assessments("alpha", original["id"], first["evaluator_fingerprint"]) == []
        finally:
            store.close()


@pytest.mark.parametrize("adapter", ["sqlite", "postgres"])
def test_correction_rolls_back_snapshot_when_assessment_deletion_fails(adapter, tmp_path):
    if adapter == "postgres":
        dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
        if not dsn:
            pytest.skip(reason)
        context = isolated_test_dsn(dsn)
    else:
        context = nullcontext()
    with context as isolated:
        store = (SQLiteStorage(str(tmp_path / "rollback.db")) if adapter == "sqlite" else
                 PostgresStorage(isolated, min_pool=1, max_pool=2))
        try:
            original = snapshot([{"role": "user", "content": "Question."},
                                 {"role": "assistant", "content": "Answer A."}])
            changed = snapshot([{"role": "user", "content": "Question."},
                                {"role": "assistant", "content": "Answer B."}])
            grade = grade_for(original)
            store.save_conversation(original)
            store.save_conversation_assessment(grade)
            if adapter == "sqlite":
                store._conn.execute("""CREATE TRIGGER fail_assessment_delete BEFORE DELETE ON conversation_assessments
                                       BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END""")
            else:
                with store._pool.connection() as conn:
                    conn.execute("""CREATE FUNCTION fail_assessment_delete() RETURNS trigger AS $$
                                    BEGIN RAISE EXCEPTION 'injected delete failure'; END $$ LANGUAGE plpgsql""")
                    conn.execute("""CREATE TRIGGER fail_assessment_delete BEFORE DELETE ON conversation_assessments
                                    FOR EACH ROW EXECUTE FUNCTION fail_assessment_delete()""")
            with pytest.raises(Exception, match="injected delete failure"):
                store.save_conversation(changed)
            assert store.get_conversation("alpha", original["id"])["revision"] == original["revision"]
            assert [a["revision"] for a in store.list_conversation_assessments(
                "alpha", original["id"], grade["evaluator_fingerprint"])] == [original["revision"]]
        finally:
            store.close()


def test_live_postgres_assessment_transaction_is_read_committed():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
    if not dsn:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        store = PostgresStorage(isolated, min_pool=1, max_pool=2)
        try:
            with store._pool.connection() as conn, conn.transaction():
                assert conn.execute("SHOW transaction_isolation").fetchone()[0] == "read committed"
        finally:
            store.close()


def test_postgres_correction_waits_for_grade_then_removes_old_revision():
    dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
    if not dsn:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as isolated:
        advisory_key = int(hashlib.sha256(isolated.encode()).hexdigest()[:12], 16)
        grader = PostgresStorage(isolated, min_pool=1, max_pool=2)
        corrector = PostgresStorage(isolated, min_pool=1, max_pool=2)
        lock = PostgresStorage(isolated, min_pool=1, max_pool=2)
        original = snapshot([{"role": "user", "content": "Question."},
                             {"role": "assistant", "content": "Answer A."}])
        changed = snapshot([{"role": "user", "content": "Question."},
                            {"role": "assistant", "content": "Answer B."}])
        grade = grade_for(original)
        grader.save_conversation(original)
        with lock._pool.connection() as conn:
            conn.execute(f"""CREATE FUNCTION hold_assessment() RETURNS trigger AS $$
                            BEGIN PERFORM pg_advisory_xact_lock({advisory_key}); RETURN NEW; END $$ LANGUAGE plpgsql""")
            conn.execute("""CREATE TRIGGER hold_assessment BEFORE INSERT ON conversation_assessments
                            FOR EACH ROW EXECUTE FUNCTION hold_assessment()""")
            conn.execute("SELECT pg_advisory_lock(%s)", (advisory_key,))
            with ThreadPoolExecutor(max_workers=2) as executor:
                try:
                    grade_future = executor.submit(grader.save_conversation_assessment, grade)

                    def waiting(fragment):
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            count = conn.execute(
                                """SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock'
                                   AND query LIKE %s""", (f"%{fragment}%",)
                            ).fetchone()[0]
                            if count:
                                return
                            time.sleep(0.02)
                        raise AssertionError(f"database operation did not block: {fragment}")

                    waiting("INSERT INTO conversation_assessments")
                    correction_future = executor.submit(corrector.save_conversation, changed)
                    waiting("INSERT INTO conversation_snapshots")
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (advisory_key,))
                assert grade_future.result(timeout=10) is True
                correction_future.result(timeout=10)
            assert corrector.get_conversation("alpha", original["id"])["revision"] == changed["revision"]
            assert corrector.list_conversation_assessments(
                "alpha", original["id"], grade["evaluator_fingerprint"]
            ) == []
        grader.close()
        corrector.close()
        lock.close()
