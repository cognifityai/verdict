import json
import random
from copy import deepcopy

import verdict.redaction as redaction_module
from verdict.redaction import redact, redact_messages, redact_structure


def test_redact_messages_string_content():
    """raw_messages with string content must be redacted (PII must not leak into
    the stored structured payload even when content capture is on)."""
    out = redact_messages([{"role": "user", "content": "email jane@acme.com or call 415-555-0100"}])
    assert "jane@acme.com" not in out[0]["content"]
    assert "<EMAIL>" in out[0]["content"]
    assert "<PHONE>" in out[0]["content"]


def test_redact_messages_block_content_and_preserves_structure():
    msgs = [{"role": "assistant", "content": [
        {"type": "text", "text": "SSN 123-45-6789"},
        {"type": "image", "url": "keep-me"},
    ]}]
    out = redact_messages(msgs)
    assert "123-45-6789" not in out[0]["content"][0]["text"]
    assert "<SSN>" in out[0]["content"][0]["text"]
    # non-text blocks pass through untouched
    assert out[0]["content"][1] == {"type": "image", "url": "keep-me"}


def test_redact_messages_recurses_through_provider_structures():
    """P0-1: structured provider payloads are part of the content boundary.

    OpenAI tool arguments, Anthropic tool inputs/results, message metadata, and
    malformed mixed lists must not retain canary PII merely because it is not in
    a top-level ``content`` or ``text`` field.
    """
    canaries = [
        "nested@example.com",
        "123-45-6789",
        "4111 1111 1111 1111",
        "203.0.113.42",
        "https://example.com/private",
        "415-555-0199",
    ]
    messages = [{
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "name": "lookup_customer",
                "input": {
                    "email": canaries[0],
                    "identity": {"ssn": canaries[1], "card": canaries[2]},
                },
            },
            {
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": f"IP {canaries[3]}"},
                    {"mixed": [canaries[4], 7, None, {"phone": canaries[5]}]},
                ],
            },
        ],
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "notify",
                "arguments": json.dumps({"email": canaries[0], "phone": canaries[5]}),
            },
        }],
        "metadata": {"audit_note": f"contact {canaries[0]} from {canaries[3]}"},
    }]

    out = redact_messages(messages)
    serialized = json.dumps(out, sort_keys=True)

    for canary in canaries:
        assert canary not in serialized
    assert messages[0]["content"][0]["input"]["email"] == canaries[0]


def test_redact_messages_does_not_mutate_input():
    msgs = [{"role": "user", "content": "user@a.co"}]
    redact_messages(msgs)
    assert msgs[0]["content"] == "user@a.co"  # original untouched


def test_redact_messages_non_list_returns_none():
    assert redact_messages("not a list") is None
    assert redact_messages(None) is None


def test_redact_messages_drops_unknown_fields_and_fails_closed_on_malformed_values():
    class SecretObject:
        def __str__(self):
            return "object-secret@example.com"

    cyclic = {}
    cyclic["self"] = cyclic
    messages = [
        {"role": "user", "content": [SecretObject(), cyclic], "unknown": "drop@example.com"},
        "malformed@example.com",
    ]

    out = redact_messages(messages)
    serialized = json.dumps(out)

    assert "drop@example.com" not in serialized
    assert "object-secret@example.com" not in serialized
    assert "malformed@example.com" not in serialized
    # The cyclic container is multiply referenced and therefore cannot be
    # represented as a JSON tree without traversal-order dependence.
    assert out[0]["content"] == ["<REDACTED>", "<REDACTED>"]
    assert out[1] == {"role": "unknown", "content": "<REDACTED>"}


def test_recursive_hash_mode_uses_configured_secret():
    out = redact_structure(
        {"tool": {"arguments": ["hash-me@example.com"]}},
        mode="hash",
        secret="test-secret",
    )

    serialized = json.dumps(out)
    assert "hash-me@example.com" not in serialized
    assert "<EMAIL:" in serialized


