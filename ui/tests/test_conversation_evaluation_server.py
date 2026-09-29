"""Dashboard entry points for rubric preview and current conversation review."""


import httpx
import pytest
from verdict.conversation_assessments import validate_assessment, validate_rubric
from verdict.conversations import validate_conversation
from verdict.dashboard.app import create_app
from verdict.storage.sqlite import SQLiteStorage


@pytest.mark.asyncio
async def test_conversation_preview_and_review_show_partial_coverage(tmp_path):
    path = tmp_path / "conversations.db"
    store = SQLiteStorage(str(path))
    row = validate_conversation({
        "id": "a" * 32, "tenant_id": "alpha", "source_scope": "b" * 16,
        "messages": [
            {"role": "user", "content": "First question."},
            {"role": "assistant", "content": "First answer."},
            {"role": "user", "content": "Second question."},
            {"role": "assistant", "content": "Second answer."},
        ],
        "event_at": "2026-09-01T12:00:00Z", "end_status": "complete", "input_issues": [],
    })
    store.save_conversation(row)
    store.close()
    config = {"unit": "conversation", "provider": "openai", "model": "synthetic-model",
              "maxCalls": 1, "rubric": {"name": "quality", "version": "1", "target": "response",
                         "dimensions": [{"name": "helpful", "description": "Addresses the request."}]}}
    app = create_app(storage=f"sqlite:///{path}", tenant_id="alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        token = (await client.get("/api/setup/token")).json()["setupToken"]
        headers = {"X-Verdict-Setup": token}
        preview = await client.post("/api/evaluators/preview", json=config, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["eligibleTargets"] == 2
        review = await client.post("/api/data/conversations/review", json=config, headers=headers)
        assert review.status_code == 200
        assert review.json()["conversations"][0]["coverage"]["fullyGraded"] is False
        assert review.json()["conversations"][0]["coverage"]["missing"] == 2
        detail = await client.get(f"/api/data/conversations/{row['id']}")
        assert detail.status_code == 200
        assert len(detail.json()["conversation"]["messages"]) == 4


@pytest.mark.asyncio
async def test_rubric_and_grade_canaries_absent_from_storage_and_detail(tmp_path):
    path = tmp_path / "private.db"
    store = SQLiteStorage(str(path))
    row = validate_conversation({
        "id": "c" * 32, "tenant_id": "alpha", "source_scope": "b" * 16,
        "messages": [{"role": "user", "content": "Email doctor@example.org for help."},
                     {"role": "assistant", "content": "Use the help desk."}],
        "event_at": "2026-09-01T12:00:00Z", "end_status": "complete", "input_issues": [],
    })
    rubric = validate_rubric({
        "name": "quality", "version": "1", "target": "conversation",
        "dimensions": [{"name": "helpful", "description": "Contact doctor@example.org."}],
    })
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rubric["fingerprint"],
                "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    grade = validate_assessment({
        "tenant_id": "alpha", "conversation_id": row["id"], "revision": row["revision"],
        "target_position": None, "rubric": rubric, "evaluator": identity,
        "status": "completed", "dimensions": {"helpful": {"state": "pass",
        "reason": "Contact doctor@example.org."}}, "findings": [],
        "evaluated_at": "2026-09-01T12:01:00Z",
    }, row)
    store.save_conversation(row)
    store.save_conversation_assessment(grade)
    store.close()
    assert b"doctor@example.org" not in path.read_bytes()
    app = create_app(storage=f"sqlite:///{path}", tenant_id="alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        detail = await client.get(f"/api/data/conversations/{row['id']}?evaluator={grade['evaluator_fingerprint']}")
        assert detail.status_code == 200
        assert "doctor@example.org" not in detail.text
        assert detail.json()["assessments"][0]["status"] == "completed"
        other = create_app(storage=f"sqlite:///{path}", tenant_id="beta")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other), base_url="http://127.0.0.1") as beta:
            hidden = await beta.get(f"/api/data/conversations/{row['id']}")
            assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_review_and_preview_page_after_5000_conversations(tmp_path):
    path = tmp_path / "long_lived.db"
    store = SQLiteStorage(str(path))
    for index in range(5001):
        store.save_conversation(validate_conversation({
            "id": f"{index:032x}", "tenant_id": "alpha", "source_scope": "b" * 16,
            "messages": [{"role": "user", "content": "Question."},
                         {"role": "assistant", "content": "Answer."}],
            "event_at": "2026-09-01T12:00:00Z", "end_status": "complete", "input_issues": [],
        }))
    store.close()
    config = {"unit": "conversation", "provider": "openai", "model": "synthetic-model",
              "after": f"{4999:032x}", "scanLimit": 1,
              "rubric": {"name": "quality", "version": "1", "target": "conversation",
                         "dimensions": [{"name": "helpful", "description": "Addresses the request."}]}}
    app = create_app(storage=f"sqlite:///{path}", tenant_id="alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        token = (await client.get("/api/setup/token")).json()["setupToken"]
        headers = {"X-Verdict-Setup": token}
        preview = await client.post("/api/evaluators/preview", json=config, headers=headers)
        review = await client.post("/api/data/conversations/review", json=config, headers=headers)
        assert preview.status_code == review.status_code == 200
        assert preview.json()["plannedCalls"] == 1
        assert [row["id"] for row in review.json()["conversations"]] == [f"{5000:032x}"]
