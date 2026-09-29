"""Source-declared paired conversation comparison through the three stores."""

from __future__ import annotations

import os
from contextlib import contextmanager

import httpx
import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.conversation_assessments import validate_assessment, validate_rubric
from verdict.conversations import validate_conversation
from verdict.dashboard.app import create_app
from verdict.matched_conversations import matched_query, preview_matched_conversations
from verdict.storage.memory import InMemoryStorage
from verdict.storage.postgres import PostgresStorage
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import import_into_storage
from verdict.telemetry.sources.voice import map_voice_conversation


@contextmanager
def store_for(kind, tmp_path):
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
    store = InMemoryStorage() if kind == "memory" else SQLiteStorage(str(tmp_path / "matched.db"))
    try:
        yield store
    finally:
        store.close()


def conversation(index, *, pair=None, variant="left", tenant="alpha", at="2026-09-02T12:00:00Z",
                 answer="Synthetic answer.", end_status="complete"):
    labels = {"variant": variant}
    if pair is not None:
        labels["pair_id"] = pair
    return validate_conversation({
        "id": f"{index:032x}", "tenant_id": tenant, "source_scope": "b" * 16,
        "messages": [{"role": "user", "content": "Synthetic question."},
                     {"role": "assistant", "content": answer}],
        "labels": labels, "event_at": at, "end_status": end_status, "input_issues": [],
    })


def grade(row, value, *, numeric=False, threshold=True, direction="higher_is_better"):
    dimension = {"name": "quality", "description": "Addresses the request.",
                 "type": "number" if numeric else "binary"}
    if numeric:
        dimension.update(min=0, max=5, direction=direction)
        if threshold:
            dimension["passThreshold"] = 3
    rubric = validate_rubric({"name": "quality", "version": "1", "target": "conversation",
                              "dimensions": [dimension]})
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rubric["fingerprint"],
                "prompt_version": "pair_v1", "max_output_tokens": 2048}
    passing = (value >= 3 if direction == "higher_is_better" else value <= 3) if numeric else None
    state = ("pass" if passing else "fail") if numeric and threshold else (
        "unclear" if numeric else value)
    return validate_assessment({
        "tenant_id": row["tenant_id"], "conversation_id": row["id"], "revision": row["revision"],
        "target_position": None, "rubric": rubric, "evaluator": identity,
        "status": "completed", "dimensions": {"quality": {
            "state": state, "score": value if numeric else None, "reason": "Synthetic grade.",
        }}, "findings": [], "evaluated_at": "2026-09-03T00:00:00Z",
    }, row)