def test_openai_json_tool_arguments_remain_valid_after_url_redaction():
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "type": "function",
            "function": {
                "name": "fetch",
                "arguments": json.dumps({"url": "https://example.com/private", "count": 2}),
            },
        }],
    }]

    out = redact_messages(messages)
    arguments = out[0]["tool_calls"][0]["function"]["arguments"]

    assert json.loads(arguments) == {"url": "<URL>", "count": 2}


def test_recursive_redaction_fuzzes_arbitrary_json_shapes_without_mutation():
    rng = random.Random(20260815)
    canaries = [
        "fuzz@example.com",
        "123-45-6789",
        "4111111111111111",
        "203.0.113.99",
        "https://example.com/fuzz",
        "415-555-0101",
    ]

    def generate(depth=0):
        if depth >= 5 or rng.random() < 0.35:
            return rng.choice([None, True, 17, 3.5, rng.choice(canaries), "safe"])
        if rng.random() < 0.5:
            return [generate(depth + 1) for _ in range(rng.randrange(4))]
        return {f"key-{index}": generate(depth + 1) for index in range(rng.randrange(4))}

    for _ in range(200):
        value = {"generated": generate(), "forced": rng.choice(canaries)}
        original = deepcopy(value)
        sanitized = redact_structure(value)
        serialized = json.dumps(sanitized, sort_keys=True)
        for canary in canaries:
            assert canary not in serialized
        assert value == original


def test_recursive_redaction_rejects_shared_dag_nodes_without_retraversal(monkeypatch):
    """Shared Python graphs fail closed instead of expanding at JSON storage."""
    shared = {"email": "shared@example.com"}
    for _ in range(18):
        shared = {"left": shared, "right": shared}

    redact_calls = 0
    real_redact = redaction_module.redact

    def counting_redact(*args, **kwargs):
        nonlocal redact_calls
        redact_calls += 1
        return real_redact(*args, **kwargs)

    monkeypatch.setattr(redaction_module, "redact", counting_redact)
    sanitized = redaction_module.redact_structure(shared)

    assert sanitized == {"left": "<REDACTED>", "right": "<REDACTED>"}
    assert redact_calls < 100


def test_ipv6_redaction_consumes_mapped_and_scoped_addresses_completely():
    cases = {
        "client ::ffff:203.0.113.42 connected": "client <IPV6> connected",
        "host fe80::1%eth0 down": "host <IPV6> down",
        "peer [2001:db8::1] ready": "peer [<IPV6>] ready",
    }

    for raw, expected in cases.items():
        assert redact(raw) == expected


def test_ipv6_redaction_preserves_surrounding_periods_without_leaking_the_address():
    cases = {
        "Connection from 2001:db8::1.": "Connection from <IPV6>.",
        "Peer ...2001:db8:85a3::8a2e:370:7334...": "Peer ...<IPV6>...",
        "Hosts 2001:db8::1, 2001:db8::2.": "Hosts <IPV6>, <IPV6>.",
        "Mapped ::ffff:203.0.113.42.": "Mapped <IPV6>.",
        "Scoped fe80::1%eth0.": "Scoped <IPV6>.",
        "Dotted scope fe80::1%eth0.1.": "Dotted scope <IPV6>.",
    }

    for raw, expected in cases.items():
        assert redact(raw) == expected


def test_ipv6_redaction_does_not_corrupt_namespaced_source_code():
    examples = [
        "Use std::vector<int> v; then call v::size()",
        "In Rust: use std::collections::HashMap;",
        "PHP uses Foo::bar() and Perl can use Package::symbol",
    ]

    for source in examples:
        assert redact(source) == source


def test_hash_mode_hashes_the_complete_mapped_or_scoped_ipv6_token():
    for address in ("::ffff:203.0.113.42", "fe80::1%eth0"):
        output = redact(f"address={address}", mode="hash", secret="test-secret")
        assert address not in output
        assert "<IPV6:" in output
        assert "%eth0" not in output
        assert ".0.113.42" not in output


