"""PII redaction. Modes: redact, hash. ("encrypt" is planned, not implemented —
rejected at init().)

Redaction is built-in regex + Luhn (credit-card checksum); it needs no extra
dependencies. A deeper Presidio-based pass is a possible future addition but is
NOT currently wired in.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Literal

RedactionMode = Literal["redact", "hash", "encrypt"]


# Cheap, fast first-pass patterns. This is NOT comprehensive PII detection — it
# covers the common, high-frequency accidental leaks in LLM traffic:
# email, URL, US SSN, payment-card-shaped digit runs, IPv4/IPv6, and US-style
# phone numbers. Known gaps (NOT handled — regex has no entity model):
# names, postal addresses, dates of birth, IBAN/passport numbers, separator-less
# phone numbers, and most non-US formats. Do not treat regex-only redaction as a
# compliance guarantee. A deeper entity-aware pass (e.g. Presidio) is a possible
# future addition but is NOT wired in today.
_PATTERNS = {
    # Order matters: EMAIL runs before URL so an email embedded in a URL is
    # matched (and hashed) as an EMAIL first, keeping hash-mode values stable
    # across prose vs. URL contexts. More-specific numeric patterns run before
    # greedier ones like PHONE.
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "URL": re.compile(r"https?://\S+"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # CANDIDATE matcher only — a 13–19 digit run (optionally space/dash grouped)
    # is merely *card-shaped*. We do NOT redact on the regex alone (that
    # irreversibly clobbered order IDs, tracking numbers, etc.). A candidate is
    # only redacted if it passes the Luhn checksum AND has a valid card length;
    # see _redact_credit_card / _luhn_ok.
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "IPV6": re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "PHONE": re.compile(r"(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)[ -]?|\d{3}[ -])\d{3}[ -]?\d{4}\b"),
}


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
    if mode == "encrypt":
        raise NotImplementedError("encrypt mode is deferred to v0.5")
    if mode == "hash" and not secret:
        raise ValueError("hash mode requires a redaction_secret")

    out = text
    for label, pat in _PATTERNS.items():
        if label == "CREDIT_CARD":
            # Gate on Luhn + valid length so non-card digit runs survive intact.
            out = pat.sub(
                lambda m, lbl=label: _credit_card_repl(m.group(0), lbl, mode, secret),
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


def _hash_match(value: str, label: str, secret: str | None) -> str:
    assert secret is not None
    h = hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    return f"<{label}:{h.hexdigest()[:12]}>"


def redact_messages(
    messages: object,
    mode: RedactionMode = "redact",
    secret: str | None = None,
) -> list[dict] | None:
    """Redact PII inside a list of chat-message dicts, preserving structure.

    The structured `raw_messages` payload must honor the same redaction as the
    flattened `prompt_redacted` text — otherwise content capture leaks raw PII
    into storage despite redaction being enabled. This walks each message's
    `content` (string or list of blocks) and redacts string text in place on a
    copy. Never mutates the input. On any unexpected shape it drops the content
    to `<REDACTED>` rather than risk leaking it.
    """
    if not isinstance(messages, list):
        return None
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        nm = dict(m)
        content = m.get("content")
        try:
            if isinstance(content, str):
                nm["content"] = redact(content, mode=mode, secret=secret)
            elif isinstance(content, list):
                blocks = []
                for b in content:
                    if isinstance(b, dict):
                        nb = dict(b)
                        if isinstance(b.get("text"), str):
                            nb["text"] = redact(b["text"], mode=mode, secret=secret)
                        blocks.append(nb)
                    elif isinstance(b, str):
                        blocks.append(redact(b, mode=mode, secret=secret))
                    else:
                        blocks.append(b)
                nm["content"] = blocks
            elif content is not None:
                nm["content"] = "<REDACTED>"
        except Exception:
            nm["content"] = "<REDACTED>"
        out.append(nm)
    return out
