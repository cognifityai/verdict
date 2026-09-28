import json
from dataclasses import replace

import pytest
from verdict.storage.sqlite import SQLiteStorage
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import import_into_storage
from verdict.telemetry.sources.voice import map_voice_conversation


def voice_record(**updates):
    return {
        "conversation_id": "session-1",
        "timestamp": "2026-09-01T12:00:00Z",
        "end_status": "complete",
        "language": "ur",
        "workflow": "support",
        "turns": [
            {"speaker": "user", "text": "Synthetic parcel has not arrived."},
            {
                "speaker": "assistant",
                "text": "When was it expected?",
                "timestamp": "2026-09-01T12:00:01Z",
            },
            {"speaker": "user", "text": "یہ مصنوعی مثال ہے"},
            {"speaker": "assistant", "text": "Is there", "status": "interrupted"},
        ],
        **updates,
    }


def test_voice_retains_trailing_user_and_interrupted_assistant():
    results = map_voice_conversation(voice_record(), ImportContext("voice", "feed", "alpha"))
    snapshots = [r.session for r in results if getattr(r, "session", None) is not None]
    assert len(snapshots) == 1, "voice import must preserve a native session snapshot"
    assert [m["content"] for m in snapshots[0]["messages"]][-2:] == ["یہ مصنوعی مثال ہے", "Is there"]
    assert snapshots[0]["messages"][-1]["status"] == "interrupted"
    assert len([r.trace for r in results if r.trace is not None]) == 1


def test_untimed_session_persists_without_inventing_trace_or_event_time(tmp_path):
    record = voice_record(
        timestamp=None,
        turns=[
            {"speaker": "user", "text": "Synthetic parcel is late."},
            {"speaker": "assistant", "text": "When was it expected?"},
        ],
    )
    store = SQLiteStorage(str(tmp_path / "v.db"))
    summary = import_into_storage(
        map_voice_conversation(record, ImportContext("voice", "feed", "alpha")), store
    )
    assert summary.stored == 0
    assert summary.sessions_stored == 1
    [session] = store.list_sessions("alpha")
    assert session["event_at"] is None
    assert store.list_traces(tenant_id="alpha") == []
    store.close()


def test_reimport_revision_and_tenant_isolation(tmp_path):
    store = SQLiteStorage(str(tmp_path / "v.db"))
    for tenant in ["alpha", "alpha", "beta"]:
        import_into_storage(
            map_voice_conversation(voice_record(), ImportContext("voice", "feed", tenant)), store
        )
    [original] = store.list_sessions("alpha")
    assert len(store.list_sessions("beta")) == 1
    assert store.list_sessions("beta")[0]["id"] != original["id"]
    updated = voice_record()
    updated["turns"].append({"speaker": "user", "text": "Additional synthetic detail."})
    import_into_storage(
        map_voice_conversation(updated, ImportContext("voice", "feed", "alpha")), store
    )
    [current] = store.list_sessions("alpha")
    assert current["id"] == original["id"]
    assert current["revision"] != original["revision"]
    assert store.get_session("alpha", original["id"], original["revision"]) == original
    assert store.get_session("beta", original["id"]) is None
    store.close()


def test_conversation_monitor_excludes_unknown_closure_and_missing_time():
    from verdict.monitoring import MonitorPolicy
    from verdict.sessions import session_monitor_units

    context = ImportContext("voice", "feed", "alpha")
    sessions = []
    for row in [voice_record(), voice_record(end_status="unknown"), voice_record(timestamp=None)]:
        results = map_voice_conversation(row, context)
        sessions.append(next(r.session for r in results if getattr(r, "session", None)))
    policy = MonitorPolicy("p", "alpha:application:conversation", analysis_unit="conversation")
    units, coverage = session_monitor_units(sessions, [], policy)
    assert len(units) == 1
    assert coverage["unknownClosure"] == 1
    assert coverage["missingEventTime"] == 1
    assert units[0].metrics["session.completed"] is True


def test_voice_retains_system_instruction_in_snapshot_and_reply_context():
    results = map_voice_conversation(
        {
            "conversation_id": "system-context",
            "turns": [
                {"role": "system", "content": "Synthetic support workflow only."},
                {"role": "user", "content": "Synthetic order question."},
                {
                    "role": "assistant",
                    "content": "Tell me more.",
                    "timestamp": "2026-09-01T01:00:00Z",
                },
            ],
        },
        ImportContext(adapter="voice", source_scope="test", tenant_id="alpha"),
    )
    row = next(r.session for r in results if r.session)
    assert row["messages"][0]["role"] == "system"
    from verdict.trace_facts import trace_conversation_history

    trace = next(r.trace for r in results if r.trace)
    assert "Synthetic support workflow only." in trace_conversation_history(trace)