def test_ipv6_hash_mode_preserves_sentence_punctuation_outside_the_hash():
    address = "::ffff:203.0.113.42"

    output = redact(f"address={address}.", mode="hash", secret="test-secret")
    bare = redact(f"address={address}", mode="hash", secret="test-secret")

    assert output is not None
    assert bare is not None
    assert address not in output
    assert output.startswith("address=<IPV6:")
    assert output.endswith(">.")
    assert output == f"{bare}."


def test_redacted_mapping_key_collisions_preserve_every_entry_deterministically():
    first = {"a@x.com": {"value": 1}, "b@y.com": {"value": 2}}
    second = {"b@y.com": {"value": 2}, "a@x.com": {"value": 1}}

    sanitized_first = redact_structure(first)
    sanitized_second = redact_structure(second)

    assert sanitized_first == sanitized_second
    assert len(sanitized_first) == 2
    assert sorted(item["value"] for item in sanitized_first.values()) == [1, 2]
    assert all("@" not in key for key in sanitized_first)


def test_shared_graph_output_is_small_deterministic_and_not_aliased():
    leaf = {"email": "shared@example.com"}
    shared = leaf
    for _ in range(22):
        shared = {"left": shared, "right": shared}

    first = redact_structure(shared)
    second = redact_structure(shared)
    encoded = json.dumps(first, sort_keys=True)

    assert first == second
    assert "shared@example.com" not in encoded
    assert len(encoded.encode("utf-8")) < 100_000
    if isinstance(first, dict) and isinstance(first.get("left"), dict):
        assert first["left"] is not first["right"]


def test_cycle_and_shared_node_redaction_is_independent_of_mapping_order():
    root = {}
    shared = {"back": root, "data": "safe"}
    root["shared"] = shared

    first = redact_structure({"root": root, "other": {"shared": shared}})
    second = redact_structure({"other": {"shared": shared}, "root": root})

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_sqlite_labeling_export_contains_only_sanitized_content(tmp_path):
    from verdict.schema import Trace
    from verdict.storage.sqlite import SQLiteStorage

    from scripts.sample_to_label import _write_rows, from_sqlite

    db_path = tmp_path / "export.db"
    out_path = tmp_path / "labels.jsonl"
    canary = "export-secret@example.com"
    storage = SQLiteStorage(str(db_path))
    storage.insert_trace(Trace(
        provider="custom",
        prompt_redacted=f"Prompt for {canary}",
        response_redacted=f"Response for {canary}",
    ))
    storage.close()

    rows, skipped = from_sqlite(str(db_path), 10, "recent")
    assert skipped == 0
    assert _write_rows(str(out_path), rows) == 1
    assert canary not in out_path.read_text()


def test_redact_email():
    out = redact("Email me at user@example.com please.")
    assert "<EMAIL>" in out
    assert "user@example.com" not in out


def test_redact_ssn():
    out = redact("My SSN is 123-45-6789.")
    assert "<SSN>" in out
    assert "123-45-6789" not in out


def test_redact_credit_card():
    # Luhn-valid PAN (test Visa 4111 1111 1111 1111) IS redacted. (The previous
    # fixture used 4012 1234 5678 9010, which is NOT Luhn-valid and was only
    # caught by the old over-eager regex.)
    out = redact("Card: 4111 1111 1111 1111 exp soon.")
    assert "<CREDIT_CARD>" in out
    assert "4111" not in out


def test_credit_card_non_luhn_not_redacted():
    """A 16-digit non-card run (e.g. an order id) must survive untouched — the
    old regex destroyed these irreversibly."""
    out = redact("Order id 1234567890123456 shipped.")
    assert "<CREDIT_CARD>" not in out
    assert "1234567890123456" in out


