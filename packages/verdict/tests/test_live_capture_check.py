from __future__ import annotations

from verdict.client import shutdown
from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage

from scripts import live_capture_check


def _passes(_storage, do_streaming: bool, model: str):
    assert do_streaming is True
    assert model
    return [], [
        "messages.create",
        "messages.create(stream=True)",
        "messages.stream().text_stream",
        "messages.stream(error)",
    ]


def _sdk_missing(_storage, do_streaming: bool, model: str) -> list[str]:
    raise ImportError("deliberately unavailable")


def _passes_nonstream(_storage, do_streaming: bool, model: str):
    assert do_streaming is False
    assert model
    return [], ["messages.create", "messages.create(error)"]


def test_requested_provider_skip_makes_live_gate_incomplete(capsys):
    shutdown()
    try:
        result = live_capture_check.main(
            ["--providers", "anthropic,openai"],
            checks={"anthropic": _passes, "openai": _sdk_missing},
        )
    finally:
        shutdown()

    output = capsys.readouterr().out
    assert result == 2
    assert "Verified requested providers: anthropic" in output
    assert "Unverified requested providers: openai" in output
    assert "LIVE CAPTURE CHECK PASSED" not in output


def test_explicitly_narrowed_live_gate_names_verified_provider(capsys):
    shutdown()
    try:
        result = live_capture_check.main(
            ["--providers", "anthropic"],
            checks={"anthropic": _passes},
        )
    finally:
        shutdown()

    output = capsys.readouterr().out
    assert result == 0
    assert "LIVE CAPTURE CHECK PASSED for: anthropic" in output
    assert "Verified entry points (anthropic): messages.create" in output
    assert "messages.create(stream=True)" in output
    assert "messages.stream().text_stream" in output
    assert "messages.stream(error)" in output
    assert "Unverified requested providers" not in output


def test_empty_provider_scope_cannot_make_a_hollow_live_pass(capsys):
    result = live_capture_check.main(["--providers", ""], checks={})

    output = capsys.readouterr().out
    assert result == 2
    assert "request at least one provider" in output
    assert "LIVE CAPTURE CHECK PASSED" not in output


def test_explicit_nonstream_scope_names_only_exercised_entry_points(capsys):
    shutdown()
    try:
        result = live_capture_check.main(
            ["--providers", "anthropic", "--no-streaming"],
            checks={"anthropic": _passes_nonstream},
        )
    finally:
        shutdown()

    output = capsys.readouterr().out
    assert result == 0
    assert "Verified entry points (anthropic): messages.create, messages.create(error)" in output
    assert "messages.create(stream=True)" not in output
    assert "messages.stream().text_stream" not in output


def test_new_trace_check_cannot_reuse_a_stale_provider_trace():
    storage = InMemoryStorage()
    storage.insert_trace(Trace(provider="anthropic"))
    before = len(storage.list_traces(limit=1000))

    trace, failures = live_capture_check._new_trace(
        storage,
        before,
        label="messages.stream",
    )
    assert trace is None
    assert failures == ["messages.stream: expected exactly one new trace, captured 0"]

    storage.insert_trace(Trace(provider="anthropic"))
    trace, failures = live_capture_check._new_trace(
        storage,
        before,
        label="messages.stream",
    )
    assert trace is not None
    assert failures == []