def test_corrected_voice_reply_preserves_original_trace_and_current_snapshot(tmp_path):
    from copy import deepcopy

    from verdict.trace_facts import trace_conversation_history

    storage = SQLiteStorage(str(tmp_path / "revisions.db"))
    context = ImportContext("voice", "stable-feed", "alpha")
    original = voice_record()
    first = map_voice_conversation(original, context)
    trace_id = next(r.trace.trace_id for r in first if r.trace)
    import_into_storage(first, storage)
    old_trace = storage.get_trace(trace_id)
    old_snapshot = storage.list_sessions("alpha")[0]
    revised = deepcopy(original)
    turns = revised.get("turns", revised.get("messages"))
    assistant = next(t for t in turns if t.get("speaker", t.get("role")) == "assistant")
    assistant["text"] = "Corrected synthetic reply."
    summary = import_into_storage(map_voice_conversation(revised, context), storage)
    assert summary.skip_reasons["voice_reply_revision_retained"] == 1
    kept = storage.get_trace(trace_id)
    assert kept.response_redacted == old_trace.response_redacted
    assert trace_conversation_history(kept) == trace_conversation_history(old_trace)
    current = storage.list_sessions("alpha")[0]
    assert current["revision"] != old_snapshot["revision"]
    assert any(m["content"] == "Corrected synthetic reply." for m in current["messages"])
    assert len(storage.list_traces(tenant_id="alpha")) == 1
    import_into_storage(map_voice_conversation(original, context), storage)
    assert storage.list_sessions("alpha")[0]["revision"] == current["revision"]
    storage.close()


@pytest.mark.parametrize("prefix", [
    [],
    [{"role": "system", "content": "Synthetic workflow."}],
    [{"role": "assistant", "content": "Earlier question.", "status": "interrupted"}],
    [{"role": "user", "content": "یہ مصنوعی مثال ہے"}],
    [{"role": "user", "content": "First detail."}, {"role": "user", "content": "Second detail."}],
    [{"role": "user", "content": "Contact synthetic@example.test."}],
    [{"role": "user", "content": "Bearer"}, {"role": "user", "content": "abcdefghijklmnopqrstuvwx"}],
    [{"role": "user", "content": "a" * 50_000}, {"role": "user", "content": "b" * 49_999}],
    [{"role": "user", "content": "a" * 50_000}, {"role": "user", "content": "b" * 50_000}],
    [{"role": "user", "content": "a" * 60_000}, {"role": "user", "content": "b" * 60_000}],
])
def test_voice_history_checks_the_published_prompt_projection(tmp_path, prefix):
    from verdict.trace_facts import trace_conversation_history

    record = {"conversation_id": "projection", "turns": [*prefix, {
        "role": "assistant", "content": "Synthetic answer.",
        "timestamp": "2026-09-01T12:00:01Z",
    }]}
    storage = SQLiteStorage(str(tmp_path / "projection.db"))
    try:
        results = map_voice_conversation(record, ImportContext("voice", "feed", "alpha"))
        import_into_storage(results, storage)
        [trace] = storage.list_traces(tenant_id="alpha")
        history = trace_conversation_history(trace)
        assert (json.loads(history) if history else []) == trace.raw_messages[:-1]
        if prefix and prefix[0]["content"] == "Bearer":
            assert trace.prompt_redacted == "<BEARER_TOKEN>"
        if prefix and len(prefix[0]["content"]) >= 50_000:
            assert len(trace.prompt_redacted) == 100_000
        for correction in [
            {"prompt_redacted": "Corrected user detail."},
            {"response_redacted": "Corrected answer."},
            {"prompt_redacted": "Corrected user detail.", "response_redacted": "Corrected answer."},
        ]:
            with pytest.raises(ValueError, match="incoherent imported voice"):
                trace_conversation_history(replace(trace, **correction))
    finally:
        storage.close()


@pytest.mark.parametrize("messages", [None, [], [None], [{"role": "user", "content": "Only user."}],
                                      [None, {"role": "assistant", "content": "Answer."}]])
def test_voice_history_rejects_missing_or_malformed_target(messages):
    from verdict.schema import Trace
    from verdict.trace_facts import trace_conversation_history

    trace = Trace(raw_messages=messages, prompt_redacted="User detail.",
                  response_redacted="Answer.", tags={"verdict.source": "voice"})
    with pytest.raises(ValueError, match="incoherent imported voice"):
        trace_conversation_history(trace)


@pytest.mark.parametrize("source", [None, "langfuse", "otlp"])
def test_other_sources_keep_missing_history_fallback(source):
    from verdict.schema import Trace
    from verdict.trace_facts import trace_conversation_history

    trace = Trace(raw_messages=None, tags={"verdict.source": source} if source else {})
    assert trace_conversation_history(trace) is None