def request(fingerprint):
    return {"analysisUnit": "matched_conversation",
            "windowStart": "2026-09-01T00:00:00Z", "windowEnd": "2026-09-04T00:00:00Z",
            "evaluatorFingerprint": fingerprint, "dimension": "quality", "pairKey": "pair_id",
            "variantKey": "variant", "leftVariant": "left", "rightVariant": "right"}


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_binary_pairing_excludes_ambiguous_and_unmatched_cases(kind, tmp_path):
    with store_for(kind, tmp_path) as store:
        rows = [conversation(1, pair="a"), conversation(2, pair="a", variant="right"),
                conversation(3, pair="b"), conversation(4, pair="b", variant="right"),
                conversation(5, pair="b", variant="right"), conversation(6, pair="c"),
                conversation(7, variant="right"), conversation(8, pair="a", variant="other"),
                conversation(9, pair="a", variant="right", tenant="beta"),
                conversation(10, pair="d", at="2026-09-04T00:00:00Z")]
        for row in rows:
            store.save_conversation(row)
        for item in [grade(rows[0], "pass"), grade(rows[1], "fail"),
                     grade(rows[2], "fail"), grade(rows[3], "pass")]:
            store.save_conversation_assessment(item)
        result = preview_matched_conversations(store, tenant_id="alpha",
                                               payload=request(grade(rows[0], "pass")["evaluator_fingerprint"]))
        assert result["candidateRows"] == 7
        assert result["declaredPairs"] == 1
        assert result["usablePairs"] == 1
        assert result["binary"]["leftPassRightFail"] == 1
        assert result["exclusions"]["duplicatePairIds"] == 1
        assert result["exclusions"]["unmatchedPairIds"] == 1
        assert result["exclusions"]["missingPairRows"] == 1
        assert result["examples"][0]["left"]["revision"] == rows[0]["revision"]


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_numeric_scores_without_threshold_remain_usable_and_corrections_invalidate_grade(kind, tmp_path):
    with store_for(kind, tmp_path) as store:
        left = conversation(11, pair="n")
        right = conversation(12, pair="n", variant="right")
        for row, score in [(left, 2), (right, 4)]:
            store.save_conversation(row)
            store.save_conversation_assessment(grade(row, score, numeric=True, threshold=False))
        payload = request(grade(left, 2, numeric=True, threshold=False)["evaluator_fingerprint"])
        result = preview_matched_conversations(store, tenant_id="alpha", payload=payload)
        assert result["usablePairs"] == 1
        assert result["numeric"]["meanRightMinusLeft"] == 2
        assert result["numeric"]["deltaInterval95"] == [-5, 5]
        corrected = conversation(12, pair="n", variant="right", answer="Corrected synthetic answer.")
        store.save_conversation(corrected)
        result = preview_matched_conversations(store, tenant_id="alpha", payload=payload)
        assert result["usablePairs"] == 0
        assert result["exclusions"]["ungradedPairs"] == 1


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_partial_pair_and_technical_failure_coverage_are_explicit(kind, tmp_path):
    with store_for(kind, tmp_path) as store:
        rows = [conversation(50, pair="error"), conversation(51, pair="error", variant="right"),
                conversation(52, pair="pending"), conversation(53, pair="pending", variant="right"),
                conversation(54, pair="stopped", end_status="technical_failure"),
                conversation(55, pair="stopped", variant="right")]
        for row in rows:
            store.save_conversation(row)
        good = grade(rows[0], "pass")
        store.save_conversation_assessment(good)
        failed = {**grade(rows[1], "pass"), "status": "error", "dimensions": {},
                  "error": "judge_unavailable"}
        store.save_conversation_assessment(validate_assessment(failed, rows[1]))
        for row in rows[4:]:
            store.save_conversation_assessment(grade(row, "pass"))
        result = preview_matched_conversations(store, tenant_id="alpha",
                                               payload=request(good["evaluator_fingerprint"]))
        assert result["declaredPairs"] == 3
        assert result["usablePairs"] == 1
        assert result["technicalFailurePairs"] == 1
        assert result["exclusions"]["errorPairs"] == 1
        assert result["exclusions"]["ungradedPairs"] == 1


def test_voice_import_to_stored_grades_to_matched_comparison(tmp_path):
    with store_for("sqlite", tmp_path) as store:
        context = ImportContext(adapter="file", source_scope="paired-example", tenant_id="alpha")
        for variant in ("left", "right"):
            source = {"conversation_id": f"source-{variant}", "end_status": "complete",
                      "ended_at": "2026-09-02T12:00:00Z",
                      "labels": {"pair_id": "opaque_case_1", "variant": variant},
                      "turns": [{"speaker": "caller", "text": "Synthetic scheduling request."},
                                {"speaker": "agent", "text": "Synthetic response.", "status": "completed",
                                 "started_at": "2026-09-02T11:59:59Z",
                                 "ended_at": "2026-09-02T12:00:00Z"}]}
            import_into_storage(map_voice_conversation(source, context), store)
        rows, _ = store.list_conversations("alpha", limit=20)
        assert len(rows) == 2
        for row in rows:
            store.save_conversation_assessment(grade(row, "pass" if row["labels"]["variant"] == "left" else "fail"))
        result = preview_matched_conversations(store, tenant_id="alpha",
                                               payload=request(grade(rows[0], "pass")["evaluator_fingerprint"]))
        assert result["usablePairs"] == 1
        assert result["binary"]["leftPassRightFail"] == 1


