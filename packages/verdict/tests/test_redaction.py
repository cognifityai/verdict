from verdict.redaction import redact, redact_messages


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


def test_redact_messages_does_not_mutate_input():
    msgs = [{"role": "user", "content": "user@a.co"}]
    redact_messages(msgs)
    assert msgs[0]["content"] == "user@a.co"  # original untouched


def test_redact_messages_non_list_returns_none():
    assert redact_messages("not a list") is None
    assert redact_messages(None) is None


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