def test_credit_card_luhn_valid_15_digit_amex():
    # 15-digit Amex test number, Luhn-valid.
    out = redact("Amex 3782 822463 10005 on file.")
    assert "<CREDIT_CARD>" in out
    assert "3782" not in out


def test_credit_card_hash_mode_only_luhn():
    # Hash mode also gates on Luhn: valid card hashed, non-card left as-is.
    out = redact(
        "card 4111111111111111 vs id 1234567890123456",
        mode="hash",
        secret="topsecret",
    )
    assert "<CREDIT_CARD:" in out
    assert "4111111111111111" not in out
    assert "1234567890123456" in out


def test_redact_phone():
    out = redact("Call (415) 555-0199 anytime.")
    assert "<PHONE>" in out
    assert "555-0199" not in out


def test_redact_url():
    out = redact("See https://example.com/secret/path for details.")
    assert "<URL>" in out


def test_clock_time_is_not_misclassified_as_ipv6():
    assert redact("Meeting at 12:34:56 UTC") == "Meeting at 12:34:56 UTC"


def test_compressed_ipv6_is_redacted_after_address_validation():
    assert redact("Host 2001:db8::1 responded") == "Host <IPV6> responded"


def test_hash_mode():
    out = redact("My email is foo@example.com", mode="hash", secret="topsecret")
    assert "<EMAIL:" in out
    assert "foo@example.com" not in out
    # Same input → same hash with same secret
    out2 = redact("My email is foo@example.com", mode="hash", secret="topsecret")
    assert out == out2


def test_redact_none_passes_through():
    assert redact(None) is None


def test_redact_multiple_in_one_string():
    out = redact("Hi user@a.co and 415-555-0100, your SSN 123-45-6789 is unsafe.")
    assert "<EMAIL>" in out
    assert "<PHONE>" in out
    assert "<SSN>" in out


def test_redact_is_linear_on_pathological_text():
    """`redact` must not backtrack quadratically on attacker-controlled text.

    Both probes are free of `@` and `:`, so no pattern can legitimately match;
    every candidate start must therefore fail in constant time.
    """
    import time

    for probe in ("1." * 32000, "a" * 64000):
        start = time.perf_counter()
        redact(probe)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, (
            f"redact() took {elapsed:.1f}s on {len(probe)} chars; "
            "a candidate matcher is backtracking quadratically"
        )


def test_colon_free_text_never_enters_ipv6_candidate_search(monkeypatch):
    """A necessary delimiter must gate the permissive IPv6 candidate regex."""

    class UnexpectedIPv6Search:
        def sub(self, *_args, **_kwargs):
            raise AssertionError("IPv6 candidate search ran without a colon")

    monkeypatch.setitem(
        redaction_module._PATTERNS,
        "IPV6",
        UnexpectedIPv6Search(),
    )

    probe = "a" * 64_000
    assert redact(probe) == probe


def test_email_redaction_is_linear_when_malformed_text_contains_at_signs():
    """An at-sign must not re-enable the old quadratic candidate search."""
    import time

    for probe in ("a" * 64_000 + "@", "患" * 64_000 + "@"):
        start = time.perf_counter()
        assert redact(probe) == probe
        elapsed = time.perf_counter() - start
        assert elapsed < 0.1, (
            f"redact() took {elapsed:.3f}s on malformed {len(probe)}-char email"
        )


def test_email_scanner_redacts_long_and_adjacent_addresses_without_leaking_heads():
    long_address = f"{'x' * 512}@example.com"

    assert redact(long_address) == "<EMAIL>"
    adjacent = redact("a@b.co+c@d.co")
    assert adjacent == "<EMAIL><EMAIL>"
    assert "@" not in adjacent


