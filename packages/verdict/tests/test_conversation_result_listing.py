"""Current stored conversation grades are discoverable without a known fingerprint."""

from __future__ import annotations

import os
from contextlib import nullcontext

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.conversation_assessments import validate_assessment, validate_rubric
from verdict.conversations import validate_conversation
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage


def _conversation(number: int, tenant: str = "alpha", answer: str = "Synthetic answer.") -> dict:
    return validate_conversation({
        "id": f"{number:032x}", "tenant_id": tenant, "source_scope": "a" * 16,
        "messages": [{"role": "user", "content": "Synthetic question."},
                     {"role": "assistant", "content": answer}],
        "event_at": "2026-09-01T12:00:00Z", "end_status": "complete", "input_issues": [],
        "labels": {"display": f"Synthetic conversation {number}"},
    })


def _grade(row: dict, version: str = "1", status: str = "completed") -> dict:
    rubric = validate_rubric({
        "name": "sample_quality", "version": version, "target": "conversation",
        "dimensions": [{"name": "helpful", "description": "Addresses the request."}],
    })
    return validate_assessment({
        "tenant_id": row["tenant_id"], "conversation_id": row["id"], "revision": row["revision"],
        "target_position": None, "rubric": rubric,
        "evaluator": {"provider": "local", "model": "synthetic-model",
                      "rubric_fingerprint": rubric["fingerprint"],
                      "prompt_version": "conversation_v1", "max_output_tokens": 2048},
        "status": status,
        "dimensions": {"helpful": {"state": "pass", "reason": "Synthetic reason."}}
        if status == "completed" else {},
        "findings": [], "evaluated_at": "2026-09-01T12:01:00Z",
        **({"error": "judge_unavailable"} if status == "error" else {}),
    }, row)


@pytest.mark.parametrize("adapter", ["memory", "buffered", "sqlite", "postgres"])
def test_result_listing_filters_before_paging_and_keeps_identities_separate(adapter, tmp_path):
    if adapter == "postgres":
        dsn, reason = validate_test_dsn(os.environ.get("VERDICT_TEST_POSTGRES_DSN"), allow_any_database=False)
        if not dsn:
            pytest.skip(reason)
        context = isolated_test_dsn(dsn)
    else:
        context = nullcontext()
    with context as isolated:
        store = (InMemoryStorage() if adapter == "memory" else
                 BufferedStorage(InMemoryStorage()) if adapter == "buffered" else
                 SQLiteStorage(str(tmp_path / "grades.db")) if adapter == "sqlite" else
                 PostgresStorage(isolated, min_pool=1, max_pool=2))
        try:
            assert store.list_conversation_evaluators("alpha") == ([], None)
            for number in range(1, 24):
                store.save_conversation(_conversation(number))
            first = _conversation(22)
            second = _conversation(23)
            foreign = _conversation(23, "beta")
            store.save_conversation(foreign)
            first_grade = _grade(first)
            second_grade = _grade(second, status="error")
            other_identity = _grade(second, version="2")
            store.save_conversation_assessment(first_grade)
            store.save_conversation_assessment(second_grade)
            store.save_conversation_assessment(other_identity)
            store.save_conversation_assessment(_grade(foreign, version="3"))

            identities, cursor = store.list_conversation_evaluators("alpha", limit=1)
            assert len(identities) == 1 and cursor == identities[0]["evaluator_fingerprint"]
            later, last_cursor = store.list_conversation_evaluators("alpha", after=cursor, limit=1)
            assert len(later) == 1 and last_cursor is None
            assert {item["evaluator_fingerprint"] for item in identities + later} == {
                first_grade["evaluator_fingerprint"], other_identity["evaluator_fingerprint"]}
            assert store.list_conversation_evaluators("beta")[0][0]["rubric"]["version"] == "3"

            pages, cursor = store.list_graded_conversations(
                "alpha", first_grade["evaluator_fingerprint"], limit=1)
            assert [item["id"] for item in pages] == [first["id"]]
            assert pages[0]["completedCount"] == 1 and pages[0]["errorCount"] == 0
            assert pages[0]["displayLabel"] == "Synthetic conversation 22"
            assert "labels" not in pages[0]
            assert cursor == first["id"]
            next_page, cursor = store.list_graded_conversations(
                "alpha", first_grade["evaluator_fingerprint"], after=cursor, limit=1)
            assert [item["id"] for item in next_page] == [second["id"]]
            assert next_page[0]["completedCount"] == 0 and next_page[0]["errorCount"] == 1
            assert cursor is None
            assert store.list_graded_conversations("beta", first_grade["evaluator_fingerprint"]) == ([], None)

            corrected = _conversation(22, answer="Corrected synthetic answer.")
            store.save_conversation(corrected)
            current, _ = store.list_graded_conversations("alpha", first_grade["evaluator_fingerprint"])
            assert [item["id"] for item in current] == [second["id"]]
        finally:
            store.close()


def test_result_listing_finds_grade_after_five_thousand_ungraded_conversations(tmp_path):
    store = SQLiteStorage(str(tmp_path / "many-conversations.db"))
    try:
        for number in range(1, 5_002):
            store.save_conversation(_conversation(number))
        graded = _conversation(5_001)
        grade = _grade(graded)
        store.save_conversation_assessment(grade)
        identities, _ = store.list_conversation_evaluators("alpha")
        rows, _ = store.list_graded_conversations("alpha", grade["evaluator_fingerprint"])
        assert [item["evaluator_fingerprint"] for item in identities] == [grade["evaluator_fingerprint"]]
        assert [item["id"] for item in rows] == [graded["id"]]
    finally:
        store.close()
