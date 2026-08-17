"""PII redaction. Modes: redact, hash. ("encrypt" is planned, not implemented —
rejected at init().)

Redaction uses built-in pattern matching, a linear ``@``-anchored email scanner,
Luhn card checks, and standard-library IP validation; it needs no extra
dependencies. A deeper
Presidio-based pass is a possible future addition but is NOT currently wired in.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from verdict.schema import Judgment, SpanRecord, Trace

RedactionMode = Literal["redact", "hash", "encrypt"]

_REDACTED = "<REDACTED>"
_MAX_NESTING_DEPTH = 64
_MAX_STRUCTURE_NODES = 10_000
_MAX_STRUCTURE_CHARACTERS = 1_000_000

# Longest textual IPv6 address:
# ``ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255``. Bounding each candidate
# side to this length keeps discovery linear even when an unrelated colon
# appears elsewhere in attacker-controlled text. Validation below remains the
# authority on whether the bounded candidate is actually an address.
_IPV6_MAX_TEXT_LEN = 45

# Provider message fields whose shape Verdict currently preserves in
# Trace.raw_messages. Values inside these fields are recursively sanitized.
# Unknown top-level fields are dropped rather than copied across the persistence
# boundary. Content blocks and tool payloads may themselves contain arbitrary
# JSON-compatible keys, all of which are preserved and sanitized recursively.
_MESSAGE_FIELDS = (
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "function_call",
    "metadata",
    "refusal",
    "audio",
    "cache_control",
)


# Cheap, fast first-pass patterns. This is NOT comprehensive PII detection — it
# covers the common, high-frequency accidental leaks in LLM traffic:
# email, URL, US SSN, payment-card-shaped digit runs, IPv4/IPv6, and US-style
# phone numbers. Known gaps (NOT handled — regex has no entity model):
# names, postal addresses, dates of birth, IBAN/passport numbers, separator-less
# phone numbers, and most non-US formats. Do not treat regex-only redaction as a
# compliance guarantee. A deeper entity-aware pass (e.g. Presidio) is a possible
# future addition but is NOT wired in today.
_PATTERNS = {
    # Email candidates are handled by the linear at-sign scanner below before
    # this mapping, so an email embedded in a URL is still classified as EMAIL.
    # More-specific numeric patterns run before greedier ones like PHONE.
    "URL": re.compile(r"https?://\S+"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # CANDIDATE matcher only — a 13-19 digit run (optionally space/dash grouped)
    # is merely *card-shaped*. We do NOT redact on the regex alone (that
    # irreversibly clobbered order IDs, tracking numbers, etc.). A candidate is
    # only redacted if it passes the Luhn checksum AND has a valid card length;
    # see _redact_credit_card / _luhn_ok.
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    # Candidate matcher only. Colons also occur in clocks and other structured
    # values and namespace separators. Match a whole token (including an IPv4
    # tail or scope ID) and validate it with ipaddress.IPv6Address below. The
    # word boundaries prevent a valid suffix such as ``d::`` from being carved
    # out of ``std::vector``. Both halves are bounded because the earlier
    # unbounded stars rescanned a long candidate-shaped run from every start
    # position whenever any unrelated colon existed later in the text.
    "IPV6": re.compile(
        r"(?<![0-9A-Za-z_])"
        rf"[0-9A-Fa-f:.]{{0,{_IPV6_MAX_TEXT_LEN}}}:"
        rf"[0-9A-Fa-f:.]{{0,{_IPV6_MAX_TEXT_LEN}}}"
        r"(?:%[0-9A-Za-z_.-]+)?"
        r"(?![0-9A-Za-z_])"
    ),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "PHONE": re.compile(r"(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)[ -]?|\d{3}[ -])\d{3}[ -]?\d{4}\b"),
}


def _is_word_character(character: str) -> bool:
    """Match the practical Unicode behavior of regex ``\\w`` per code point."""
    return character == "_" or character.isalnum()


def _is_email_local_character(character: str) -> bool:
    return _is_word_character(character) or character in ".+-"


def _is_email_domain_label_character(character: str) -> bool:
    return _is_word_character(character) or character == "-"


def _is_email_domain_tail_character(character: str) -> bool:
    return _is_email_domain_label_character(character) or character == "."


def _redact_emails(
    text: str,
    mode: RedactionMode,
    secret: str | None,
) -> str:
    """Redact email-shaped candidates in one monotonic scan.

    The previous search regex began with an unbounded local-part expression, so
    every possible start position rescanned a long token when the eventual
    candidate was malformed. Anchoring discovery on each ``@`` means every code
    point is visited a bounded number of times. Deliberately retain the previous
    permissive candidate grammar; validation breadth is unchanged here.
    """
    if "@" not in text:
        return text

    output: list[str] = []
    output_cursor = 0
    search_cursor = 0
    text_length = len(text)

    while True:
        at_index = text.find("@", search_cursor)
        if at_index < 0:
            break

        start = at_index
        while (
            start > output_cursor
            and _is_email_local_character(text[start - 1])
        ):
            start -= 1

        domain_end = at_index + 1
        while (
            domain_end < text_length
            and _is_email_domain_label_character(text[domain_end])
        ):
            domain_end += 1

        has_local = start < at_index
        has_domain_label = domain_end > at_index + 1
        has_dot = domain_end < text_length and text[domain_end] == "."
        tail_end = domain_end + 1
        if has_dot:
            while (
                tail_end < text_length
                and _is_email_domain_tail_character(text[tail_end])
            ):
                tail_end += 1
        has_domain_tail = has_dot and tail_end > domain_end + 1

        if not (has_local and has_domain_label and has_domain_tail):
            search_cursor = at_index + 1
            continue

        candidate = text[start:tail_end]
        output.append(text[output_cursor:start])
        output.append(
            _hash_match(candidate, "EMAIL", secret)
            if mode == "hash"
            else "<EMAIL>"
        )
        output_cursor = tail_end
        search_cursor = tail_end

    if not output:
        return text
    output.append(text[output_cursor:])
    return "".join(output)


def redact(
    text: str | None,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> str | None:
    """Redact PII from text.

    Args:
        text: The input string (or None).
        mode: "redact" → replace with placeholder; "hash" → HMAC-SHA-256 the value;
              "encrypt" → not yet implemented in v0.
        secret: HMAC secret (required for hash mode).

    Returns:
        Redacted string, or None if input was None.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return _REDACTED
    if mode == "encrypt":
        raise NotImplementedError("encrypt mode is deferred to v0.5")
    if mode == "hash" and not secret:
        raise ValueError("hash mode requires a redaction_secret")

    # Email discovery is first for classification stability in URL contexts.
    out = _redact_emails(text, mode, secret)
    for label, pat in _PATTERNS.items():
        if label == "CREDIT_CARD":
            # Gate on Luhn + valid length so non-card digit runs survive intact.
            out = pat.sub(
                lambda m, lbl=label: _credit_card_repl(m.group(0), lbl, mode, secret),
                out,
            )
        elif label == "IPV6":
            # A colon is required by every IPv6 spelling.  Avoid entering the
            # candidate regex when one is absent: its permissive ``[0-9a-f:.]*``
            # prefix would otherwise rescan long digit/dot runs from every
            # position before proving that no candidate exists.
            if ":" not in out:
                continue
            out = pat.sub(
                lambda m, lbl=label: _ipv6_repl(m.group(0), lbl, mode, secret),
                out,
            )
        elif mode == "hash":
            out = pat.sub(lambda m, lbl=label: _hash_match(m.group(0), lbl, secret), out)
        else:  # redact
            out = pat.sub(f"<{label}>", out)
    return out