def test_ipv6_redaction_boundaries_are_unchanged_by_the_candidate_bound():
    """Lock the redact/keep decision either side of the IPV6 candidate bound.

    Bounding the candidate length must not change which tokens reach
    `IPv6Address` validation. Every case here is a form whose handling is
    unambiguous; `dead::beef`-style hex words are deliberately excluded because
    their current over-redaction is a separate open defect, not behaviour worth
    enshrining.
    """
    expected = {
        # Real addresses — must be fully consumed, including mapped IPv4
        # tails, scope IDs and bracketed forms.
        "client ::ffff:203.0.113.42 ok": "client <IPV6> ok",
        "host fe80::1%eth0 down": "host <IPV6> down",
        "peer [2001:db8::1] ready": "peer [<IPV6>] ready",
        "v6 2001:db8::1 ok": "v6 <IPV6> ok",
        "loop ::1 here": "loop <IPV6> here",
        "full 1:2:3:4:5:6:7:8 ok": "full <IPV6> ok",
        # Colon-bearing non-addresses — must survive untouched.
        "time 12:34:56 ok": "time 12:34:56 ok",
        "Use std::vector<int> v": "Use std::vector<int> v",
        "mac 00:1A:2B:3C:4D:5E ok": "mac 00:1A:2B:3C:4D:5E ok",
        "ratio 3:4 ok": "ratio 3:4 ok",
        "time 12:34:56.": "time 12:34:56.",
        "Use std::vector<int>.": "Use std::vector<int>.",
        "mac 00:1A:2B:3C:4D:5E.": "mac 00:1A:2B:3C:4D:5E.",
        "ratio 3:4.": "ratio 3:4.",
        # IPv4 must still be handled by the IP pattern, not swallowed as a
        # partial IPv6 candidate.
        "ip 203.0.113.42 here": "ip <IP> here",
    }
    for text, want in expected.items():
        assert redact(text) == want, f"{text!r} redacted to {redact(text)!r}"


def test_ipv6_canary_is_absent_from_storage_and_the_dashboard_bundle():
    """An IPv6 canary must not survive into stored records or the API payload.

    The original leak (`::ffff:203.0.113.42` reduced to `<IPV6>.0.113.42`)
    persisted through SQLite and back out through `build_bundle`, because every
    redaction canary in the suite was an email address. This exercises the real
    cross-layer path the standard requires.
    """
    import tempfile
    from pathlib import Path

    from verdict.schema import Trace
    from verdict.storage.sqlite import SQLiteStorage

    from ui.server import build_bundle

    canary = "::ffff:203.0.113.42"
    # A partially consumed candidate leaves a SUFFIX behind, not the whole
    # address: the original defect rendered "<IPV6>.0.113.42". Asserting only
    # on the full address therefore passes while three octets leak, so every
    # progressively shorter tail is checked too.
    leaked_fragments = (canary, "203.0.113.42", ".0.113.42", "113.42", "0.113")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "verdict.db"
        storage = SQLiteStorage(str(path))
        trace = Trace(
            provider="openai",
            request_model="gpt-4o-mini",
            prompt_redacted=f"Prompt from {canary}.",
            response_redacted=f"Response to {canary}.",
            error=f"Error contacting {canary}.",
            tags={"peer": f"{canary}."},
        )
        storage.insert_trace(trace)
        stored = storage.get_trace(trace.trace_id)
        storage.close()

        # The address must be replaced whole, not merely reduced.
        assert stored.prompt_redacted == "Prompt from <IPV6>."
        assert stored.response_redacted == "Response to <IPV6>."
        assert stored.error == "Error contacting <IPV6>."
        assert stored.tags["peer"] == "<IPV6>."

        stored_repr = repr(stored)
        for fragment in leaked_fragments:
            assert fragment not in stored_repr, (
                f"IPv6 canary fragment {fragment!r} survived into storage"
            )

        bundle = build_bundle(path)
        serialized = json.dumps(bundle, sort_keys=True, default=str)
        for fragment in leaked_fragments:
            assert fragment not in serialized, (
                f"IPv6 canary fragment {fragment!r} reached the dashboard bundle"
            )
