"""Current-state conversation comparison through real storage adapters."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.conversation_assessments import validate_assessment, validate_rubric
from verdict.conversation_monitoring import comparison_query, preview_conversation_comparison
from verdict.conversations import validate_conversation
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage


@contextmanager
def adapter(kind, tmp_path):
    if kind == "postgres":
        dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
        if not dsn:
            pytest.skip(reason)
        with isolated_test_dsn(dsn) as isolated:
            store = PostgresStorage(isolated, min_pool=1, max_pool=2)
            try:
                yield store
            finally:
                store.close()
        return
    store = InMemoryStorage() if kind == "memory" else SQLiteStorage(str(tmp_path / "compare.db"))
    try:
        yield store
    finally:
        store.close()


def snapshot(index, at, *, labels=None, tenant="alpha"):
    payload = {
        "id": f"{index:032x}", "tenant_id": tenant, "source_scope": "b" * 16,
        "messages": [{"role": "user", "content": "Synthetic question."},
                     {"role": "assistant", "content": "Synthetic answer."}],
        "event_at": at, "end_status": "complete", "input_issues": [],
    }
    if labels is not None:
        payload["labels"] = labels
    return validate_conversation(payload)


def rubric(kind="binary", *, name="quality"):
    dimension = {"name": name, "description": "Addresses the request.", "type": kind}
    if kind == "number":
        dimension.update(min=0, max=5, passThreshold=3)
    return validate_rubric({"name": "quality", "version": "1", "target": "conversation",
                            "dimensions": [dimension]})


def grade(row, state, *, kind="binary", name="quality"):
    rule = rubric(kind, name=name)
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rule["fingerprint"],
                "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    return validate_assessment({
        "tenant_id": row["tenant_id"], "conversation_id": row["id"], "revision": row["revision"],
        "target_position": None, "rubric": rule, "evaluator": identity,
        "status": "error" if state == "error" else "completed",
        "dimensions": {} if state == "error" else {name: {
            "state": state, "score": (4 if state == "pass" else 1) if kind == "number" else None,
            "reason": "Synthetic judgment.",
        }},
        "findings": [], "evaluated_at": "2026-09-10T00:00:00Z",
    }, row)


def request(fingerprint, *, label_key="group", dimension="quality"):
    return {
        "analysisUnit": "conversation",
        "referenceStart": "2026-09-01T00:00:00Z", "referenceEnd": "2026-09-03T00:00:00Z",
        "currentStart": "2026-09-03T00:00:00Z", "currentEnd": "2026-09-05T00:00:00Z",
        "evaluatorFingerprint": fingerprint, "dimension": dimension, "labelKey": label_key,
    }


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_two_windows_use_exact_current_grade_and_partition_selected_label(kind, tmp_path):
    with adapter(kind, tmp_path) as store:
        rows = [
            snapshot(1, "2026-09-01T00:00:00Z", labels={"group": "one", "persona": "avatar_a"}),
            snapshot(2, "2026-09-02T12:00:00Z", labels={"group": "two"}),
            snapshot(3, "2026-09-03T00:00:00Z", labels={"group": "one"}),
            snapshot(4, "2026-09-04T12:00:00Z", labels={"group": "one"}),
            snapshot(5, "2026-09-04T13:00:00Z"),
            snapshot(6, "2026-09-05T00:00:00Z"),
            snapshot(7, None),
            snapshot(8, "2026-09-04T14:00:00Z", labels={"group": "two"}),
        ]
        for row in rows:
            store.save_conversation(row)
        grades = [grade(rows[0], "pass"), grade(rows[1], "fail"),
                  grade(rows[2], "pass"), grade(rows[3], "unclear"),
                  grade(rows[7], "error")]
        for item in grades:
            store.save_conversation_assessment(item)
        result = preview_conversation_comparison(store, tenant_id="alpha",
                                                 payload=request(grades[0]["evaluator_fingerprint"]))
        assert result["reference"]["captured"] == 2
        assert result["reference"]["passRate"] == 0.5
        assert result["current"]["captured"] == 4
        assert result["current"]["passRate"] == 1.0
        assert result["current"]["unclear"] == 1
        assert result["current"]["error"] == 1
        assert result["current"]["ungraded"] == 1
        assert result["effect"] == 0.5
        assert sum(group["reference"]["captured"] for group in result["groups"]) == 2
        assert sum(group["current"]["captured"] for group in result["groups"]) == 4
        assert {group["label"] for group in result["groups"]} == {"one", "two", "(missing label)"}
        assert result["reference"]["interval"][0] is not None
        assert result["method"] == "current_snapshot_binary_v1"


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_labels_only_correction_removes_old_grade_from_comparison(kind, tmp_path):
    with adapter(kind, tmp_path) as store:
        row = snapshot(8, "2026-09-01T12:00:00Z", labels={"group": "one"})
        stored = grade(row, "pass")
        store.save_conversation(row)
        store.save_conversation_assessment(stored)
        changed = snapshot(8, "2026-09-01T12:00:00Z", labels={"group": "two"})
        store.save_conversation(changed)
        result = preview_conversation_comparison(store, tenant_id="alpha",
                                                 payload=request(stored["evaluator_fingerprint"]))
        assert result["reference"]["pass"] == 0
        assert result["reference"]["ungraded"] == 1
        assert result["groups"][0]["label"] == "two"


@pytest.mark.parametrize("boundary", ["2026-09-03T00:00:00Z", "2026-09-02T00:00:00Z"])
def test_comparison_rejects_overlapping_or_reversed_windows(boundary):
    payload = request("a" * 64)
    payload["referenceEnd"] = boundary
    if boundary.startswith("2026-09-02"):
        payload["referenceStart"] = "2026-09-03T00:00:00Z"
    else:
        payload["currentStart"] = "2026-09-02T12:00:00Z"
    with pytest.raises(ValueError, match="windows"):
        comparison_query("alpha", payload)


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_numeric_and_response_grades_never_become_binary_conversation_observations(kind, tmp_path):
    with adapter(kind, tmp_path) as store:
        row = snapshot(11, "2026-09-01T12:00:00Z")
        store.save_conversation(row)
        numeric = grade(row, "pass", kind="number")
        store.save_conversation_assessment(numeric)
        with pytest.raises(ValueError, match="binary grade"):
            preview_conversation_comparison(store, tenant_id="alpha",
                payload=request(numeric["evaluator_fingerprint"], label_key=""))
        response_rubric = validate_rubric({"name": "reply", "version": "1", "target": "response",
            "dimensions": [{"name": "quality", "description": "Addresses the request."}]})
        identity = {"provider": "local", "model": "synthetic",
                    "rubric_fingerprint": response_rubric["fingerprint"],
                    "prompt_version": "conversation_v1", "max_output_tokens": 2048}
        response = validate_assessment({"tenant_id": "alpha", "conversation_id": row["id"],
            "revision": row["revision"], "target_position": 1, "rubric": response_rubric,
            "evaluator": identity, "status": "completed", "dimensions": {
                "quality": {"state": "fail", "reason": "Synthetic judgment."}}, "findings": [],
            "evaluated_at": "2026-09-10T00:00:00Z"}, row)
        store.save_conversation_assessment(response)
        result = preview_conversation_comparison(store, tenant_id="alpha",
            payload=request(response["evaluator_fingerprint"], label_key=""))
        assert result["reference"]["ungraded"] == 1
        assert result["reference"]["evaluable"] == 0


def test_group_mix_shift_does_not_change_within_group_rates(tmp_path):
    with adapter("memory", tmp_path) as store:
        fingerprint = None
        for index in range(20):
            reference = index < 10
            group = "one" if (index < 9 if reference else index == 10) else "two"
            row = snapshot(index + 100,
                "2026-09-01T12:00:00Z" if reference else "2026-09-03T12:00:00Z",
                labels={"group": group})
            result = grade(row, "pass" if group == "one" else "fail")
            store.save_conversation(row)
            store.save_conversation_assessment(result)
            fingerprint = result["evaluator_fingerprint"]
        compared = preview_conversation_comparison(store, tenant_id="alpha",
                                                 payload=request(fingerprint))
        assert compared["reference"]["passRate"] == 0.9
        assert compared["current"]["passRate"] == 0.1
        assert compared["effect"] == -0.8
        assert all(group["effect"] == 0 for group in compared["groups"])
        assert next(group for group in compared["groups"] if group["value"] == "one")["currentShare"] == 0.1


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_dimension_path_and_tenant_isolation(kind, tmp_path):
    with adapter(kind, tmp_path) as store:
        own = snapshot(300, "2026-09-01T12:00:00Z")
        other = snapshot(301, "2026-09-01T12:00:00Z", tenant="beta")
        name = "quality.score/v1"
        own_grade = grade(own, "fail", name=name)
        store.save_conversation(own)
        store.save_conversation(other)
        store.save_conversation_assessment(own_grade)
        store.save_conversation_assessment(grade(other, "pass", name=name))
        result = preview_conversation_comparison(store, tenant_id="alpha",
            payload=request(own_grade["evaluator_fingerprint"], label_key="", dimension=name))
        assert result["reference"]["captured"] == 1
        assert result["reference"]["fail"] == 1
        assert result["reference"]["pass"] == 0
        assert result["current"]["captured"] == 0
        assert result["current"]["passRate"] is None
        assert result["effect"] is None


def test_indexed_sqlite_windows_survive_5001_lifetime_rows_and_fail_closed_on_overflow(tmp_path):
    with adapter("sqlite", tmp_path) as store:
        outside = [snapshot(index + 1_000, "2026-08-01T00:00:00Z") for index in range(5_001)]
        inside = [snapshot(9_000, "2026-09-01T00:00:00Z"),
                  snapshot(9_001, "2026-09-03T00:00:00Z")]
        with store._lock:
            store._conn.execute("BEGIN IMMEDIATE")
            store._conn.executemany(
                "INSERT INTO conversation_snapshots (tenant_id,conversation_id,retention_at,payload) VALUES (?,?,?,?)",
                [("alpha", row["id"], row["event_at"], json.dumps(row)) for row in outside + inside],
            )
            store._conn.execute("COMMIT")
            plan = store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT conversation_id FROM conversation_snapshots "
                "WHERE tenant_id=? AND json_extract(payload,'$.event_at')>=? "
                "AND json_extract(payload,'$.event_at')<? "
                "ORDER BY json_extract(payload,'$.event_at'),conversation_id LIMIT 100",
                ("alpha", "2026-09-01T00:00:00+00:00", "2026-09-03T00:00:00+00:00"),
            ).fetchall()
        assert any("conversation_event_window" in row[3] for row in plan)
        result = preview_conversation_comparison(store, tenant_id="alpha",
                                                 payload=request("a" * 64, label_key=""))
        assert result["reference"]["captured"] == 1
        assert result["current"]["captured"] == 1
        with store._lock:
            store._conn.execute("BEGIN IMMEDIATE")
            selected = [snapshot(index + 20_000, "2026-09-01T12:00:00Z")
                        for index in range(10_001)]
            store._conn.executemany(
                "INSERT INTO conversation_snapshots (tenant_id,conversation_id,retention_at,payload) VALUES (?,?,?,?)",
                [("alpha", row["id"], row["event_at"], json.dumps(row)) for row in selected],
            )
            store._conn.execute("COMMIT")
        with pytest.raises(ValueError, match="exceed 10,000"):
            preview_conversation_comparison(store, tenant_id="alpha",
                                             payload=request("a" * 64, label_key=""))


def test_live_postgres_window_uses_expression_index(tmp_path):
    with adapter("postgres", tmp_path) as store:
        outside = [snapshot(index + 30_000, "2026-08-01T00:00:00Z") for index in range(5_001)]
        inside = [snapshot(40_000, "2026-09-01T00:00:00Z"),
                  snapshot(40_001, "2026-09-03T00:00:00Z")]
        with store._pool.connection() as connection, connection.transaction():
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO conversation_snapshots (tenant_id,conversation_id,retention_at,payload) "
                    "VALUES (%s,%s,%s::timestamptz,%s)",
                    [("alpha", row["id"], row["event_at"], json.dumps(row)) for row in outside + inside],
                )
                cursor.execute("ANALYZE conversation_snapshots")
        plan = store._fetchall(
            "EXPLAIN SELECT conversation_id FROM conversation_snapshots "
            "WHERE tenant_id=%s AND ((payload::jsonb)->>'event_at') COLLATE \"C\">=%s "
            "AND ((payload::jsonb)->>'event_at') COLLATE \"C\"<%s "
            "ORDER BY ((payload::jsonb)->>'event_at') COLLATE \"C\",conversation_id LIMIT 100",
            ("alpha", "2026-09-01T00:00:00+00:00", "2026-09-03T00:00:00+00:00"),
        )
        assert "conversation_event_window" in " ".join(line for (line,) in plan)
        result = preview_conversation_comparison(store, tenant_id="alpha",
                                                 payload=request("a" * 64, label_key=""))
        assert result["reference"]["captured"] == 1
        assert result["current"]["captured"] == 1