# Valid PAN lengths in circulation (digits only): 13 (older Visa), 14 (Diners),
# 15 (Amex), 16 (most), 19 (some Visa/Maestro). 17 and 18 are not assigned, so
# they're treated as non-cards even if they happen to pass Luhn.
_VALID_CARD_LENGTHS = frozenset({13, 14, 15, 16, 19})


def _luhn_ok(digits: str) -> bool:
    """Return True if `digits` (a string of decimal digits) passes the Luhn
    checksum. Empty/non-digit input returns False."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    # Double every second digit counting from the rightmost.
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48  # int(ch), faster/explicit
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _credit_card_repl(value: str, label: str, mode: RedactionMode, secret: str | None) -> str:
    """Replace a CREDIT_CARD *candidate* only if it's a plausible card number.

    Non-Luhn or wrong-length digit runs (order IDs, tracking numbers, etc.) are
    returned unchanged so they aren't irreversibly destroyed.
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) not in _VALID_CARD_LENGTHS or not _luhn_ok(digits):
        return value  # leave non-cards untouched
    if mode == "hash":
        return _hash_match(value, label, secret)
    return f"<{label}>"


def _ipv6_repl(
    value: str,
    label: str,
    mode: RedactionMode,
    secret: str | None,
) -> str:
    """Replace only candidates that the standard library validates as IPv6."""
    try:
        ipaddress.IPv6Address(value)
    except ipaddress.AddressValueError:
        return value
    if mode == "hash":
        return _hash_match(value, label, secret)
    return f"<{label}>"


