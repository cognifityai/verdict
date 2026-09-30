"""Unlabeled vendor credentials with distinctive prefixes are redacted.

Fixtures are assembled at runtime so this file never contains a literal string
that secret scanners treat as a live credential.
"""

from __future__ import annotations

import json
import time

import pytest
import verdict
from verdict.redaction import redact, redact_structure
from verdict.storage.memory import InMemoryStorage


def _token(*parts: str) -> str:
    return "".join(parts)


_BODY = "Q7vN2kX9pL4mR8tZ1wY6cB3hJ5dF0gSe"  # 32 alphanumerics
assert len(_BODY) == 32

# One representative per supported vendor format.
VENDOR_TOKENS = {
    "stripe_secret_live": _token("sk", "_live_", _BODY),
    "stripe_secret_test": _token("sk", "_test_", _BODY),
    "stripe_restricted": _token("rk", "_live_", _BODY),
    "stripe_webhook": _token("wh", "sec_", _BODY),
    "hugging_face": _token("hf", "_", _BODY, "Ab"),
    "gitlab_pat": _token("gl", "pat-", _BODY[:20]),
    "gitlab_routable_pat": _token("gl", "pat-", _BODY[:27], ".01.", "171a2b3c4"),
    "gitlab_deploy": _token("gl", "dt-", _BODY[:20]),
    "gitlab_runner": _token("gl", "rt-", _BODY[:20]),
    "slack_bot": _token("xo", "xb-", "2468013579-1357924680-", _BODY[:24]),
    "slack_user": _token("xo", "xp-", "2468013579-1357924680-2468013579-", _BODY),
    "slack_app": _token("xa", "pp-1-", "A0123456789-2468013579-", _BODY),
    "slack_rotating": _token("xo", "xe.xo", "xp-1-", _BODY),
    "npm": _token("np", "m_", _BODY, "Wx7a"),
    "pypi": _token("py", "pi-AgEIcHlwaS5vcmc", _BODY, _BODY),
    "groq": _token("gs", "k_", _BODY, _BODY[:20]),
    "xai": _token("xa", "i-", _BODY, _BODY[:20]),
    "replicate": _token("r8", "_", _BODY, "Hk3"),
    "perplexity": _token("pp", "lx-", _BODY, _BODY[:16]),
    "langsmith": _token("ls", "v2_pt_", "0123456789abcdef0123456789abcdef", "_", "0a1b2c3d4e"),
}


def _fragments(token: str) -> tuple[str, ...]:
    # A partial redaction can leave a usable credential tail behind.
    return (token, token[-16:])


@pytest.mark.parametrize("name", sorted(VENDOR_TOKENS))
@pytest.mark.parametrize(
    "template",
    [
        "{token}",
        "my key is {token} and it stopped working",
        "use {token}.",
        '{{"key": "{token}", "note": "ok"}}',
        "curl https://example.invalid/hook?token={token}&x=1",
        "'{token}'",
        "({token})",
        "line one\n{token}\nline three",
    ],
)
def test_unlabeled_vendor_token_is_removed_in_common_contexts(name: str, template: str) -> None:
    token = VENDOR_TOKENS[name]
    text = template.format(token=token)

    output = redact(text)

    assert output is not None
    for fragment in _fragments(token):
        assert fragment not in output, f"{name} fragment survived: {output!r}"


@pytest.mark.parametrize("name", sorted(VENDOR_TOKENS))
def test_unlabeled_vendor_token_uses_the_provider_key_placeholder(name: str) -> None:
    token = VENDOR_TOKENS[name]

    assert redact(f"key {token} here") == "key <PROVIDER_KEY> here"


@pytest.mark.parametrize("name", sorted(VENDOR_TOKENS))
def test_unlabeled_vendor_token_is_hashed_in_hash_mode(name: str) -> None:
    token = VENDOR_TOKENS[name]

    output = redact(f"key {token} here", mode="hash", secret="s")

    assert output is not None
    assert token not in output
    assert output.startswith("key <PROVIDER_KEY:") and output.endswith("> here")


