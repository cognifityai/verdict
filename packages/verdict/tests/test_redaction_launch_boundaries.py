"""Synthetic redaction canaries at structured and persisted agent boundaries."""

from __future__ import annotations

import json

import pytest
import verdict
from fastapi.testclient import TestClient
from verdict.dashboard.app import create_app
from verdict.redaction import redact, redact_structure
from verdict.storage.sqlite import SQLiteStorage

CARD = "4111111111111111"
PEM_BODY = "ZmFrZS1wcml2YXRlLWtleQ=="
PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    f"{PEM_BODY}\n"
    "-----END PRIVATE KEY-----"
)
URL_PASSWORD = "fakeP%40ss"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def reset_sdk() -> None:
    verdict.shutdown()
    yield
    verdict.shutdown()


def test_json_text_redacts_numeric_card_without_reformatting_safe_json() -> None:
    raw = '{ "note": "café", "amount": 1.25, "cards": [4111111111111111] }'
    safe = '{ "note": "café", "amount": 1.25 }'
    sanitized = redact_structure(raw)

    assert CARD not in sanitized
    assert json.loads(sanitized)["cards"] == ["<CREDIT_CARD>"]
    assert redact_structure(sanitized) == sanitized
    assert redact_structure(safe) == safe


def test_literal_unicode_escape_in_prose_preserves_the_sentence() -> None:
    raw = r"Why does the page show \u2019?"
    assert redact_structure(raw) == raw


@pytest.mark.parametrize("label", ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY",
                                   "OPENSSH PRIVATE KEY", "ENCRYPTED PRIVATE KEY"])
@pytest.mark.parametrize("separator", ["\n", r"\n"])
def test_private_key_blocks_are_removed_in_plain_and_escaped_text(
    label: str, separator: str
) -> None:
    block = separator.join((f"-----BEGIN {label}-----", PEM_BODY,
                            f"-----END {label}-----"))
    sanitized = redact(f"output: {block} done")
    assert PEM_BODY not in sanitized
    assert f"BEGIN {label}" not in sanitized
    assert redact(sanitized) == sanitized


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "https", "custom+db"])
@pytest.mark.parametrize("password", ["fakeP%40ss", "fakePassword"])
def test_credential_urls_remove_the_password_for_multiple_schemes(
    scheme: str, password: str
) -> None:
    sanitized = redact(f"connect {scheme}://reader:{password}@db.example.invalid/path")
    assert password not in sanitized
    assert "reader:" not in sanitized
    assert redact(sanitized) == sanitized


def test_non_sensitive_identifier_with_provider_prefix_fragment_survives() -> None:
    assert redact("adapter_AKIA_SHORT and hf_hub_download") == "adapter_AKIA_SHORT and hf_hub_download"


@pytest.mark.parametrize("prefix", ["", "key_"])
def test_fixed_width_provider_key_before_identifier_suffix_is_removed(prefix: str) -> None:
    sanitized = redact(f"{prefix}{AWS_KEY}_suffix")
    assert AWS_KEY not in sanitized
    assert "suffix" in sanitized
    assert redact(sanitized) == sanitized


@pytest.mark.parametrize(
    ("raw", "canary"),
    [
        (f"card {CARD}https://example.invalid/path", CARD),
        (PRIVATE_KEY, PEM_BODY),
        (f"postgres://admin:{URL_PASSWORD}@db.example.invalid/test", "fakeP"),
        (f"key_{AWS_KEY}", AWS_KEY),
    ],
)
def test_plain_text_sensitive_values_are_removed_and_remain_stable(
    raw: str, canary: str
) -> None:
    sanitized = redact(raw)
    assert canary not in sanitized
    assert redact(sanitized) == sanitized


@pytest.mark.parametrize("left", [
    "4111 1111 1111 1111", "555-123-4567", "2001:db8::1",
])
@pytest.mark.parametrize("separator", ["", " ", ","])
def test_numeric_sensitive_value_glued_to_url_is_stable_and_removed(
    left: str, separator: str,
) -> None:
    right = "https://example.invalid/path"
    sanitized = redact(f"note {left}{separator}{right} end")
    assert left not in sanitized
    assert right not in sanitized
    assert redact(sanitized) == sanitized


def test_agent_tool_output_canaries_absent_from_sqlite_and_run_api(tmp_path) -> None:
    path = tmp_path / "redaction.db"
    storage = SQLiteStorage(str(path))
    verdict.init(storage=storage, tenant_id="__verdict_local__", instrumentors=[])
    output = json.dumps({
        "cards": [int(CARD)],
        "adjacent": f"{CARD}https://example.invalid/path",
        "payload": PRIVATE_KEY,
        "database_url": f"postgres://admin:{URL_PASSWORD}@db.example.invalid/test",
        "provider_key": f"key_{AWS_KEY}_suffix",
    })
    with verdict.agent_run(name="synthetic-agent", session_id="synthetic-session") as run:
        with run.turn(user_input="Inspect synthetic record") as turn:
            with turn.tool("inspect_record") as tool:
                tool.set_output(output)
    [bundle] = storage.list_agent_run_bundles("__verdict_local__")
    with TestClient(create_app(storage=str(path))) as client:
        response = client.get(f"/api/runs/{bundle.run.run_id}")
    assert response.status_code == 200
    for canary in (CARD, PEM_BODY, "fakeP", AWS_KEY):
        assert canary not in repr(bundle)
        assert canary not in response.text
        assert canary.encode() not in path.read_bytes()
    storage.close()