def _hash_match(value: str, label: str, secret: str | None) -> str:
    assert secret is not None
    h = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    return f"<{label}:{h.hexdigest()[:12]}>"


def redact_messages(
    messages: object,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> list[dict] | None:
    """Sanitize supported provider message shapes without mutating the input.

    Supported top-level fields are listed in ``_MESSAGE_FIELDS``. Their values
    may contain arbitrary JSON-compatible dict/list structures, including
    OpenAI tool-call arguments and Anthropic tool inputs/results. Every string
    key and value is redacted recursively. Unknown top-level fields are dropped;
    unsupported values, cycles, and excessive nesting fail closed.
    """
    if not isinstance(messages, list):
        return None
    out: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            out.append({"role": "unknown", "content": _REDACTED})
            continue
        sanitized = {}
        for key in _MESSAGE_FIELDS:
            if key not in message:
                continue
            sanitized_key = redact(key, mode=mode, secret=secret) or key
            sanitized[sanitized_key] = redact_structure(
                message[key], mode=mode, secret=secret
            )
        out.append(sanitized)
    return out


def redact_structure(
    value: Any,
    mode: RedactionMode = "redact",
    secret: str | None = None,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
    _memo: dict[tuple[int, int], tuple[Any, Any]] | None = None,
    _shared: set[int] | None = None,
) -> Any:
    """Recursively redact a JSON-compatible value, failing closed otherwise.

    Provider payloads are expected to be JSON trees. Python-only shared or
    cyclic container graphs cannot be represented faithfully in JSON, so every
    multiply referenced non-root container fails closed. This prevents a tiny
    shared DAG from expanding exponentially at the later ``json.dumps`` storage
    boundary and guarantees that returned containers are never aliased.
    """
    if _shared is None:
        shared, within_budget = _analyze_structure(value)
        if not within_budget:
            return _REDACTED
        # A directly supplied cyclic root remains representable as a container
        # whose back-edge is redacted. Shared descendants are redacted at every
        # occurrence, making the result independent of traversal order.
        if isinstance(value, (dict, list, tuple)):
            shared.discard(id(value))
        _shared = shared

    if _depth > _MAX_NESTING_DEPTH:
        return _REDACTED
    if isinstance(value, str):
        return redact(value, mode=mode, secret=secret)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _REDACTED

    seen = _seen if _seen is not None else set()
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen or identity in _shared:
            return _REDACTED
        seen.add(identity)
        try:
            entries: list[tuple[str, str, Any]] = []
            for key, child in value.items():
                if not isinstance(key, str):
                    return _REDACTED
                sanitized_key = redact(key, mode=mode, secret=secret) or _REDACTED
                if key == "arguments" and isinstance(child, str):
                    try:
                        parsed_arguments = json.loads(child)
                    except (TypeError, json.JSONDecodeError):
                        parsed_arguments = None
                    if isinstance(parsed_arguments, (dict, list)):
                        sanitized_arguments = redact_structure(
                            parsed_arguments,
                            mode=mode,
                            secret=secret,
                            _depth=_depth + 1,
                            _seen=seen,
                            _shared=None,
                        )
                        entries.append((
                            sanitized_key,
                            key,
                            json.dumps(sanitized_arguments, separators=(",", ":")),
                        ))
                        continue
                entries.append((
                    sanitized_key,
                    key,
                    redact_structure(
                        child,
                        mode=mode,
                        secret=secret,
                        _depth=_depth + 1,
                        _seen=seen,
                        _shared=_shared,
                    ),
                ))
        finally:
            seen.remove(identity)
        return _mapping_from_redacted_entries(entries)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen or identity in _shared:
            return _REDACTED
        seen.add(identity)
        try:
            result = [
                redact_structure(
                    child,
                    mode=mode,
                    secret=secret,
                    _depth=_depth + 1,
                    _seen=seen,
                    _shared=_shared,
                )
                for child in value
            ]
        finally:
            seen.remove(identity)
        return result
    return _REDACTED


def _analyze_structure(value: Any) -> tuple[set[int], bool]:
    """Return multiply referenced container IDs and whether input is bounded."""
    references: Counter[int] = Counter()
    traversed: set[int] = set()
    nodes = 0
    characters = 0
    stack: list[tuple[Any, int]] = [(value, 0)]

    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_STRUCTURE_NODES:
            return set(), False
        if isinstance(current, str):
            characters += len(current)
            if characters > _MAX_STRUCTURE_CHARACTERS:
                return set(), False
            continue
        if depth > _MAX_NESTING_DEPTH or not isinstance(current, (dict, list, tuple)):
            continue

        identity = id(current)
        references[identity] += 1
        if identity in traversed:
            continue
        traversed.add(identity)

        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, str):
                    characters += len(key)
                    if characters > _MAX_STRUCTURE_CHARACTERS:
                        return set(), False
                stack.append((child, depth + 1))
        else:
            stack.extend((child, depth + 1) for child in current)

    return {identity for identity, count in references.items() if count > 1}, True


