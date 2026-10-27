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
ANTHROPIC_KEY = "sk-ant-" + "A" * 24
GITHUB_KEY = "ghp_" + "B" * 24
GITHUB_FINE_GRAINED_KEY = "github_pat_" + "C" * 24


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


@pytest.mark.parametrize("prefix", ["555-123-4567", "4111111111111111", "10.0.0.1", "2001:db8::1",
                                     "fe80::1%en0"])
@pytest.mark.parametrize("credential", [ANTHROPIC_KEY, GITHUB_KEY,
                                         GITHUB_FINE_GRAINED_KEY])
def test_credential_glued_to_numeric_sensitive_value_is_redacted(
    prefix: str, credential: str,
) -> None:
    raw = f"observed {prefix}{credential} end"
    for mode, secret in (("redact", None), ("hash", "synthetic-test-secret")):
        sanitized = redact(raw, mode=mode, secret=secret)
        assert credential not in sanitized
        assert prefix not in sanitized
        assert redact(sanitized, mode=mode, secret=secret) == sanitized


def test_short_credential_prefix_fragments_and_letter_prefixed_identifiers_survive() -> None:
    text = "v1ghp_short component_ghp_short module_sk-ant-short"
    assert redact(text) == text


@pytest.mark.parametrize("following", ["555-123-4567", "10.0.0.1", "2001:db8::1"])
def test_fixed_width_provider_key_before_numeric_value_is_redacted(following: str) -> None:
    raw = f"observed {AWS_KEY}{following} end"
    sanitized = redact(raw)
    assert AWS_KEY not in sanitized
    assert following not in sanitized
    assert redact(sanitized) == sanitized


@pytest.mark.parametrize("credential", [ANTHROPIC_KEY, GITHUB_KEY,
                                         GITHUB_FINE_GRAINED_KEY])
@pytest.mark.parametrize("following", ["192.168.1.42", "555-123-4567", "2001:db8::1"])
def test_variable_width_credential_before_numeric_value_is_fully_redacted(
    credential: str, following: str,
) -> None:
    raw = f"observed {credential}{following} end"
    for mode, secret in (("redact", None), ("hash", "synthetic-test-secret")):
        sanitized = redact(raw, mode=mode, secret=secret)
        assert credential not in sanitized
        assert following not in sanitized
        for fragment in (".168.1.42", "-123-4567", ":db8::1"):
            assert fragment not in sanitized
        assert redact(sanitized, mode=mode, secret=secret) == sanitized


def test_credential_scan_stops_at_whitespace_and_keeps_ordinary_following_text() -> None:
    for tail in (" 200 OK", " 2026-10-27 note", " 2026 1027 0000 UTC", ". End"):
        assert redact(GITHUB_KEY + tail) == "<GITHUB_TOKEN>" + tail


@pytest.mark.parametrize("card,fragment", [
    ("4111 1111 1111 1111", " 1111 1111 1111"),
    ("41111111 11111111", " 11111111"),
    ("3782 822463 10005", " 822463 10005"),
])
def test_digit_ending_variable_key_does_not_leave_grouped_card_fragment(
    card: str, fragment: str,
) -> None:
    key = "ghp_" + "B" * 20 + "4111"
    sanitized = redact(key + card)
    assert fragment not in sanitized
    assert redact(sanitized) == sanitized


def test_fixed_width_key_preserves_separate_numeric_status_and_timestamp() -> None:
    key = "AKIA" + "A" * 12 + "4111"
    for tail in (" 2026 1027 0000 UTC", " 123 4567 rows"):
        raw = key + tail
        assert redact(raw) == "<PROVIDER_KEY>" + tail
        assert redact(redact(raw)) == redact(raw)


@pytest.mark.parametrize("card", ["41111111 11111111", "4111 1111 1111 1111"])
def test_complete_card_before_numeric_status_remains_redacted(card: str) -> None:
    key = "AKIA" + "A" * 12 + "4111"
    for raw in (f"{card} 200 OK", f"{key}{card} 200 OK"):
        sanitized = redact(raw)
        assert card not in sanitized
        assert "200 OK" in sanitized
        assert redact(sanitized) == sanitized


