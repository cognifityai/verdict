"""Dashboard entry points for rubric preview and current conversation review."""


import httpx
import pytest
from verdict.conversation_assessments import validate_assessment, validate_rubric
from verdict.conversations import validate_conversation
from verdict.dashboard.app import create_app
from verdict.storage.sqlite import SQLiteStorage


@pytest.mark.asyncio
async def test_custom_anthropic_endpoint_is_disclosed_without_its_url(monkeypatch, tmp_path):
    endpoint = "https://synthetic-judge.invalid/v1"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    app = create_app(storage=f"sqlite:///{tmp_path / 'env.db'}", tenant_id="alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.get("/api/evaluators")
    assert response.status_code == 200
    anthropic = next(row for row in response.json()["providers"] if row["provider"] == "anthropic")
    assert anthropic["configured"] is True
    assert anthropic["customEndpointConfigured"] is True
    assert endpoint not in response.text and "synthetic-key" not in response.text


@pytest.mark.asyncio
async def test_oversized_numeric_rubric_values_return_validation_error(tmp_path):
    app = create_app(storage=f"sqlite:///{tmp_path / 'rubric.db'}", tenant_id="alpha")
    huge = int("9" * 400)
    document = {"name": "quality", "version": "1", "target": "conversation",
                "dimensions": [{"name": "score", "description": "Synthetic range.",
                                "type": "number", "min": 0, "max": 5,
                                "passThreshold": 3}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://127.0.0.1") as client:
        token = (await client.get("/api/setup/token")).json()["setupToken"]
        headers = {"X-Verdict-Setup": token}
        for field in ("min", "max", "passThreshold"):
            candidate = {**document, "dimensions": [{**document["dimensions"][0], field: huge}]}
            response = await client.post("/api/evaluators/rubric/validate",
                                         json={"document": candidate}, headers=headers)
            assert response.status_code == 400, field
            assert response.json() == {"error": "invalid executable rubric JSON"}
        valid = await client.post("/api/evaluators/rubric/validate",
                                  json={"document": document}, headers=headers)
    assert valid.status_code == 200


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
async def test_detail_never_pairs_old_snapshot_with_corrected_grade(tmp_path, monkeypatch):
    path = tmp_path / "corrected.db"
    original = validate_conversation({
        "id": "d" * 32, "tenant_id": "alpha", "source_scope": "b" * 16,
        "messages": [{"role": "user", "content": "Question."},
                     {"role": "assistant", "content": "Old answer."}],
        "event_at": "2026-09-01T12:00:00Z", "end_status": "complete", "input_issues": [],
    })
    corrected_input = {key: value for key, value in original.items() if key != "revision"}
    corrected = validate_conversation({
        **corrected_input, "messages": [{"role": "user", "content": "Question."},
                                         {"role": "assistant", "content": "New answer."}],
    })
    rubric = validate_rubric({
        "name": "quality", "version": "1", "target": "conversation",
        "dimensions": [{"name": "helpful", "description": "Addresses the request."}],
    })
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rubric["fingerprint"],
                "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    grade = validate_assessment({
        "tenant_id": "alpha", "conversation_id": corrected["id"], "revision": corrected["revision"],
        "target_position": None, "rubric": rubric, "evaluator": identity,
        "status": "completed", "dimensions": {"helpful": {"state": "pass", "reason": "New answer."}},
        "findings": [], "evaluated_at": "2026-09-01T12:01:00Z",
    }, corrected)
    writer = SQLiteStorage(str(path))
    writer.save_conversation(original)
    writer.close()
    read_snapshot = SQLiteStorage.get_conversation

    def interleave_correction(storage, tenant_id, conversation_id):
        old = read_snapshot(storage, tenant_id, conversation_id)
        writer = SQLiteStorage(str(path))
        writer.save_conversation(corrected)
        writer.save_conversation_assessment(grade)
        writer.close()
        return old

    monkeypatch.setattr(SQLiteStorage, "get_conversation", interleave_correction)
    app = create_app(storage=f"sqlite:///{path}", tenant_id="alpha")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        detail = await client.get(f"/api/data/conversations/{original['id']}?evaluator={grade['evaluator_fingerprint']}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["conversation"]["revision"] == original["revision"]
        assert all(a["revision"] == payload["conversation"]["revision"] for a in payload["assessments"])


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


@pytest.mark.asyncio
async def test_monitor_previews_current_conversation_grades_without_activation(tmp_path):
    path = tmp_path / "comparison.db"
    store = SQLiteStorage(str(path))
    rubric = validate_rubric({
        "name": "quality", "version": "1", "target": "conversation",
        "dimensions": [{"name": "helpful", "description": "Addresses the request."}],
    })
    identity = {"provider": "local", "model": "synthetic", "rubric_fingerprint": rubric["fingerprint"],
                "prompt_version": "conversation_v1", "max_output_tokens": 2048}
    grades = []
    for index, (event_at, state) in enumerate([
        ("2026-09-01T12:00:00Z", "pass"), ("2026-09-03T12:00:00Z", "fail"),
    ]):
        row = validate_conversation({
            "id": f"{index + 1:032x}", "tenant_id": "alpha", "source_scope": "b" * 16,
            "messages": [{"role": "user", "content": "Question."},
                         {"role": "assistant", "content": "Answer."}],
            "event_at": event_at, "end_status": "complete", "input_issues": [],
            "labels": {"group": "one"},
        })
        store.save_conversation(row)
        result = validate_assessment({
            "tenant_id": "alpha", "conversation_id": row["id"], "revision": row["revision"],
            "target_position": None, "rubric": rubric, "evaluator": identity,
            "status": "completed", "dimensions": {"helpful": {"state": state,
            "reason": "Synthetic judgment."}}, "findings": [],
            "evaluated_at": "2026-09-05T00:00:00Z",
        }, row)
        store.save_conversation_assessment(result)
        grades.append(result)
    store.close()
    app = create_app(storage=f"sqlite:///{path}", tenant_id="alpha")
    payload = {"analysisUnit": "conversation", "referenceStart": "2026-09-01T00:00:00Z",
               "referenceEnd": "2026-09-03T00:00:00Z", "currentStart": "2026-09-03T00:00:00Z",
               "currentEnd": "2026-09-05T00:00:00Z",
               "evaluatorFingerprint": grades[0]["evaluator_fingerprint"],
               "dimension": "helpful", "labelKey": "group"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        denied = await client.post("/api/monitor/preview", json=payload)
        assert denied.status_code == 403
        token = (await client.get("/api/setup/token")).json()["setupToken"]
        response = await client.post("/api/monitor/preview", json=payload,
                                     headers={"X-Verdict-Setup": token})
        assert response.status_code == 200
        body = response.json()
        assert body["unit"] == "conversation"
        assert body["reference"]["passRate"] == 1.0
        assert body["current"]["passRate"] == 0.0
        assert body["effect"] == -1.0
        assert body["groups"][0]["label"] == "one"
        assert "pValue" not in body and "alert" not in body
        payload["referenceStart"] = "0001-01-01T00:00:00+23:59"
        invalid = await client.post("/api/monitor/preview", json=payload,
                                    headers={"X-Verdict-Setup": token})
        assert invalid.status_code == 400