def _mapping_from_redacted_entries(
    entries: list[tuple[str, str, Any]],
) -> dict[str, Any]:
    """Build a deterministic mapping without dropping colliding redacted keys."""
    counts = Counter(sanitized for sanitized, _original, _value in entries)
    result: dict[str, Any] = {}
    used: set[str] = set()

    for sanitized, _original, child in sorted(entries, key=lambda item: (item[0], item[1])):
        candidate = sanitized
        if counts[sanitized] > 1 or candidate in used:
            suffix = 1
            candidate = f"{sanitized}#{suffix}"
            while candidate in used:
                suffix += 1
                candidate = f"{sanitized}#{suffix}"
        used.add(candidate)
        result[candidate] = child
    return result


def sanitize_trace(
    trace: Trace,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> Trace:
    """Apply the content privacy boundary to every content-bearing Trace field.

    Instrumentors call this with the configured mode before persistence. Storage
    adapters call it again in default redact mode so manually constructed traces
    cannot bypass the boundary. Redaction placeholders and hashes are idempotent.
    """
    trace.prompt_redacted = redact(trace.prompt_redacted, mode=mode, secret=secret)
    trace.response_redacted = redact(trace.response_redacted, mode=mode, secret=secret)
    trace.error = redact(trace.error, mode=mode, secret=secret)
    if trace.raw_messages is not None:
        trace.raw_messages = redact_messages(
            trace.raw_messages, mode=mode, secret=secret
        )
    sanitized_tags = redact_structure(trace.tags, mode=mode, secret=secret)
    # Keep the marker rather than silently dropping to {}: an over-budget or
    # unrepresentable structure must be visible, not indistinguishable from
    # "this trace had no tags".
    trace.tags = (
        sanitized_tags
        if isinstance(sanitized_tags, dict)
        else {"verdict.redaction_status": str(sanitized_tags)}
    )
    return trace


def sanitize_judgment(
    judgment: Judgment,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> Judgment:
    """Redact content-bearing judge output without changing evaluator identity."""
    judgment.error = redact(judgment.error, mode=mode, secret=secret)
    for dimension in judgment.dimensions:
        dimension.reasoning = (
            redact(dimension.reasoning, mode=mode, secret=secret) or ""
        )
    return judgment


def sanitize_span(
    span: SpanRecord,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> SpanRecord:
    """Redact manual-span names, nested attributes, and errors in place."""
    span.name = redact(span.name, mode=mode, secret=secret) or ""
    span.parent_name = redact(span.parent_name, mode=mode, secret=secret)
    span.error = redact(span.error, mode=mode, secret=secret)
    sanitized_attributes = redact_structure(
        span.attributes, mode=mode, secret=secret
    )
    # See sanitize_trace: preserve the marker instead of wiping every sibling
    # attribute and leaving no evidence that anything was dropped.
    span.attributes = (
        sanitized_attributes
        if isinstance(sanitized_attributes, dict)
        else {"verdict.redaction_status": str(sanitized_attributes)}
    )
    return span
