"""Idempotence contract for redaction: ``redact(redact(x)) == redact(x)``.

Every storage adapter re-sanitizes the records it persists, and conversation
snapshots carry a digest of their redacted content, so a second scan that
changes text is a persistence defect rather than a cosmetic one. The
generators below are seeded, so a failing case prints a reproducible input.
"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from datetime import datetime, timezone

import pytest
import verdict.redaction as redaction_module
from verdict.conversations import validate_conversation
from verdict.evidence import (
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunBundle,
    AgentTurn,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
    SourceSession,
)
from verdict.redaction import (
    redact,
    redact_messages,
    redact_structure,
    sanitize_agent_run_bundle,
    sanitize_trace,
)
from verdict.schema import Trace

HASH_SECRET = "idempotence-test-secret"
MODES = (("redact", None), ("hash", HASH_SECRET))
NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)

# Digits of pi grouped by ten, the shape that made 0.1.0a23 peel one "phone
# number" off the right end of the run on every scan.
DIGIT_RUN = "1816334467 7522431712 1992458631 5030286182 9627117"

PLACEHOLDERS = (
    "<PHONE>",
    "<EMAIL>",
    "<SSN>",
    "<CREDIT_CARD>",
    "<IP>",
    "<IPV6>",
    "<URL>",
    "<SECRET>",
    "<PROVIDER_KEY>",
    "<GITHUB_TOKEN>",
    "<BEARER_TOKEN>",
    "<BASIC_AUTH>",
    "<REDACTED>",
    "<PHONE:0123456789ab>",
    "<EMAIL:abcdef012345>",
    "<SECRET:0000000000ff>",
)
ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _once(text: str, mode: str, secret: str | None) -> str:
    return redact(text, mode=mode, secret=secret)


def _changing_scans(text: str, mode: str, secret: str | None) -> int:
    """Count single scans that still change the text before it stabilizes."""
    current = text
    for count in range(8):
        following = redaction_module._redact_once(current, mode, secret)
        if following == current:
            return count
        current = following
    return 8


def _assert_fixed_point(text: str, mode: str, secret: str | None) -> str:
    once = _once(text, mode, secret)
    twice = _once(once, mode, secret)
    assert twice == once, f"mode={mode} input={text!r}\n once={once!r}\n twice={twice!r}"
    return once


# --------------------------------------------------------------------------
# Reported counterexamples (fail on 0.1.0a23).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_grouped_digit_run_is_a_fixed_point(mode: str, secret: str | None) -> None:
    once = _assert_fixed_point(DIGIT_RUN, mode, secret)
    # One scan of the original text decides. The placeholder it inserts does not
    # turn the digit group before it into another phone number.
    assert once.count("<") == 1
    assert once.startswith("1816334467 7522431712 1992458631 5030286")


def test_minimal_reported_digit_run_matches_single_scan_output() -> None:
    assert redact("631 5030286182 9627117") == "631 5030286<PHONE>"
    assert redact("631 5030286<PHONE>") == "631 5030286<PHONE>"


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_authorization_fstring_value_is_a_fixed_point(mode: str, secret: str | None) -> None:
    # The multiword colon-clause exemption applies to ``f"Bearer …"`` on the
    # first scan; once the token is a single placeholder the assignment scanner
    # would classify the value differently. The result must still be stable.
    text = 'extra_headers={"Authorization": f"Bearer abcdefghijklmnopqrstuvwxyz012345"})'
    once = _assert_fixed_point(text, mode, secret)
    assert "abcdefghijklmnopqrstuvwxyz012345" not in once


# --------------------------------------------------------------------------
# Placeholder edge contract: a placeholder is opaque. Text beside it was
# classified by the scan that produced it, so a boundary assertion may not be
# satisfied by the placeholder edge alone. Patterns without an assertion on
# that side still match.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("631 5030286<PHONE>", "631 5030286<PHONE>"),
        ("555-123-4567<PHONE>", "555-123-4567<PHONE>"),
        ("<PHONE>555-123-4567", "<PHONE><PHONE>"),
        ("<PHONE>,123-45-6789", "<PHONE>,<SSN>"),
        ("<REDACTED>123-45-6789", "<REDACTED>123-45-6789"),
        ("123-45-6789<REDACTED>", "123-45-6789<REDACTED>"),
        ("4111 1111 1111 1111<PHONE>", "4111 1111 1111 1111<PHONE>"),
        ("<EMAIL>4111 1111 1111 1111", "<EMAIL>4111 1111 1111 1111"),
        ("<EMAIL> 4111 1111 1111 1111", "<EMAIL> <CREDIT_CARD>"),
        ("<IP>10.0.0.1", "<IP>10.0.0.1"),
        ("10.0.0.1<IP>", "10.0.0.1<IP>"),
        ("<IPV6>2001:db8::1", "<IPV6>2001:db8::1"),
        ("2001:db8::1<IPV6>", "2001:db8::1<IPV6>"),
        ("2001:db8::1 <IPV6>", "<IPV6> <IPV6>"),
        ("<SECRET>sk-abcdefghijklmnopqrstuvwxyz", "<SECRET>sk-abcdefghijklmnopqrstuvwxyz"),
        ("<SECRET> sk-abcdefghijklmnopqrstuvwxyz", "<SECRET> <PROVIDER_KEY>"),
        ("https://x.com/a<EMAIL>", "<URL><EMAIL>"),
        ("<EMAIL>https://x.com/a", "<EMAIL><URL>"),
        ("<PHONE:0123456789ab>555-123-4567", "<PHONE:0123456789ab><PHONE>"),
        ("555-123-4567<PHONE:0123456789ab>", "555-123-4567<PHONE:0123456789ab>"),
        # A candidate survives an edge only over its full span, and a rejected
        # candidate is skipped whole like a declined one. Backtracking to a
        # shorter interior match, or resuming inside the candidate, would let a
        # re-scan replace text the producing scan skipped: on real logs that
        # turned a colon-separated hex fingerprint into one more "IPv6 address"
        # per scan until the fail-closed cap destroyed the message.
        ("4111 1111 1111 1111 123<PHONE>", "4111 1111 1111 1111 123<PHONE>"),
        ("fe80::1:2<IPV6>", "fe80::1:2<IPV6>"),
        ("<CREDIT_CARD>793834 01746814 384019", "<CREDIT_CARD>793834 01746814 384019"),
        (
            "Manifest: <IPV6>:dc:4f:01:b1:8e:61:64:39:4c:10:85:0b:a6:c4:c7:48:f0:fa:95",
            "Manifest: <IPV6>:dc:4f:01:b1:8e:61:64:39:4c:10:85:0b:a6:c4:c7:48:f0:fa:95",
        ),
    ],
)
def test_placeholder_edges_never_satisfy_boundary_assertions(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert redact(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "<PHONE>555-123-4567",
        "555-123-4567<PHONE>",
        "<REDACTED>123-45-6789",
        "123-45-6789<REDACTED>",
        "<EMAIL>4111 1111 1111 1111",
        "<IP>10.0.0.1",
        "10.0.0.1<IP>",
        "2001:db8::1<IPV6>",
        "<SECRET>sk-abcdefghijklmnopqrstuvwxyz",
        "<SECRET>AKIAABCDEFGHIJKLMNOP",
        "AKIAABCDEFGHIJKLMNOP<SECRET>",
        "<SECRET>ghp_abcdefghijklmnopqrstuvwxyz",
        "<EMAIL>https://x.com/a",
        "<URL>Bearer abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_placeholder_edge_matches_word_character_neighbour(text: str) -> None:
    """Executable statement of the edge semantic: where a regex assertion
    decides the leftmost candidate, a placeholder neighbour and a word-character
    neighbour classify the text beside them identically. (Greedy tails such as
    ``\\S+`` swallow a literal word character but stop at a placeholder, and a
    rejected candidate is skipped whole rather than re-scanned from inside, so
    those rows live in the expectation table above instead.)"""
    placeholder = next(item for item in PLACEHOLDERS if item in text)
    if text.startswith(placeholder):
        with_word = redact("x" + text[len(placeholder) :])
        assert with_word.startswith("x")
        assert redact(text) == placeholder + with_word[1:]
    else:
        assert text.endswith(placeholder)
        with_word = redact(text[: -len(placeholder)] + "x")
        assert with_word.endswith("x")
        assert redact(text) == with_word[:-1] + placeholder


def test_placeholder_edge_rule_is_mode_independent() -> None:
    text = "631 5030286182 9627117"
    hashed = redact(text, mode="hash", secret=HASH_SECRET)
    assert hashed.startswith("631 5030286<PHONE:")
    assert redact(hashed, mode="hash", secret=HASH_SECRET) == hashed


# --------------------------------------------------------------------------
# Seeded adversarial grammar over digit runs, glued tokens, literal
# placeholders, and every pattern family.
# --------------------------------------------------------------------------


def _digits(rng: random.Random, count: int) -> str:
    return "".join(rng.choice("0123456789") for _ in range(count))


def _alnum(rng: random.Random, count: int) -> str:
    return "".join(rng.choice(ALNUM) for _ in range(count))


def _luhn_card(rng: random.Random, length: int) -> str:
    digits = [rng.randrange(10) for _ in range(length - 1)]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return "".join(map(str, digits)) + str((10 - total % 10) % 10)


def _group(rng: random.Random, digits: str) -> str:
    separator = rng.choice(["", " ", "-"])
    return separator.join(digits[index : index + 4] for index in range(0, len(digits), 4))


def _token(rng: random.Random) -> str:
    kind = rng.choice(
        [
            "digit_groups",
            "digit_groups",
            "phone",
            "ssn",
            "card",
            "ipv4",
            "ipv6",
            "email",
            "url",
            "key",
            "bearer",
            "basic",
            "assignment",
            "placeholder",
            "placeholder",
            "word",
            "punct",
        ]
    )
    if kind == "digit_groups":
        groups = [
            _digits(rng, rng.choice([1, 2, 3, 4, 7, 10, 13])) for _ in range(rng.randint(1, 8))
        ]
        return rng.choice(["", " ", "-", "  "]).join(groups)
    if kind == "phone":
        area, exchange, line = _digits(rng, 3), _digits(rng, 3), _digits(rng, 4)
        return rng.choice(
            [
                f"{area}-{exchange}-{line}",
                f"{area} {exchange} {line}",
                f"({area}) {exchange}-{line}",
                f"+1 {area} {exchange} {line}",
                f"+44-{area}-{exchange}-{line}",
                f"{area}{exchange}{line}",
            ]
        )
    if kind == "ssn":
        return f"{_digits(rng, 3)}-{_digits(rng, 2)}-{_digits(rng, 4)}"
    if kind == "card":
        card = _luhn_card(rng, rng.choice([13, 15, 16, 19]))
        if rng.random() < 0.3:
            card = card[:-1] + str((int(card[-1]) + 1) % 10)
        return _group(rng, card)
    if kind == "ipv4":
        return ".".join(str(rng.randrange(256)) for _ in range(4))
    if kind == "ipv6":
        return rng.choice(
            [
                "2001:db8::1",
                "fe80::1%eth0",
                "::ffff:192.0.2.1",
                "[2001:db8::1]:8080",
                "2001:db8::1:54321",
                "dead::beef::codec",
                "::1",
                "fe80::1.",
                "std::vector",
                "12:30",
            ]
        )
    if kind == "email":
        return rng.choice(
            [
                f"{_alnum(rng, 5)}@{_alnum(rng, 4)}.com",
                f"{_alnum(rng, 3)}.{_alnum(rng, 3)}+tag@{_alnum(rng, 4)}.example.co.uk",
                f"{_digits(rng, 10)}@{_alnum(rng, 4)}.io",
            ]
        )
    if kind == "url":
        return rng.choice(
            [
                "https://example.com/path?q=1&e=a@b.com",
                "http://10.0.0.1:8080/x",
                "https://[2001:db8::1]/",
                "https://x.com/555-123-4567",
                "https://x.com/a",
            ]
        )
    if kind == "key":
        return rng.choice(
            [
                "sk-" + _alnum(rng, 24),
                "sk-proj-" + _alnum(rng, 24),
                "AKIA" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16)),
                "AIza" + _alnum(rng, 32),
                "ghp_" + _alnum(rng, 24),
                "github_pat_" + _alnum(rng, 24),
            ]
        )
    if kind == "bearer":
        return "Bearer " + _alnum(rng, rng.randint(16, 40))
    if kind == "basic":
        return "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    if kind == "assignment":
        key = rng.choice(
            ["password", "api_key", "token", "client_secret", "Authorization", "x-api-key", "cookie"]
        )
        separator = rng.choice(["=", ": ", ":", " = "])
        value = rng.choice(
            [
                _alnum(rng, 12),
                f'"{_alnum(rng, 12)}"',
                f"'{_alnum(rng, 8)} {_alnum(rng, 8)}'",
                f"{_alnum(rng, 6)} {_alnum(rng, 6)}",
                f'f"Bearer {_alnum(rng, 20)}"',
                f"Bearer {_alnum(rng, 20)}",
                f"{_digits(rng, 3)} {_digits(rng, 3)} {_digits(rng, 4)}",
                '""',
                rng.choice(PLACEHOLDERS),
                "abc" + rng.choice(PLACEHOLDERS),
            ]
        )
        return f"{key}{separator}{value}"
    if kind == "placeholder":
        return rng.choice(PLACEHOLDERS)
    if kind == "word":
        return rng.choice(
            ["hello", "order", "id", "naïve", "東京", "x", "A", "_", "v1.2.3", "3.14159", "π", "f"]
        )
    return rng.choice([",", ".", ";", ":", "/", "(", ")", '"', "'", "{", "}", "[", "]", "\\", "#", "@"])


def _case(seed: int) -> str:
    rng = random.Random(seed)
    tokens = [_token(rng) for _ in range(rng.randint(1, 8))]
    text = tokens[0]
    for token in tokens[1:]:
        text += rng.choice(["", " ", "  ", "\n", "-", ",", ", ", ":", "/", "."]) + token
    return text


def _digit_run_case(seed: int) -> str:
    rng = random.Random(seed)
    groups = [_digits(rng, rng.randint(1, 12)) for _ in range(rng.randint(1, 14))]
    text = rng.choice([" ", "-", "  ", " - "]).join(groups)
    if rng.random() < 0.3:
        text = rng.choice(PLACEHOLDERS) + rng.choice(["", " "]) + text
    if rng.random() < 0.3:
        text = text + rng.choice(["", " "]) + rng.choice(PLACEHOLDERS)
    return text


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_generated_mixed_tokens_are_fixed_points(mode: str, secret: str | None) -> None:
    for seed in range(3000):
        text = _case(seed)
        _assert_fixed_point(text, mode, secret)


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_generated_digit_runs_are_fixed_points(mode: str, secret: str | None) -> None:
    for seed in range(1500):
        text = _digit_run_case(seed)
        _assert_fixed_point(text, mode, secret)


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_known_mechanisms_converge_within_three_changing_scans(
    mode: str, secret: str | None
) -> None:
    # Premise pinned as a test: the placeholder edge rule removes the boundary
    # cascade, and assignment values can collapse in a chain of at most two
    # further scans (see the explicit chain below). Digit runs may need one
    # extra scan: a replacement moves where the next declined candidate begins,
    # so an interior candidate the first scan skipped (``re.sub`` resumes after
    # a declined candidate) can become visible once. A 40,000-input sweep found
    # no case slower than this. Anything slower is a new mechanism that must be
    # investigated, not absorbed by the fail-closed cap.
    for seed in range(3000):
        text = _case(seed)
        scans = _changing_scans(text, mode, secret)
        assert scans <= 3, f"{scans} changing scans for {text!r}"
    for seed in range(1500):
        text = _digit_run_case(seed)
        scans = _changing_scans(text, mode, secret)
        assert scans <= 2, f"{scans} changing scans for {text!r}"


def test_assignment_value_collapse_chain_is_bounded() -> None:
    # Scan 1 collapses ``token=b c`` (the value ends where ``https:`` looks like
    # a following assignment) and replaces the URL. Scan 2 sees that value
    # extend to the URL placeholder and collapses it again. Scan 3 collapses
    # the outer colon clause once it is a single token. Nesting deeper adds no
    # further scans because the outermost clause collapses everything inside.
    text = "token:x/token=b c, https://u"
    assert _changing_scans(text, "redact", None) == 3
    assert redact(text) == "token:<SECRET>"
    assert _changing_scans("token=v, https://a token=v, https://b", "redact", None) == 2


def test_long_digit_table_converges_in_one_changing_scan() -> None:
    # 0.1.0a23 needed one scan per eleven characters on this shape: each unit
    # of the table lost one more "phone number" per scan.
    text = " ".join(["1816334467", "7522431712", "1992458631", "5030286182", "9627117"] * 1200)
    assert len(text.encode("utf-8")) > 60_000
    assert _changing_scans(text, "redact", None) == 1
    assert _assert_fixed_point(text, "redact", None).count("<PHONE>") == 1200


# --------------------------------------------------------------------------
# Structural guarantee: bounded iteration that fails closed.
# --------------------------------------------------------------------------


def test_redact_fails_closed_when_no_fixed_point_within_cap(monkeypatch) -> None:
    calls = []

    def never_stable(text: str, mode: str, secret: str | None) -> str:
        calls.append(text)
        return text + "."

    monkeypatch.setattr(redaction_module, "_redact_once", never_stable)
    assert redact("anything") == "<REDACTED>"
    assert len(calls) == redaction_module._MAX_REDACTION_PASSES


def test_unchanged_text_costs_one_scan(monkeypatch) -> None:
    calls = []
    original = redaction_module._redact_once

    def counting(text: str, mode: str, secret: str | None) -> str:
        calls.append(text)
        return original(text, mode, secret)

    monkeypatch.setattr(redaction_module, "_redact_once", counting)
    assert redact("nothing sensitive here") == "nothing sensitive here"
    assert len(calls) == 1
    calls.clear()
    assert redact("mail a@b.com") == "mail <EMAIL>"
    assert len(calls) == 2


# --------------------------------------------------------------------------
# Structure level: the guarantee must survive every sink that re-sanitizes.
# --------------------------------------------------------------------------


def _adversarial_structure() -> dict:
    colliding_keys = {f"user{index}@example.com": f"{index:03d}-45-6789" for index in range(12)}
    return {
        "content": f"{DIGIT_RUN} and jane@acme.com <PHONE>555-123-4567 password: abc555 123 4567",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "lookup",
                    "arguments": json.dumps(
                        {
                            **colliding_keys,
                            "password": "hunter2",
                            "note": 'extra_headers={"Authorization": f"Bearer abcdefghijklmnopqrstuvwxyz012345"})',
                        }
                    ),
                },
            }
        ],
        "metadata": {
            "ips": ["10.0.0.1<IP>", "<IPV6>2001:db8::1", "2001:db8::1 <IPV6>"],
            "nested": {"a@b.com": {"password": ""}, "b@c.com": {"token": "<SECRET>"}},
        },
    }


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_redact_structure_is_a_fixed_point(mode: str, secret: str | None) -> None:
    once = redact_structure(_adversarial_structure(), mode=mode, secret=secret)
    assert redact_structure(deepcopy(once), mode=mode, secret=secret) == once
    assert json.dumps(redact_structure(deepcopy(once), mode=mode, secret=secret)) == json.dumps(once)


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_redact_messages_is_a_fixed_point(mode: str, secret: str | None) -> None:
    messages = [{"role": "user", **_adversarial_structure()}]
    once = redact_messages(messages, mode=mode, secret=secret)
    assert redact_messages(deepcopy(once), mode=mode, secret=secret) == once


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_sanitize_trace_is_a_fixed_point(mode: str, secret: str | None) -> None:
    def build() -> Trace:
        return Trace(
            trace_id="trace-1",
            started_at=NOW,
            prompt_redacted=DIGIT_RUN,
            response_redacted='"Authorization": f"Bearer abcdefghijklmnopqrstuvwxyz012345"',
            error=f"failed for {DIGIT_RUN}",
            raw_messages=[{"role": "user", **_adversarial_structure()}],
            tags=_adversarial_structure(),
            service_name="svc 555-123-4567",
            environment="<PHONE>555-123-4567",
        )

    once = sanitize_trace(build(), mode=mode, secret=secret)
    twice = sanitize_trace(deepcopy(once), mode=mode, secret=secret)
    assert twice == once


def _agent_bundle() -> AgentRunBundle:
    session = SourceSession(
        source_session_id="ses_1",
        tenant_id="tenant-a",
        source_kind="custom-agent",
        source_locator_hash="a" * 64,
        started_at=NOW,
        ended_at=NOW,
        observed_at=NOW,
    )
    run = AgentRun(
        run_id="run_1",
        source_session_id="ses_1",
        tenant_id="tenant-a",
        started_at=NOW,
        ended_at=NOW,
        status=ExecutionStatus.COMPLETED,
        agent_name=f"agent {DIGIT_RUN}",
    )
    turn = AgentTurn(
        turn_id="turn_1",
        run_id="run_1",
        sequence=0,
        started_at=NOW,
        ended_at=NOW,
        status=ExecutionStatus.COMPLETED,
        user_request_redacted=f"recite {DIGIT_RUN}",
        final_response_redacted='"Authorization": f"Bearer abcdefghijklmnopqrstuvwxyz012345"',
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.PRESENT,
    )
    event = AgentEvent(
        event_id="event_1",
        turn_id="turn_1",
        sequence=0,
        occurred_at=NOW,
        event_type=AgentEventType.TOOL_RESULT,
        status=ExecutionStatus.COMPLETED,
        provenance="custom-agent:tool-result",
        privacy_classification=PrivacyClassification.REDACTED,
        attributes={
            "tool_name": "lookup",
            "call_id": "call-1",
            "result": {
                "content": f"{DIGIT_RUN} jane@acme.com <PHONE>555-123-4567",
                "note": 'extra_headers={"Authorization": f"Bearer abcdefghijklmnopqrstuvwxyz012345"})',
                "ips": ["10.0.0.1<IP>", "<IPV6>2001:db8::1"],
                "a@b.com": {"password": ""},
                "c@d.com": {"password": "x"},
            },
        },
    )
    return AgentRunBundle(session=session, run=run, turns=(turn,), events=(event,))


@pytest.mark.parametrize(("mode", "secret"), MODES)
def test_sanitize_agent_run_bundle_is_a_fixed_point(mode: str, secret: str | None) -> None:
    once = sanitize_agent_run_bundle(_agent_bundle(), mode=mode, secret=secret)
    assert sanitize_agent_run_bundle(once, mode=mode, secret=secret) == once


# --------------------------------------------------------------------------
# Compatibility: snapshots stored by 0.1.0a23 keep validating with their
# stored revision, because the edge rule only rejects or shortens matches and
# re-scans positions only after a rejection, so a23-stable text is unchanged.
# --------------------------------------------------------------------------

A23_SNAPSHOT = json.loads(
    '{"end_status": "complete", "event_at": "2026-09-20T12:00:00+00:00", '
    '"id": "0123456789abcdef0123456789abcdef", "input_issues": [], '
    '"labels": {"channel": "voice <PHONE>"}, "messages": [{"content": '
    '"mail <EMAIL>, card <CREDIT_CARD>, ip <IP>", "role": "user", "status": '
    '"completed"}, {"content": "Authorization: <BEARER_TOKEN> and password=<SECRET>", '
    '"role": "assistant", "status": "completed"}], "revision": '
    '"c453ea18c2624575e9d7564563db59c69dbdebeb40259b355fcc29c9e6d5e75a", '
    '"source_scope": "0123456789abcdef", "tenant_id": "tenant-a"}'
)


def test_snapshot_stored_by_0_1_0a23_still_validates_with_its_revision() -> None:
    assert validate_conversation(deepcopy(A23_SNAPSHOT)) == A23_SNAPSHOT