@pytest.mark.parametrize(
    "text",
    [
        # Publishable keys are public by design and must stay readable.
        _token("pk", "_live_", _BODY),
        _token("pk", "_test_", _BODY),
        # Ordinary identifiers that share a vendor prefix.
        "from huggingface_hub import hf_hub_download",
        "npm_config_cache=/tmp/npm",
        "glpat- tokens start with this prefix",
        "set the xoxb- prefix in the Slack settings",
        "SLACK_TOKEN placeholder xoxb-your-token-here",
        "the model xai-grok-2 answered",
        "replicate model r8_small",
        "documentation placeholder sk_live_... here",
        "pypi- tokens are scoped",
        "gsk_ is the Groq prefix",
        # Too short to be a credential.
        _token("sk", "_live_", "abc123"),
        _token("hf", "_", "abc123"),
    ],
)
def test_prefix_lookalikes_are_not_redacted(text: str) -> None:
    assert redact(text) == text


def test_existing_placeholders_are_stable_when_redaction_is_reapplied() -> None:
    token = VENDOR_TOKENS["stripe_secret_live"]
    once = redact(f"key {token}")

    assert redact(once) == once


def test_vendor_token_scan_is_linear_on_adversarial_input() -> None:
    probes = [
        _token("gl", "pat-", "a" * 20) + ".a" * 20_000 + ".",
        _token("xo", "xb-") + "-" * 40_000 + "!",
        ("hf_" + "a" * 29 + " ") * 2_000,
    ]
    for probe in probes:
        start = time.perf_counter()
        redact(probe)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"redact() took {elapsed:.3f}s on a {len(probe)}-char probe"


def test_vendor_tokens_are_redacted_in_nested_structures() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": VENDOR_TOKENS["hugging_face"]}]},
            {"role": "tool", "content": json.dumps({"out": VENDOR_TOKENS["stripe_secret_live"]})},
        ],
        "metadata": {"note": f"deploy with {VENDOR_TOKENS['gitlab_pat']}"},
    }

    encoded = json.dumps(redact_structure(payload))

    for name in ("hugging_face", "stripe_secret_live", "gitlab_pat"):
        for fragment in _fragments(VENDOR_TOKENS[name]):
            assert fragment not in encoded


def test_vendor_token_canaries_are_absent_from_storage_bundle_and_http_payload(
    tmp_path,
    caplog,
) -> None:
    from fastapi.testclient import TestClient
    from verdict.dashboard import create_app
    from verdict.schema import Trace
    from verdict.storage.sqlite import SQLiteStorage

    from ui.server import build_bundle

    stripe = VENDOR_TOKENS["stripe_secret_live"]
    slack = VENDOR_TOKENS["slack_user"]
    hugging_face = VENDOR_TOKENS["hugging_face"]
    npm = VENDOR_TOKENS["npm"]
    canaries = (stripe, slack, hugging_face, npm)

    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="anthropic",
        request_model="claude-haiku-4-5",
        prompt_redacted=f"Why does {stripe} fail?",
        response_redacted=f"Rotate {slack} first.",
        error=f"upstream rejected {hugging_face}",
        raw_messages=[{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "content": json.dumps({"stdout": f"NPM_TOKEN ok {npm}"}),
            }],
            "metadata": {"hint": f"stripe {stripe}"},
        }],
        tags={"note": f"slack {slack}"},
    )
    storage.insert_trace(trace)
    stored = storage.get_trace(trace.trace_id)
    storage.close()

    assert stored is not None
    stored_repr = repr(stored)
    serialized_bundle = json.dumps(build_bundle(path), sort_keys=True, default=str)
    response = TestClient(create_app(storage=str(path))).get("/api/data")
    assert response.status_code == 200
    for token in canaries:
        for fragment in _fragments(token):
            assert fragment not in stored_repr, f"{fragment!r} survived into storage"
            assert fragment not in serialized_bundle, f"{fragment!r} reached the bundle"
            assert fragment not in response.text, f"{fragment!r} reached the HTTP payload"
            assert fragment not in caplog.text


def test_vendor_tokens_in_agent_tool_evidence_are_redacted_before_storage() -> None:
    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    stripe = VENDOR_TOKENS["stripe_restricted"]
    gitlab = VENDOR_TOKENS["gitlab_routable_pat"]
    try:
        with verdict.agent_run(name="agent") as run:
            with run.turn(user_input=f"deploy using {gitlab}") as turn:
                with turn.tool("charge", arguments={"note": f"use {stripe}"}) as tool:
                    tool.set_output({"stdout": f"authenticated as {gitlab}"})
                turn.set_output(f"done with {stripe}")
        [bundle] = storage.list_agent_run_bundles("tenant-a")
    finally:
        verdict.shutdown()

    encoded = repr(bundle)
    for token in (stripe, gitlab):
        for fragment in _fragments(token):
            assert fragment not in encoded