def test_selected_variant_overflow_is_bounded_but_other_variants_do_not_count(tmp_path):
    with store_for("memory", tmp_path) as store:
        for index in range(1, 10002):
            store.save_conversation(conversation(index, pair=f"case_{index}"))
        store.save_conversation(conversation(11000, pair="other", variant="unselected"))
        with pytest.raises(ValueError, match="exceeds 10,000 variant rows"):
            preview_matched_conversations(store, tenant_id="alpha", payload=request("a" * 64))


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_lower_is_better_numeric_and_missing_score_are_separate(kind, tmp_path):
    with store_for(kind, tmp_path) as store:
        rows = [conversation(9000, pair="scored"), conversation(9001, pair="scored", variant="right"),
                conversation(9002, pair="missing"), conversation(9003, pair="missing", variant="right")]
        for row in rows:
            store.save_conversation(row)
        for row, score in [(rows[0], 4), (rows[1], 2), (rows[2], 4)]:
            store.save_conversation_assessment(grade(row, score, numeric=True,
                                                     threshold=False, direction="lower_is_better"))
        absent = grade(rows[3], 2, numeric=True, threshold=False, direction="lower_is_better")
        absent["dimensions"]["quality"]["score"] = None
        store.save_conversation_assessment(validate_assessment(absent, rows[3]))
        result = preview_matched_conversations(store, tenant_id="alpha",
            payload=request(grade(rows[0], 4, numeric=True, threshold=False,
                                  direction="lower_is_better")["evaluator_fingerprint"]))
        assert result["usablePairs"] == 1
        assert result["direction"] == "lower_is_better"
        assert result["numeric"]["meanRightMinusLeft"] == -2
        assert result["exclusions"]["missingScorePairs"] == 1


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_valid_extreme_numeric_bounds_keep_pair_inspectable_when_aggregate_is_unrepresentable(kind, tmp_path):
    with store_for(kind, tmp_path) as store:
        left = conversation(9500, pair="extreme")
        right = conversation(9501, pair="extreme", variant="right")
        rubric = validate_rubric({"name": "extreme", "version": "1", "target": "conversation",
            "dimensions": [{"name": "quality", "description": "Bounded synthetic score.",
                            "type": "number", "min": -1e308, "max": 1e308}]})
        identity = {"provider": "local", "model": "synthetic",
                    "rubric_fingerprint": rubric["fingerprint"], "prompt_version": "pair_v1",
                    "max_output_tokens": 2048}
        for row, score in [(left, 0), (right, 1)]:
            store.save_conversation(row)
            stored = validate_assessment({"tenant_id": row["tenant_id"],
                "conversation_id": row["id"], "revision": row["revision"],
                "target_position": None, "rubric": rubric, "evaluator": identity,
                "status": "completed", "dimensions": {"quality": {
                    "state": "unclear", "score": score, "reason": "Synthetic grade."}},
                "findings": [], "evaluated_at": "2026-09-03T00:00:00Z"}, row)
            store.save_conversation_assessment(stored)
        result = preview_matched_conversations(store, tenant_id="alpha",
            payload=request(stored["evaluator_fingerprint"]))
        assert result["usablePairs"] == 1
        assert result["numeric"]["unavailableReason"] == "score_arithmetic_overflow"
        assert result["examples"][0]["left"]["score"] == 0
        assert result["examples"][0]["right"]["score"] == 1


def test_corrupt_boolean_score_fails_closed_at_api_boundary(tmp_path):
    with store_for("sqlite", tmp_path) as store:
        left = conversation(9600, pair="corrupt")
        right = conversation(9601, pair="corrupt", variant="right")
        for row, score in ((left, 2), (right, 4)):
            store.save_conversation(row)
            store.save_conversation_assessment(grade(row, score, numeric=True))
        store._conn.execute(
            "UPDATE conversation_assessments SET payload=json_set(payload, '$.dimensions.quality.score', json('true')) "
            "WHERE tenant_id=? AND conversation_id=?", ("alpha", right["id"]),
        )
        with pytest.raises(ValueError, match="numeric score"):
            preview_matched_conversations(store, tenant_id="alpha",
                payload=request(grade(left, 2, numeric=True)["evaluator_fingerprint"]))


def test_postgres_preserves_boolean_score_type_for_fail_closed_comparison(tmp_path):
    with store_for("postgres", tmp_path) as store:
        left = conversation(9650, pair="corrupt")
        right = conversation(9651, pair="corrupt", variant="right")
        for row, score in ((left, 2), (right, 4)):
            store.save_conversation(row)
            store.save_conversation_assessment(grade(row, score, numeric=True))
        store._exec(
            "UPDATE conversation_assessments SET payload=jsonb_set(payload::jsonb, "
            "'{dimensions,quality,score}', 'true'::jsonb)::text "
            "WHERE tenant_id=%s AND conversation_id=%s", ("alpha", right["id"]),
        )
        with pytest.raises(ValueError, match="numeric score"):
            preview_matched_conversations(store, tenant_id="alpha",
                payload=request(grade(left, 2, numeric=True)["evaluator_fingerprint"]))