def test_long_scoped_ipv6_glued_to_credential_does_not_leave_scope_fragment() -> None:
    raw = GITHUB_KEY + "fe80::1%" + "q" * 300
    assert redact(raw) == "<GITHUB_TOKEN>"
    assert redact(raw, mode="hash", secret="synthetic-test-secret") == redact(
        redact(raw, mode="hash", secret="synthetic-test-secret"),
        mode="hash", secret="synthetic-test-secret",
    )


@pytest.mark.parametrize("following,fragment", [
    ("555 123 4567", " 123 4567"),
    ("555-123 4567", " 4567"),
    ("555 1234567", " 1234567"),
    ("4111 1111 1111 1111", " 1111 1111 1111"),
    ("4111-1111 1111 1111", " 1111 1111"),
    ("41111111 11111111", " 11111111"),
    ("1043 3218 19608 016", " 016"),
    ("1043321819608 123455", " 123455"),
    ("3782 822463 10005", " 822463 10005"),
    ("3782-822463 10005", " 10005"),
])
def test_credential_before_grouped_numeric_value_keeps_no_sensitive_fragment(
    following: str, fragment: str,
) -> None:
    raw = GITHUB_KEY + following
    for mode, secret in (("redact", None), ("hash", "synthetic-test-secret")):
        sanitized = redact(raw, mode=mode, secret=secret)
        assert GITHUB_KEY not in sanitized
        assert fragment not in sanitized
        assert redact(sanitized, mode=mode, secret=secret) == sanitized


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
        "glued_provider": f"555-123-4567{ANTHROPIC_KEY}",
        "glued_github": f"10.0.0.1{GITHUB_KEY}",
        "reverse_glued_github": f"{GITHUB_KEY}192.168.1.42",
        "reverse_grouped_card": f"{GITHUB_KEY}3782 822463 10005",
        "reverse_phone_two_groups": f"{GITHUB_KEY}555 1234567",
        "reverse_card_two_groups": f"{GITHUB_KEY}41111111 11111111",
        "card_with_status": "41111111 11111111 200 OK",
        "fixed_key_card_with_status": f"{'AKIA' + 'A' * 12 + '4111'}41111111 11111111 200 OK",
        "ordinary_status": f"{GITHUB_KEY} 200 OK",
        "ordinary_timestamp": f"{'AKIA' + 'A' * 12 + '4111'} 2026 1027 0000 UTC",
    })
    with verdict.agent_run(name="synthetic-agent", session_id="synthetic-session") as run:
        with run.turn(user_input="Inspect synthetic record") as turn:
            with turn.tool("inspect_record") as tool:
                tool.set_output(output)
    [bundle] = storage.list_agent_run_bundles("__verdict_local__")
    with TestClient(create_app(storage=str(path))) as client:
        response = client.get(f"/api/runs/{bundle.run.run_id}")
    assert response.status_code == 200
    for canary in (CARD, PEM_BODY, "fakeP", AWS_KEY, ANTHROPIC_KEY, GITHUB_KEY):
        assert canary not in repr(bundle)
        assert canary not in response.text
        assert canary.encode() not in path.read_bytes()
    assert ".168.1.42" not in repr(bundle)
    assert ".168.1.42" not in response.text
    assert b".168.1.42" not in path.read_bytes()
    assert "822463 10005" not in repr(bundle)
    assert "822463 10005" not in response.text
    assert b"822463 10005" not in path.read_bytes()
    for fragment in (" 1234567", " 11111111"):
        assert fragment not in repr(bundle)
        assert fragment not in response.text
        assert fragment.encode() not in path.read_bytes()
    assert "200 OK" in response.text
    assert "2026 1027 0000 UTC" in response.text
    storage.close()
