"""Smoke tests for the chat export parsers.

We don't have real exports checked in (privacy + size). These tests use
synthetic fixtures shaped exactly like the real exports — enough to catch
parser regressions without needing customer data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from verdict_inspect.parsers import (
    detect_format,
    parse_chatgpt_export,
    parse_claude_ai_export,
    parse_cowork_jsonl,
    parse_openai_jsonl,
)

# --------------------------------------------------------------------------- #
# ChatGPT export
# --------------------------------------------------------------------------- #

def _chatgpt_fixture(n_turns: int = 3) -> list[dict]:
    """Build a ChatGPT export-style structure with a linear conversation."""
    mapping: dict[str, dict] = {}
    prev = None
    leaf = None
    for i in range(n_turns):
        # user
        u_id = f"u-{i}"
        mapping[u_id] = {
            "id": u_id,
            "message": {
                "id": u_id,
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [f"user message {i}"]},
                "create_time": 1700000000.0 + i * 2,
            },
            "parent": prev,
            "children": [],
        }
        if prev:
            mapping[prev]["children"].append(u_id)
        # assistant
        a_id = f"a-{i}"
        mapping[a_id] = {
            "id": a_id,
            "message": {
                "id": a_id,
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [f"assistant reply {i} with several words"]},
                "create_time": 1700000000.0 + i * 2 + 1,
                "metadata": {"model_slug": "gpt-4-turbo"},
            },
            "parent": u_id,
            "children": [],
        }
        mapping[u_id]["children"].append(a_id)
        leaf = a_id
        prev = a_id

    return [{
        "title": "Test conversation",
        "id": "conv-1",
        "create_time": 1700000000.0,
        "mapping": mapping,
        "current_node": leaf,
        "default_model_slug": "gpt-4-turbo",
    }]


def test_chatgpt_basic(tmp_path: Path) -> None:
    data = _chatgpt_fixture(n_turns=3)
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps(data))
    convs = parse_chatgpt_export(f)
    assert len(convs) == 1
    assert convs[0].n_turns == 3
    assert "assistant reply 0" in convs[0].turns[0].assistant_text
    assert convs[0].turns[0].user_text == "user message 0"
    assert convs[0].model == "gpt-4-turbo"


def test_chatgpt_multimodal_content(tmp_path: Path) -> None:
    """Multimodal content blocks (text + image) should extract the text part."""
    data = _chatgpt_fixture(n_turns=1)
    # Mutate the assistant content to be multimodal
    mapping = data[0]["mapping"]
    a_id = next(k for k in mapping if k.startswith("a-"))
    mapping[a_id]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": [
            {"type": "text", "text": "Here's the answer with several words for substantive turn."},
            {"type": "image_asset_pointer", "asset_pointer": "file-abc"},
        ],
    }
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps(data))
    convs = parse_chatgpt_export(f)
    assert convs[0].turns[0].assistant_text.startswith("Here's the answer")


# --------------------------------------------------------------------------- #
# Claude.ai export
# --------------------------------------------------------------------------- #

def test_claude_ai_basic(tmp_path: Path) -> None:
    data = [{
        "uuid": "abc",
        "name": "Test",
        "created_at": "2024-01-15T22:30:00.000000+00:00",
        "model": "claude-3-5-sonnet-20241022",
        "chat_messages": [
            {"sender": "human", "text": "Hi there",
             "created_at": "2024-01-15T22:30:01+00:00"},
            {"sender": "assistant", "text": "Hello, how can I help you today with several words",
             "created_at": "2024-01-15T22:30:02+00:00"},
            {"sender": "human", "text": "Tell me about drift detection",
             "created_at": "2024-01-15T22:30:10+00:00"},
            {"sender": "assistant",
             "content": [{"type": "text", "text": "Drift detection uses statistical tests over many turns"}],
             "created_at": "2024-01-15T22:30:11+00:00"},
        ],
    }]
    f = tmp_path / "claude_export.json"
    f.write_text(json.dumps(data))
    convs = parse_claude_ai_export(f)
    assert len(convs) == 1
    assert convs[0].n_turns == 2
    assert "Drift detection" in convs[0].turns[1].assistant_text
    assert convs[0].turns[0].user_text == "Hi there"
    assert convs[0].model == "claude-3-5-sonnet-20241022"


# --------------------------------------------------------------------------- #
# OpenAI-style JSONL
# --------------------------------------------------------------------------- #

def test_openai_jsonl_shape_a(tmp_path: Path) -> None:
    """One conversation per line, with `messages` array."""
    f = tmp_path / "chat.jsonl"
    with f.open("w") as fh:
        fh.write(json.dumps({
            "conversation_id": "c-1",
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "user", "content": "What's drift?"},
                {"role": "assistant", "content": "Drift is when responses change distribution over time"},
            ],
        }) + "\n")
        fh.write(json.dumps({
            "conversation_id": "c-2",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Multi-part user msg"}]},
                {"role": "assistant", "content": [{"type": "output_text", "text": "Multi-part assistant reply with words"}]},
            ],
        }) + "\n")
    convs = parse_openai_jsonl(f)
    assert len(convs) == 2
    assert convs[0].turns[0].assistant_text.startswith("Drift is")
    assert "Multi-part assistant reply" in convs[1].turns[0].assistant_text


def test_openai_jsonl_shape_b(tmp_path: Path) -> None:
    """One message per line, grouped by conversation_id."""
    f = tmp_path / "chat.jsonl"
    with f.open("w") as fh:
        fh.write(json.dumps({"conversation_id": "X", "role": "user",
                             "content": "hello", "timestamp": "2024-01-01T00:00:00Z"}) + "\n")
        fh.write(json.dumps({"conversation_id": "X", "role": "assistant",
                             "content": "hi there with several substantive words in the response",
                             "timestamp": "2024-01-01T00:00:01Z"}) + "\n")
    convs = parse_openai_jsonl(f)
    assert len(convs) == 1
    assert convs[0].n_turns == 1
    assert "several substantive words" in convs[0].turns[0].assistant_text


# --------------------------------------------------------------------------- #
# Agent-session JSONL
# --------------------------------------------------------------------------- #

def test_cowork_jsonl(tmp_path: Path) -> None:
    """Agent sessions have type-tagged JSONL with intermixed tool calls."""
    f = tmp_path / "session.jsonl"
    records = [
        {"type": "ai-title", "aiTitle": "Test session"},
        {"type": "user", "message": {"role": "user", "content": "hello there"},
         "timestamp": "2026-05-10T18:00:00Z"},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-7",
                     "content": [
                         {"type": "thinking", "thinking": "internal..."},
                         {"type": "text", "text": "Hi! Here's a substantive response with words."},
                     ]},
         "timestamp": "2026-05-10T18:00:01Z"},
        # Tool result should NOT count as a user turn
        {"type": "user",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
         "timestamp": "2026-05-10T18:00:02Z"},
        # Real follow-up user message
        {"type": "user", "message": {"role": "user", "content": "more please"},
         "timestamp": "2026-05-10T18:00:03Z"},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text",
                                  "text": "Of course, here are additional substantive words and content."}]},
         "timestamp": "2026-05-10T18:00:04Z"},
    ]
    with f.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    convs = parse_cowork_jsonl(f)
    assert len(convs) == 1
    assert convs[0].n_turns == 2
    assert convs[0].title == "Test session"
    assert convs[0].turns[0].user_text == "hello there"
    assert "more please" in convs[0].turns[1].user_text


# --------------------------------------------------------------------------- #
# Auto-detect
# --------------------------------------------------------------------------- #

def test_detect_chatgpt(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text(json.dumps(_chatgpt_fixture(n_turns=1)))
    assert detect_format(f) == "chatgpt"


def test_detect_claude_ai(tmp_path: Path) -> None:
    f = tmp_path / "x.json"
    f.write_text(json.dumps([{
        "uuid": "1", "name": "T", "model": "claude-3-5-sonnet",
        "chat_messages": []
    }]))
    assert detect_format(f) == "claude_ai"


def test_detect_openai_jsonl(tmp_path: Path) -> None:
    f = tmp_path / "x.jsonl"
    f.write_text(json.dumps({
        "messages": [{"role": "user", "content": "hi"}]
    }) + "\n")
    assert detect_format(f) == "openai_jsonl"


def test_detect_cowork(tmp_path: Path) -> None:
    f = tmp_path / "x.jsonl"
    f.write_text(json.dumps({
        "type": "user",
        "sessionId": "s1",
        "message": {"role": "user", "content": "hi"},
    }) + "\n")
    assert detect_format(f) == "cowork"


def test_detect_unknown_format(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("just plain text, not JSON")
    with pytest.raises(ValueError):
        detect_format(f)
