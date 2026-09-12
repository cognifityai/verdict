import asyncio
import json

import httpx
from verdict.dashboard.app import create_app
from verdict.dashboard.inspect_lab import _LOCK, MAX_INSPECT_BYTES


def _export() -> bytes:
    return (json.dumps({
        "conversation_id": "one-off",
        "messages": [
            {"role": "user", "content": "SECRET_CANARY question"},
            {
                "role": "assistant",
                "content": "This is a substantive answer with enough words for structural analysis today.",
            },
        ],
    }) + "\n").encode()


def test_inspect_api_is_gated_bounded_and_does_not_persist_source(tmp_path) -> None:
    database = tmp_path / "verdict.db"

    async def scenario():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{database}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            denied = await client.post(
                "/api/evaluators/inspect?format=openai_jsonl", content=_export()
            )
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token, "Content-Type": "text/plain"}
            accepted = await client.post(
                "/api/evaluators/inspect?format=openai_jsonl",
                headers=headers,
                content=_export(),
            )
            malformed = await client.post(
                "/api/evaluators/inspect?format=auto",
                headers=headers,
                content=b"not-json",
            )
            non_utf8 = await client.post(
                "/api/evaluators/inspect?format=auto",
                headers=headers,
                content=b"\xff",
            )
            oversized = await client.post(
                "/api/evaluators/inspect?format=auto",
                headers=headers,
                content=b"x" * (MAX_INSPECT_BYTES + 1),
            )
            unconfirmed = await client.post(
                "/api/evaluators/inspect?format=openai_jsonl&judge=1",
                headers=headers,
                content=_export(),
            )
            invalid_flag = await client.post(
                "/api/evaluators/inspect?format=openai_jsonl&semantic=yes",
                headers=headers,
                content=_export(),
            )
            return (
                denied,
                accepted,
                malformed,
                non_utf8,
                oversized,
                unconfirmed,
                invalid_flag,
            )

    (
        denied,
        accepted,
        malformed,
        non_utf8,
        oversized,
        unconfirmed,
        invalid_flag,
    ) = asyncio.run(scenario())

    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["schema"] == "verdict-inspect-dashboard-v1"
    assert accepted.json()["report"]["n_conversations"] == 1
    assert accepted.json()["report"]["n_turns_total"] == 1
    assert "SECRET_CANARY" not in accepted.text
    assert malformed.status_code == non_utf8.status_code == 400
    assert oversized.status_code == 413
    assert unconfirmed.status_code == 400
    assert "Confirm external judge egress" in unconfirmed.text
    assert invalid_flag.status_code == 400
    assert "semantic must be 0 or 1" in invalid_flag.text
    assert not database.exists()


def test_inspect_api_rejects_a_concurrent_analysis(tmp_path) -> None:
    async def scenario():
        transport = httpx.ASGITransport(
            app=create_app(storage=f"sqlite:///{tmp_path / 'verdict.db'}")
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            return await client.post(
                "/api/evaluators/inspect?format=openai_jsonl",
                headers={"X-Verdict-Setup": token},
                content=_export(),
            )

    assert _LOCK.acquire(blocking=False)
    try:
        response = asyncio.run(scenario())
    finally:
        _LOCK.release()
    assert response.status_code == 409
