from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_judge_alignment.py"
SPEC = importlib.util.spec_from_file_location("verify_judge_alignment", SCRIPT)
assert SPEC is not None
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verifier)


def _mtbench_row() -> dict:
    return {
        "conversation_a": [
            {"role": "user", "content": "Turn 1 question"},
            {"role": "assistant", "content": "Assistant A turn 1 answer"},
            {"role": "user", "content": "Turn 2 follow-up"},
            {"role": "assistant", "content": "Assistant A turn 2 answer"},
        ],
        "conversation_b": [
            {"role": "user", "content": "Turn 1 question"},
            {"role": "assistant", "content": "Assistant B turn 1 answer"},
            {"role": "user", "content": "Turn 2 follow-up"},
            {"role": "assistant", "content": "Assistant B turn 2 answer"},
        ],
    }


def test_full_context_pair_preserves_all_mtbench_turns() -> None:
    query, response_a, response_b = verifier._build_full_context_pair(_mtbench_row())

    assert "Turn 1 question" in query
    assert "Turn 2 follow-up" in query
    assert "Assistant A turn 1 answer" in response_a
    assert "Assistant A turn 2 answer" in response_a
    assert "Assistant B turn 1 answer" in response_b
    assert "Assistant B turn 2 answer" in response_b


def test_legacy_pair_reproduces_first_user_final_answer_extraction() -> None:
    query, response_a, response_b = verifier._build_legacy_pair(_mtbench_row())

    assert query == "Turn 1 question"
    assert response_a == "Assistant A turn 2 answer"
    assert response_b == "Assistant B turn 2 answer"
