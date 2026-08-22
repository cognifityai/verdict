"""Live capture check — verify the instrumentors capture REAL provider traffic.

Capture has deterministic tests through real provider SDKs and local HTTP
transports. This script adds the evidence those tests cannot: tiny calls to the
real provider services, confirming that current wire responses still produce
complete stored traces.

It is INHERENTLY a thing you run, not something that runs in CI: it needs real
API keys, network, and the provider SDKs installed. Nothing here is mocked.

Usage (set keys for every requested provider; a skip makes the gate nonzero):
    export ANTHROPIC_API_KEY=...      # and/or OPENAI_API_KEY / GOOGLE_API_KEY
    python scripts/live_capture_check.py
    python scripts/live_capture_check.py --providers anthropic,openai
    python scripts/live_capture_check.py --no-streaming   # explicitly narrow scope
    python scripts/live_capture_check.py --anthropic-model claude-haiku-4-5

Cost: a handful of tiny (max ~32 token) calls per provider. Cents at most.

What it asserts for each captured trace:
    - a trace was persisted to storage (the monkeypatch fired)
    - input/output token counts are present and > 0
    - finish_reason is set and normalized (lowercase, no enum prefix)
    - cost_usd is computed (> 0 for a known-priced model)
    - for streaming calls: the same, proving the streaming wrapper captured
    - an intentionally-failing call records a trace WITH an error (not a
      silent success) — the bug we fixed for the Anthropic stream path

The final summary names every provider and entry point that passed, so a saved
artifact identifies the exact live surface exercised.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _check_trace(
    t,
    *,
    label: str,
    expect_error: bool,
    expect_cost: bool,
    expect_stream_completion: str | None = None,
) -> list[str]:
    """Return a list of failure strings (empty = all good)."""
    fails: list[str] = []
    if t is None:
        return [f"{label}: NO trace was captured (monkeypatch did not fire)"]
    if expect_stream_completion is not None:
        completion = (t.tags or {}).get("verdict.stream_completion")
        if completion != expect_stream_completion:
            fails.append(
                f"{label}: stream completion is {completion!r}, "
                f"expected {expect_stream_completion!r}"
            )
    if expect_error:
        if not t.error:
            fails.append(
                f"{label}: expected an error trace, but error is empty "
                "(a failed call was recorded as success)"
            )
        return fails  # don't demand tokens/cost on an errored call
    if not t.input_tokens or t.input_tokens <= 0:
        fails.append(f"{label}: input_tokens missing/zero ({t.input_tokens})")
    if not t.output_tokens or t.output_tokens <= 0:
        fails.append(f"{label}: output_tokens missing/zero ({t.output_tokens})")
    if not t.finish_reason:
        fails.append(f"{label}: finish_reason missing")
    elif t.finish_reason != t.finish_reason.lower() or "." in t.finish_reason:
        fails.append(f"{label}: finish_reason not normalized ({t.finish_reason!r})")
    if expect_cost and (t.cost_usd is None or t.cost_usd <= 0):
        fails.append(f"{label}: cost_usd not computed ({t.cost_usd})")
    return fails


def _new_trace(storage, before: int, *, label: str):
    """Return the one trace added by an entry point, or an exact count failure."""
    traces = storage.list_traces(limit=1000)
    added = len(traces) - before
    if added != 1:
        return None, [f"{label}: expected exactly one new trace, captured {added}"]
    return traces[0], []


def _verify_new_trace(
    storage,
    before: int,
    *,
    label: str,
    expect_error: bool,
    expect_cost: bool,
    expect_stream_completion: str | None = None,
) -> list[str]:
    trace, failures = _new_trace(storage, before, label=label)
    return failures + _check_trace(
        trace,
        label=label,
        expect_error=expect_error,
        expect_cost=expect_cost,
        expect_stream_completion=expect_stream_completion,
    )


def check_anthropic(
    storage,
    do_streaming: bool,
    model: str,
) -> tuple[list[str], list[str]]:
    import anthropic

    fails: list[str] = []
    verified: list[str] = []
    client = anthropic.Anthropic()

    # 1. non-streaming
    before = len(storage.list_traces(limit=1000))
    client.messages.create(
        model=model, max_tokens=16, messages=[{"role": "user", "content": "Say 'ok'."}]
    )
    entry_fails = _verify_new_trace(
        storage,
        before,
        label="anthropic messages.create",
        expect_error=False,
        expect_cost=True,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append("messages.create")

    # 2. Raw streaming response.
    if do_streaming:
        before = len(storage.list_traces(limit=1000))
        stream = client.messages.create(
            model=model,
            max_tokens=16,
            stream=True,
            messages=[{"role": "user", "content": "Count to 3."}],
        )
        for _ in stream:
            pass
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="anthropic messages.create(stream=True)",
            expect_error=False,
            expect_cost=True,
            expect_stream_completion="complete",
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("messages.create(stream=True)")

        # 3. Anthropic's documented accumulating stream helper. Its text lens
        # must run through the same telemetry lifecycle as direct event iteration.
        before = len(storage.list_traces(limit=1000))
        with client.messages.stream(
            model=model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Say 'stream ok'."}],
        ) as helper_stream:
            for _ in helper_stream.text_stream:
                pass
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="anthropic messages.stream().text_stream",
            expect_error=False,
            expect_cost=True,
            expect_stream_completion="complete",
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("messages.stream().text_stream")

    # 4. The selected error surface must record an error, never success.
    bad_model = "claude-does-not-exist-xyz"
    before = len(storage.list_traces(limit=1000))
    try:
        if do_streaming:
            with client.messages.stream(
                model=bad_model,
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            ) as error_stream:
                error_stream.until_done()
        else:
            client.messages.create(
                model=bad_model,
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            )
    except Exception:
        pass
    error_entry = "messages.stream(error)" if do_streaming else "messages.create(error)"
    entry_fails = _verify_new_trace(
        storage,
        before,
        label=f"anthropic {error_entry}",
        expect_error=True,
        expect_cost=False,
        expect_stream_completion="error" if do_streaming else None,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append(error_entry)
    return fails, verified


def check_openai(
    storage,
    do_streaming: bool,
    model: str,
) -> tuple[list[str], list[str]]:
    import openai

    fails: list[str] = []
    verified: list[str] = []
    client = openai.OpenAI()

    before = len(storage.list_traces(limit=1000))
    client.chat.completions.create(
        model=model, max_tokens=16, messages=[{"role": "user", "content": "Say 'ok'."}]
    )
    entry_fails = _verify_new_trace(
        storage,
        before,
        label="openai chat.create",
        expect_error=False,
        expect_cost=True,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append("chat.completions.create")

    if do_streaming:
        before = len(storage.list_traces(limit=1000))
        stream = client.chat.completions.create(
            model=model,
            max_tokens=16,
            stream=True,
            stream_options={"include_usage": True},
            messages=[{"role": "user", "content": "Count to 3."}],
        )
        for _ in stream:
            pass
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="openai chat stream",
            expect_error=False,
            expect_cost=True,
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("chat.completions.create(stream=True)")

    if not hasattr(client, "responses"):
        fails.append(
            "openai Responses API is unavailable in the installed SDK; "
            "install a current openai release"
        )
        return fails, verified

    before = len(storage.list_traces(limit=1000))
    response = client.responses.create(
        model=model,
        max_output_tokens=16,
        input="Say 'responses ok'.",
    )
    entry_fails = _verify_new_trace(
        storage,
        before,
        label="openai responses.create",
        expect_error=False,
        expect_cost=True,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append("responses.create")

    before = len(storage.list_traces(limit=1000))
    client.responses.parse(
        model=model,
        max_output_tokens=16,
        input="Say 'parsed ok'.",
    )
    entry_fails = _verify_new_trace(
        storage,
        before,
        label="openai responses.parse",
        expect_error=False,
        expect_cost=True,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append("responses.parse")

    if do_streaming:
        before = len(storage.list_traces(limit=1000))
        stream = client.responses.create(
            model=model,
            max_output_tokens=16,
            input="Count to 3.",
            stream=True,
        )
        for _ in stream:
            pass
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="openai responses.create(stream=True)",
            expect_error=False,
            expect_cost=True,
            expect_stream_completion="complete",
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("responses.create(stream=True)")

        before = len(storage.list_traces(limit=1000))
        with client.responses.stream(
            model=model,
            max_output_tokens=16,
            input="Say 'helper ok'.",
        ) as helper_stream:
            helper_stream.until_done()
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="openai responses.stream(new response)",
            expect_error=False,
            expect_cost=True,
            expect_stream_completion="complete",
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("responses.stream(new response)")

        before = len(storage.list_traces(limit=1000))
        with client.responses.stream(response_id=response.id) as helper_stream:
            helper_stream.until_done()
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="openai responses.stream(existing response)",
            expect_error=False,
            expect_cost=True,
            expect_stream_completion="complete",
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("responses.stream(existing response)")

    bad_model = "openai-model-does-not-exist-xyz"
    before = len(storage.list_traces(limit=1000))
    try:
        if do_streaming:
            with client.responses.stream(
                model=bad_model,
                max_output_tokens=8,
                input="hi",
            ) as error_stream:
                error_stream.until_done()
        else:
            client.responses.create(
                model=bad_model,
                max_output_tokens=8,
                input="hi",
            )
    except Exception:
        pass
    error_entry = "responses.stream(error)" if do_streaming else "responses.create(error)"
    entry_fails = _verify_new_trace(
        storage,
        before,
        label=f"openai {error_entry}",
        expect_error=True,
        expect_cost=False,
        expect_stream_completion="error" if do_streaming else None,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append(error_entry)
    return fails, verified


def check_google(
    storage,
    do_streaming: bool,
    model: str,
) -> tuple[list[str], list[str]]:
    from google import genai

    fails: list[str] = []
    verified: list[str] = []
    client = genai.Client()

    before = len(storage.list_traces(limit=1000))
    client.models.generate_content(model=model, contents="Say 'ok'.")
    entry_fails = _verify_new_trace(
        storage,
        before,
        label="google generate_content",
        expect_error=False,
        expect_cost=True,
    )
    fails += entry_fails
    if not entry_fails:
        verified.append("models.generate_content")

    if do_streaming:
        # The instrumentor DOES wrap the modern-SDK streaming method
        # (Models.generate_content_stream). This live check keeps that path
        # honest as provider SDK stream chunk shapes evolve.
        before = len(storage.list_traces(limit=1000))
        for _ in client.models.generate_content_stream(model=model, contents="Count to 3."):
            pass
        entry_fails = _verify_new_trace(
            storage,
            before,
            label="google generate_content_stream",
            expect_error=False,
            expect_cost=True,
        )
        fails += entry_fails
        if not entry_fails:
            verified.append("models.generate_content_stream")
    return fails, verified


CHECKS = {"anthropic": check_anthropic, "openai": check_openai, "google": check_google}


def main(argv: list[str] | None = None, *, checks=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--providers",
        default="anthropic,openai,google",
        help="Comma list of required providers; every named provider must run.",
    )
    p.add_argument(
        "--no-streaming",
        action="store_true",
        help="Explicitly narrow the gate to non-streaming calls.",
    )
    p.add_argument(
        "--anthropic-model",
        default=os.environ.get("VERDICT_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        help="Anthropic model for live calls. Env fallback: VERDICT_LIVE_ANTHROPIC_MODEL.",
    )
    p.add_argument(
        "--openai-model",
        default=os.environ.get("VERDICT_LIVE_OPENAI_MODEL", "gpt-4o-mini"),
        help="OpenAI model for live calls. Env fallback: VERDICT_LIVE_OPENAI_MODEL.",
    )
    p.add_argument(
        "--google-model",
        default=os.environ.get("VERDICT_LIVE_GOOGLE_MODEL", "gemini-2.5-flash"),
        help="Google model for live calls. Env fallback: VERDICT_LIVE_GOOGLE_MODEL.",
    )
    args = p.parse_args(argv)
    requested = [x.strip() for x in args.providers.split(",") if x.strip()]
    if not requested:
        print("LIVE CAPTURE CHECK INCOMPLETE — request at least one provider.")
        return 2

    import verdict
    from verdict.client import get_client
    from verdict.storage.memory import InMemoryStorage

    # In-memory storage (no leftover DB file); a fresh one per provider.
    verdict.init(capture_content=True, storage=InMemoryStorage())

    models = {
        "anthropic": args.anthropic_model,
        "openai": args.openai_model,
        "google": args.google_model,
    }
    selected_checks = CHECKS if checks is None else checks
    all_fails: list[str] = []
    verified: list[str] = []
    verified_entries: dict[str, list[str]] = {}
    unverified: dict[str, str] = {}

    for name in requested:
        check = selected_checks.get(name)
        if check is None:
            print(f"  ? unknown requested provider {name!r}")
            unverified[name] = "unknown provider"
            continue
        store = InMemoryStorage()
        get_client().storage = store
        try:
            model = models[name]
            print(f"\n=== {name} ({model}) ===")
            fails, entries = check(
                store,
                do_streaming=not args.no_streaming,
                model=model,
            )
        except ImportError as e:
            print(f"  - {name}: SDK not installed ({type(e).__name__}); unverified")
            unverified[name] = type(e).__name__
            continue
        except Exception as e:
            # Auth/network failures are evidence gaps, never a passing gate.
            print(f"  - {name}: live calls did not complete ({type(e).__name__}); unverified")
            unverified[name] = type(e).__name__
            continue
        if fails:
            for f in fails:
                print(f"  {FAIL} {f}")
            all_fails += fails
        else:
            verified.append(name)
            verified_entries[name] = entries
            print(f"  {PASS} verified entry points: {', '.join(entries)}")

    print("\n" + "=" * 60)
    print("Verified requested providers: " + (", ".join(verified) or "none"))
    for name in verified:
        print(f"Verified entry points ({name}): " + ", ".join(verified_entries[name]))
    if unverified:
        print("Unverified requested providers: " + ", ".join(sorted(unverified)))
    if all_fails:
        print(f"LIVE CAPTURE CHECK FAILED ({len(all_fails)} issue(s)).")
        return 1
    if unverified:
        print("LIVE CAPTURE CHECK INCOMPLETE — every requested provider must run.")
        return 2
    print("LIVE CAPTURE CHECK PASSED for: " + ", ".join(verified))
    return 0


if __name__ == "__main__":
    sys.exit(main())