def test_published_prefix_only_voice_upsert_is_rejected_by_context_consumers(tmp_path):
    from copy import deepcopy

    from verdict.dashboard.evaluator_lab import preview_evaluation
    from verdict.matched_comparison import compare_saved_replies
    from verdict.schema import Judgment
    from verdict.trace_facts import trace_conversation_history

    storage = SQLiteStorage(str(tmp_path / "legacy-voice.db"))
    try:
        context = ImportContext("voice", "feed", "alpha")
        original = voice_record()
        first = next(r.trace for r in map_voice_conversation(original, context) if r.trace)
        storage.insert_trace(first)
        revised = deepcopy(original)
        revised["turns"][0]["text"] = "Corrected synthetic user detail."
        second = next(r.trace for r in map_voice_conversation(revised, context) if r.trace)
        storage.insert_trace(second)  # Published generic UPSERT preserves raw messages.
        stored = storage.get_trace(first.trace_id)
        assert stored.prompt_redacted == second.prompt_redacted
        assert stored.raw_messages == first.raw_messages
        assert stored.response_redacted == first.response_redacted
        with pytest.raises(ValueError, match="incoherent imported voice"):
            trace_conversation_history(stored)
        config = {"provider": "anthropic", "model": "synthetic-judge", "maxCalls": 1,
                  "rubric": {"name": "quality", "version": "1", "dimensions": [
                      {"name": "quality", "description": "Responds to captured context."}]}}
        with pytest.raises(ValueError, match="incoherent imported voice"):
            preview_evaluation(storage, tenant_id="alpha", config=config)
        judgment = Judgment(trace_id=stored.trace_id, evaluator_provider="anthropic",
                            evaluator_fingerprint="a" * 64, judge_models=["synthetic-judge"],
                            expected_dimensions=["quality"])
        with pytest.raises(ValueError, match="incoherent imported voice"):
            compare_saved_replies([stored], [judgment], "voice-agent/voice-agent", "other/model")
    finally:
        storage.close()


@pytest.mark.parametrize("canary", [
    "4111111111111111", "123-45-6789", "192.0.2.10",
    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwx",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwx",
])
def test_sensitive_identifiers_are_rejected_before_session_and_rubric_api(tmp_path, canary):
    from fastapi.testclient import TestClient
    from verdict.dashboard.app import create_app
    from verdict.redaction import redact
    from verdict.sessions import key

    assert redact(canary) != canary
    with pytest.raises(ValueError, match="invalid bounded identifier"):
        key(canary)
    path = tmp_path / "identifier-privacy.db"
    storage = SQLiteStorage(str(path))
    try:
        context = ImportContext("voice", "feed", "alpha")
        safe = map_voice_conversation(voice_record(), context)
        import_into_storage(safe, storage)
        [snapshot] = storage.list_sessions("alpha")
        with pytest.raises(ValueError, match="invalid bounded identifier"):
            storage.save_session({**snapshot, "id": canary, "revision": None})
        summary = import_into_storage(map_voice_conversation(voice_record(workflow=canary), context), storage)
        assert summary.sessions_stored == 1
        assert canary not in json.dumps(storage.list_sessions("alpha"))
    finally:
        storage.close()
    with TestClient(create_app(storage=f"sqlite:///{path}", tenant_id="alpha")) as client:
        listing = client.get("/api/data/sessions")
        detail = client.get(f"/api/data/sessions/{snapshot['id']}")
        assert listing.status_code == detail.status_code == 200
        assert canary not in listing.text + detail.text
        token = client.get("/api/setup/token").json()["setupToken"]
        document = {"name": "custom_quality", "version": "1", "target": "conversation",
                    "dimensions": [{"name": canary, "description": "Synthetic criterion."}]}
        result = client.post("/api/evaluators/rubric/validate", json={"document": document},
                             headers={"X-Verdict-Setup": token})
        assert result.status_code == 400
        assert canary not in result.text
        document["dimensions"][0]["name"] = "unfamiliar/custom-quality.v9"
        assert client.post("/api/evaluators/rubric/validate", json={"document": document},
                           headers={"X-Verdict-Setup": token}).status_code == 200


@pytest.mark.parametrize("identifier", ["custom_phase_v9", "vendor/model-v17", "quality.v9", "a" * 64])
def test_benign_custom_identifiers_remain_exact(identifier):
    from verdict.sessions import key

    assert key(identifier) == identifier


def test_generated_numeric_scope_uses_an_opaque_native_prefix(tmp_path):
    from verdict.redaction import redact

    context = ImportContext("voice", "synthetic-feed-1737", "alpha")
    assert context.scope_digest == "8710947061076952"
    assert redact(context.scope_digest) != context.scope_digest
    results = map_voice_conversation(voice_record(), context)
    row = next(r.session for r in results if r.session)
    assert row["source_scope"] == f"scope_{context.scope_digest}"
    trace = next(r.trace for r in results if r.trace)
    assert trace.trace_id == context.trace_id("session-1:assistant:1", "session-1")
    storage = SQLiteStorage(str(tmp_path / "numeric-scope.db"))
    try:
        import_into_storage(results, storage)
        assert storage.list_sessions("alpha")[0]["source_scope"] == row["source_scope"]
    finally:
        storage.close()