def test_corrupt_noncanonical_event_time_cannot_enter_utc_window(tmp_path):
    with store_for("sqlite", tmp_path) as store:
        left = conversation(9700, pair="time")
        right = conversation(9701, pair="time", variant="right")
        for row in (left, right):
            store.save_conversation(row)
            store.save_conversation_assessment(grade(row, "pass"))
        store._conn.execute(
            "UPDATE conversation_snapshots SET payload=json_set(payload, '$.event_at', '2026-09-02T23:00:00-03:00') "
            "WHERE tenant_id=? AND conversation_id=?", ("alpha", right["id"]),
        )
        payload = request(grade(left, "pass")["evaluator_fingerprint"])
        payload["windowStart"] = "2026-09-02T00:00:00Z"
        payload["windowEnd"] = "2026-09-03T00:00:00Z"
        with pytest.raises(ValueError, match="noncanonical event time"):
            preview_matched_conversations(store, tenant_id="alpha", payload=payload)


@pytest.mark.parametrize("changed", [
    {"pairKey": "Bad key"}, {"variantKey": "pair_id"},
    {"leftVariant": "right"}, {"windowEnd": "2026-09-01T00:00:00Z"},
    {"windowStart": "0001-01-01T00:00:00+23:59"},
    {"dimension": "invalid dimension"}, {"extra": "unknown"},
])
def test_malformed_or_ambiguous_pair_requests_fail_closed(changed):
    with pytest.raises(ValueError):
        matched_query("alpha", {**request("a" * 64), **changed})


@pytest.mark.parametrize("kind", ["memory", "sqlite", "postgres"])
def test_older_lifetime_rows_do_not_block_small_selected_study(kind, tmp_path):
    with store_for(kind, tmp_path) as store:
        for index in range(1, 5002):
            store.save_conversation(conversation(index, at="2026-08-01T00:00:00Z"))
        left = conversation(6000, pair="x")
        right = conversation(6001, pair="x", variant="right")
        for row in (left, right):
            store.save_conversation(row)
            store.save_conversation_assessment(grade(row, "pass"))
        result = preview_matched_conversations(store, tenant_id="alpha",
                                               payload=request(grade(left, "pass")["evaluator_fingerprint"]))
        assert result["candidateRows"] == 2
        assert result["usablePairs"] == 1


@pytest.mark.asyncio
async def test_dashboard_pair_api_requires_setup_token_and_tenant_scope(tmp_path):
    path = tmp_path / "pair-api.db"
    store = SQLiteStorage(str(path))
    left = conversation(8000, pair="x")
    right = conversation(8001, pair="x", variant="right")
    for row in (left, right):
        store.save_conversation(row)
        store.save_conversation_assessment(grade(row, "pass"))
    store.close()
    payload = request(grade(left, "pass")["evaluator_fingerprint"])
    app = create_app(storage=f"sqlite:///{path}", tenant_id="alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        denied = await client.post("/api/compare/conversations/matched", json=payload)
        assert denied.status_code == 403
        token = (await client.get("/api/setup/token")).json()["setupToken"]
        response = await client.post("/api/compare/conversations/matched", json=payload,
                                     headers={"X-Verdict-Setup": token})
        assert response.status_code == 200
        assert response.json()["usablePairs"] == 1
        assert "Synthetic answer" not in response.text
        assert len(response.json()["examples"]) == 1
        malformed = await client.post("/api/compare/conversations/matched",
                                      json={**payload, "pairKey": "invalid-key"},
                                      headers={"X-Verdict-Setup": token})
        assert malformed.status_code == 400
    other = create_app(storage=f"sqlite:///{path}", tenant_id="beta")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other), base_url="http://127.0.0.1") as client:
        token = (await client.get("/api/setup/token")).json()["setupToken"]
        response = await client.post("/api/compare/conversations/matched", json=payload,
                                     headers={"X-Verdict-Setup": token})
        assert response.status_code == 200
        assert response.json()["candidateRows"] == 0
