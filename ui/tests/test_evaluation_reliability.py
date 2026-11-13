"""Synthetic scoped reliability workflows through actual storage and API."""

import asyncio
import builtins
import copy
import json
import os
from dataclasses import replace
from datetime import timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from test_scoped_evaluation_server import _bundle, _config
from verdict.dashboard.app import build_agent_runs_bundle, build_agent_session_bundle, create_app
from verdict.dashboard.control_plane import ControlStore
from verdict.dashboard.evaluator_lab import _IdentityOnlyProvider
from verdict.dashboard.scoped_evaluation import (
    execute_scoped,
    preview_scoped,
    run_scheduled_once,
    schedule_scoped_once,
)
from verdict.evidence import AgentEvent, AgentEventType, ExecutionStatus, PrivacyClassification
from verdict.storage import SQLiteStorage
from verdict_eval.providers import CompletionResponse


@pytest.mark.parametrize("role", ["summary", "count", "reference", "missing"])
def test_known_nonanswer_is_structurally_complete_but_unsuitable(tmp_path, role):
    bundle = _bundle()
    provenance = AgentEvent(
        "capture", "turn-a", 3, bundle.turns[0].started_at,
        AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic",
        {"name": "evaluation_capture", "value": {"response": role}}, PrivacyClassification.REDACTED,
    )
    storage = SQLiteStorage(str(tmp_path / "capture.db"))
    try:
        storage.replace_agent_run_bundle(replace(bundle, events=(*bundle.events, provenance)))
        preview = preview_scoped(storage, tenant="local", config=_config())
        assert preview["coverage"] == "complete"
        assert preview["plannedCalls"] == 0
        assert preview["suitability"]["state"] == "unavailable"
    finally:
        storage.close()


def test_unknown_legacy_provenance_does_not_disable_evaluation(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "legacy.db"))
    try:
        storage.replace_agent_run_bundle(_bundle())
        preview = preview_scoped(storage, tenant="local", config=_config())
        assert preview["plannedCalls"] == 1
        assert preview["suitability"]["turns"][0]["response"] == "unknown"
    finally:
        storage.close()


def history_bundle(responses=("A hexagon has five sides.", "A hexagon has six sides."), tenant="local"):
    """Independent synthetic input, unrelated to any captured conversation."""
    base = _bundle(tenant)
    start = base.turns[0].started_at
    turns = tuple(replace(
        base.turns[0], turn_id=f"turn-{i}", sequence=i,
        started_at=start + timedelta(minutes=i * 2),
        ended_at=start + timedelta(minutes=i * 2 + 1),
        user_request_redacted="How many sides does a hexagon have? Answer one sentence.",
        final_response_redacted=response,
    ) for i, response in enumerate(responses))
    return replace(base, turns=turns, events=(),
                   run=replace(base.run, ended_at=turns[-1].ended_at))


def audit_config(aggregation="historical_all_turns", authority="public_knowledge"):
    return {**_config(), "evaluationMode": "historical_audit", "maxOutputTokens": 4096,
            "rubric": {"name": "synthetic_history", "version": "1", "dimensions": [{
                "name": "geometry_check", "description": "No material false geometric claims.",
                "aggregation": aggregation, "evidenceAuthority": authority,
                "requiredEvidence": ["request", "response"],
            }]}}


def audit_reply(evidence, labels=("FAIL", "PASS"), recoveries=None, holistic=None):
    """Serialize independently assigned provider labels; no evaluation logic."""
    rows = []
    pairs = evidence["turnPairs"]
    recoveries = recoveries or {}
    for index, (pair, label) in enumerate(zip(pairs, labels, strict=True)):
        citation = {"reference": pair["responseReference"], "quote": pair["response"]}
        assessment = {"verdict": label, "reason": "Synthetic assigned assessment.", "citations": [citation]}
        findings = []
        if label == "FAIL":
            target = recoveries.get(index)
            recovery = {"state": "unresolved", "turnReference": None,
                        "reason": "This specific finding remains unresolved.", "citations": []}
            if target is not None:
                later = pairs[target]
                recovery = {"state": "recovered", "turnReference": later["reference"],
                            "reason": "The specific claim is corrected in this later answer.",
                            "citations": [{"reference": later["responseReference"], "quote": later["response"]}]}
            findings = [{"reason": "A material geometric claim is false.",
                         "citations": [citation], "recovery": recovery}]
        rows.append({"reference": pair["reference"], "dimensions": {"geometry_check": {
            "original": copy.deepcopy(assessment), "retrospective": copy.deepcopy(assessment),
            "findings": findings,
        }}})
    return {"turns": rows, "holistic": holistic or {}}


class AuditProvider(_IdentityOnlyProvider):
    def __init__(self, payload, finish_reason=None):
        super().__init__("anthropic")
        self.payload, self.calls, self.request = payload, 0, None
        self.finish_reason = finish_reason

    def complete(self, request):
        self.calls += 1
        self.request = request
        return CompletionResponse(text=json.dumps(self.payload, ensure_ascii=False), input_tokens=1, output_tokens=1,
                                  finish_reason=self.finish_reason)


@pytest.fixture(params=["sqlite", "postgres"])
def reliability_store(request, tmp_path):
    if request.param == "sqlite":
        path = tmp_path / "audit.db"
        store, url = SQLiteStorage(str(path)), f"sqlite:///{path}"
        yield store, url
        store.close()
        return
    dsn = os.environ.get("VERDICT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("no explicitly disposable live PostgreSQL database")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from verdict.storage.postgres import PostgresStorage

    if "test" not in conninfo_to_dict(dsn).get("dbname", "").lower():
        pytest.fail("reliability tests require an explicitly disposable test database")
    schema = f"audit_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = make_conninfo(dsn, options=f"-csearch_path={schema}")
    store = PostgresStorage(url, min_pool=1, max_pool=2)
    try:
        yield store, url
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def run_audit(storage, url, config, provider):
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert preview["plannedCalls"] == 1, preview["unavailableReason"]
    return execute_scoped(storage, storage_url=url, tenant="local",
                          config={**config, "planFingerprint": preview["planFingerprint"]},
                          confirm_external_egress=True, provider=provider)


@pytest.mark.parametrize("responses,labels,recovered,expected", [
    (("A hexagon has five sides.", "A hexagon has six sides."), ("FAIL", "PASS"), {0: 1}, "fail"),
    (("A hexagon has six sides.", "A hexagon has six sides."), ("PASS", "PASS"), {}, "pass"),
    (("A hexagon has five sides.", "A hexagon has five sides."), ("FAIL", "FAIL"), {}, "fail"),
    (("A hexagon has five sides.", "A hexagon has six sides; an octagon has seven."),
     ("FAIL", "FAIL"), {0: 1}, "fail"),
    (("The unseen device has a blue indicator.", "You claim the indicator is red."),
     ("UNCLEAR", "UNCLEAR"), {}, "unclear"),
])
def test_history_contract_roundtrip_replay_and_individual_recovery(
        reliability_store, responses, labels, recovered, expected):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle(responses))
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], labels, recovered))
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "completed", result
    assert result["dimensions"][0]["verdict"] == expected
    assert len(result["turnAssessments"]) == len(labels)
    assert len(result["historicalFindings"]) == labels.count("FAIL")
    if recovered:
        assert result["historicalFindings"][0]["recovery"]["state"] == "recovered"
        assert result["turnAssessments"][0]["original"]["verdict"] == "FAIL"
    replay = run_audit(storage, url, config, provider)
    assert replay == result
    assert provider.calls == 1
    assert provider.request.max_tokens == 4096
    saved = ControlStore(url).history("local", "scoped_evaluation", result["resultId"])[0]["payload"]
    assert saved["turnAssessments"] == result["turnAssessments"]
    assert ControlStore(url).history("other", "scoped_evaluation", result["resultId"]) == []


def test_final_completion_is_explicitly_distinct_from_historical_accuracy(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle())
    config = audit_config("final_response")
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    result = run_audit(storage, url, config, AuditProvider(audit_reply(preview["evidencePreview"], recoveries={0: 1})))
    assert result["dimensions"][0]["verdict"] == "pass"
    assert result["historicalFindings"][0]["recovery"]["state"] == "recovered"
    assert result["turnAssessments"][0]["original"]["verdict"] == "FAIL"
    historical = preview_scoped(storage, tenant="local", config=audit_config(), storage_url=url)
    assert historical["evaluatorFingerprint"] != result["evaluatorFingerprint"]
    assert historical.get("savedResult") is None


@pytest.mark.parametrize("mutation", ["missing_turn", "duplicate_turn", "extra_dimension", "erased_failure",
                                    "wrong_quote", "foreign_reference", "future_original", "no_finding",
                                    "recovery_same_turn", "judge_finding_id", "missing_assessment"])
def test_contradictory_structured_reply_is_saved_error_without_retry(reliability_store, mutation):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle())
    config = audit_config()
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    payload = audit_reply(evidence, ("FAIL", "FAIL"))
    first = payload["turns"][0]["dimensions"]["geometry_check"]
    if mutation == "missing_turn":
        payload["turns"].pop()
    elif mutation == "duplicate_turn":
        payload["turns"][1] = copy.deepcopy(payload["turns"][0])
    elif mutation == "extra_dimension":
        payload["turns"][0]["dimensions"]["undeclared"] = copy.deepcopy(first)
    elif mutation == "erased_failure":
        first["retrospective"]["verdict"] = "PASS"
        first["findings"] = []
    elif mutation == "wrong_quote":
        first["original"]["citations"][0]["quote"] = "A hexagon has nine sides."
    elif mutation == "foreign_reference":
        first["original"]["citations"][0]["reference"] = "response:foreign"
    elif mutation == "future_original":
        first["original"]["citations"].append({"reference": evidence["turnPairs"][1]["responseReference"],
                                                 "quote": evidence["turnPairs"][1]["response"]})
    elif mutation == "no_finding":
        first["findings"] = []
    elif mutation == "recovery_same_turn":
        first["findings"][0]["recovery"] = {"state": "recovered", "turnReference": evidence["turnPairs"][0]["reference"],
                                            "reason": "Synthetic", "citations": []}
    elif mutation == "judge_finding_id":
        # Finding IDs are assigned by the application; a judge-chosen one is an extra key.
        payload["turns"][1]["dimensions"]["geometry_check"]["findings"][0]["id"] = "finding-0"
    else:
        del first["original"]
    provider = AuditProvider(payload)
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "error"
    assert result["dimensions"] == []
    assert provider.calls == 1
    assert run_audit(storage, url, config, provider) == result
    assert provider.calls == 1


def test_real_citation_does_not_claim_entailment_validation(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle(("A hexagon has six sides.",)))
    config = audit_config()
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    payload = audit_reply(evidence, ("FAIL",))
    # The real response quote does not support this deliberately incorrect provider claim.
    payload["turns"][0]["dimensions"]["geometry_check"]["findings"][0]["reason"] = "The answer names a dangerous chemical."
    result = run_audit(storage, url, config, AuditProvider(payload))
    assert result["status"] == "completed"
    assert result["dimensions"][0]["citationStatus"] == "linked_unverified_support"
    assert "entailment is unverified" in result["citationValidation"]


def test_uncertain_chronology_cannot_be_clean_pass(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("A hexagon has six sides.", "A hexagon has six sides."))
    bundle = replace(bundle, turns=(bundle.turns[0], replace(bundle.turns[1],
                     started_at=bundle.turns[0].started_at, ended_at=bundle.turns[0].ended_at)))
    storage.replace_agent_run_bundle(bundle)
    config = audit_config()
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    assert not evidence["chronologyCertain"]
    result = run_audit(storage, url, config, AuditProvider(audit_reply(evidence, ("PASS", "PASS"))))
    assert result["dimensions"][0]["verdict"] == "unclear"


def test_audit_preview_withholds_oversized_shape_and_binds_budget(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle(tuple("A hexagon has six sides." for _ in range(5))))
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert preview["plannedCalls"] == 0
    assert preview["unavailableReason"] == "audit_shape_limit"
    config = {**config, "scope": "turn_window", "turnId": "turn-1"}
    first = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    second = preview_scoped(storage, tenant="local", config={**config, "maxOutputTokens": 2048}, storage_url=url)
    assert first["evaluatorFingerprint"] != second["evaluatorFingerprint"]
    assert first["upperStaticListPriceUsd"] > second["upperStaticListPriceUsd"]


def test_redacted_run_identifier_reports_unknown_freshness_not_stale(reliability_store, monkeypatch):
    """A run ID the redactor rewrites cannot select its evidence again."""
    import verdict.dashboard.scoped_evaluation as scoped

    storage, url = reliability_store
    run_id = "run-10.0.0.12"
    base = history_bundle()
    bundle = replace(base, run=replace(base.run, run_id=run_id),
                     turns=tuple(replace(turn, run_id=run_id) for turn in base.turns))
    storage.replace_agent_run_bundle(bundle)
    config = {**audit_config(), "runId": run_id}
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], recoveries={0: 1}))
    monkeypatch.setattr(scoped, "_provider", lambda _: provider)

    async def scenario():
        app = create_app(storage=url, tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            completed = await client.post("/api/scoped/run", json={**config, "planFingerprint": preview["planFingerprint"],
                                         "confirmExternalEgress": True}, headers=headers)
            assert completed.status_code == 200, completed.text
            saved = (await client.get(f"/api/scoped/result/{completed.json()['resultId']}")).json()
            assert saved["evidenceStatus"] == "unknown"
            assert saved["evidenceSelection"] is None
            assert run_id not in json.dumps(saved)
    asyncio.run(scenario())


def test_scheduled_audit_retains_mode_policy_and_output_allowance(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle())
    config = audit_config()
    config.pop("maxOutputTokens")
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], recoveries={0: 1}))
    schedule_scoped_once(storage, storage_url=url, tenant="local",
                         config={**config, "planFingerprint": preview["planFingerprint"]},
                         max_estimated_usd=1, confirm_persistent_egress=True)
    result = run_scheduled_once(url, tenant="local", provider=provider)
    assert result["status"] == "completed", result
    assert provider.calls == 1
    assert provider.request.max_tokens == 4096
    assert run_scheduled_once(url, tenant="local", provider=provider)["status"] == "idle"


def test_api_workflow_stale_evidence_and_saved_history(reliability_store, monkeypatch):
    import verdict.dashboard.scoped_evaluation as scoped

    storage, url = reliability_store
    bundle = history_bundle()
    storage.replace_agent_run_bundle(bundle)
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], recoveries={0: 1}))
    monkeypatch.setattr(scoped, "_provider", lambda _: provider)

    async def scenario():
        app = create_app(storage=url, tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            token = (await client.get("/api/setup/token")).json()["setupToken"]
            headers = {"X-Verdict-Setup": token}
            fresh = (await client.post("/api/scoped/preview", json=config, headers=headers)).json()
            completed = await client.post("/api/scoped/run", json={**config, "planFingerprint": fresh["planFingerprint"],
                                         "confirmExternalEgress": True}, headers=headers)
            assert completed.status_code == 200, completed.text
            result = completed.json()
            current = (await client.get(f"/api/scoped/result/{result['resultId']}")).json()
            assert current["evidenceStatus"] == "current"
            storage.replace_agent_run_bundle(replace(bundle, turns=(replace(bundle.turns[0],
                        final_response_redacted=bundle.turns[0].final_response_redacted + " Additional captured detail."), bundle.turns[1])))
            stale = (await client.get(f"/api/scoped/result/{result['resultId']}")).json()
            assert stale["evidenceStatus"] == "stale"
            assert stale["turnAssessments"] == result["turnAssessments"]
            assert stale["evaluatedAt"] == result["evaluatedAt"]
            replan = (await client.post("/api/scoped/preview", json=config, headers=headers)).json()
            assert replan.get("savedResult") is None
            assert provider.calls == 1
    asyncio.run(scenario())


def test_repeated_turn_ids_in_separate_runs_are_source_qualified(reliability_store):
    storage, url = reliability_store
    first = history_bundle(("A hexagon has five sides.",))
    later = first.turns[0].started_at + timedelta(minutes=5)
    second = replace(first, run=replace(first.run, run_id="run-b", started_at=later,
                                       ended_at=later + timedelta(minutes=1)),
                     turns=(replace(first.turns[0], run_id="run-b", started_at=later,
                                    ended_at=later + timedelta(minutes=1),
                                    final_response_redacted="A hexagon has six sides."),))
    storage.replace_agent_run_bundle(first)
    storage.replace_agent_run_bundle(second)
    config = {key: value for key, value in audit_config().items() if key != "runId"}
    config.update(scope="session", sessionId=first.session.source_session_id,
                  sourceKind=first.session.source_kind)
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    pairs = preview["evidencePreview"]["turnPairs"]
    assert pairs[0]["turnId"] == pairs[1]["turnId"]
    assert [pair["reference"] for pair in pairs] == ["T1", "T2"]
    assert "run-b" not in json.dumps(preview["evidencePreview"]["documents"])
    assert pairs[1]["responseReference"] not in pairs[0]["originalEvidenceReferences"]
    result = run_audit(storage, url, config, AuditProvider(audit_reply(preview["evidencePreview"], recoveries={0: 1})))
    assert result["status"] == "completed"
    assert result["dimensions"][0]["verdict"] == "fail"


@pytest.mark.parametrize("issue", ["truncated", "summary", "missing_sources", "conflicting"])
def test_capture_suitability_withholds_only_known_required_evidence(reliability_store, issue):
    storage, url = reliability_store
    bundle = history_bundle(("A hexagon has six sides.",))
    config = audit_config()
    if issue == "truncated":
        bundle = replace(bundle, turns=(replace(bundle.turns[0], response_truncated=True),))
    else:
        value = {"sources": "missing"} if issue == "missing_sources" else {"response": "summary"}
        if issue == "missing_sources":
            config["rubric"]["dimensions"][0]["requiredEvidence"].append("sources")
        event = AgentEvent("capture", "turn-0", 0, bundle.turns[0].started_at,
                           AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic",
                           {"name": "evaluation_capture", "value": value}, PrivacyClassification.REDACTED)
        events = (event,)
        if issue == "conflicting":
            events += (replace(event, event_id="capture-2", sequence=1,
                               attributes={"name": "evaluation_capture", "value": {"response": "substantive"}}),)
        bundle = replace(bundle, events=events)
    storage.replace_agent_run_bundle(bundle)
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert preview["plannedCalls"] == 0
    assert preview["suitability"]["state"] == "unavailable"
    assert preview["coverage"] == ("partial" if issue == "truncated" else "complete")


def test_holistic_writing_requirement_and_ambiguous_recovery_are_separate(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("There are six sides. This is a second sentence.", "A hexagon has six sides."))
    storage.replace_agent_run_bundle(bundle)
    config = audit_config("holistic")
    config["rubric"]["dimensions"][0]["description"] = "Use exactly one sentence per answer."
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    holistic = {"geometry_check": {"verdict": "FAIL", "reason": "The first answer contains two sentences.",
                                  "citations": [{"reference": evidence["turnPairs"][0]["responseReference"],
                                                 "quote": bundle.turns[0].final_response_redacted}]}}
    provider = AuditProvider(audit_reply(evidence, recoveries={0: 1}, holistic=holistic))
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "completed"
    assert result["dimensions"][0]["verdict"] == "fail"
    assert "exactly one sentence" in provider.request.messages[1]["content"]


def test_partial_nonrequired_events_cannot_make_clean_audit_pass(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("A hexagon has six sides.",))
    marker = AgentEvent("limit", "turn-0", 0, bundle.turns[0].started_at,
                        AgentEventType.OUTCOME, ExecutionStatus.UNKNOWN, "verdict:capture_limit",
                        {"name": "capture_limit", "value": "bounded"}, PrivacyClassification.REDACTED)
    storage.replace_agent_run_bundle(replace(bundle, events=(marker,)))
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert preview["plannedCalls"] == 1
    assert preview["coverage"] == "partial"
    result = run_audit(storage, url, config, AuditProvider(audit_reply(preview["evidencePreview"], ("PASS",))))
    assert result["dimensions"][0]["verdict"] == "unclear"


def test_redaction_canaries_reach_neither_provider_nor_saved_api(reliability_store, monkeypatch):
    storage, url = reliability_store
    secret = "sk-proj-" + "Z" * 40
    email = "synthetic.person@example.com"
    bundle = history_bundle((f"A hexagon has six sides. Contact {email}; key {secret}",))
    storage.replace_agent_run_bundle(bundle)
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], ("UNCLEAR",)))
    provider.payload["turns"][0]["dimensions"]["geometry_check"]["original"]["reason"] = f"{secret} {email}"
    result = run_audit(storage, url, config, provider)
    assert secret not in json.dumps(provider.request.messages)
    assert email not in json.dumps(provider.request.messages)
    saved = ControlStore(url).history("local", "scoped_evaluation", result["resultId"])
    for rendered in (json.dumps(result), json.dumps(saved)):
        assert secret not in rendered
        assert email not in rendered

    async def read_saved():
        app = create_app(storage=url, tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get(f"/api/scoped/result/{result['resultId']}")
            assert response.status_code == 200
            assert secret not in response.text
            assert email not in response.text
    asyncio.run(read_saved())


@pytest.mark.parametrize("offset_minutes", [-480, 0, 330])
def test_same_turn_late_source_and_time_offsets_partition_all_evidence(reliability_store, offset_minutes):
    storage, url = reliability_store
    bundle = history_bundle()
    zone = timezone(timedelta(minutes=offset_minutes))
    bundle = replace(bundle, turns=tuple(replace(turn, started_at=turn.started_at.astimezone(zone),
                     ended_at=turn.ended_at.astimezone(zone)) for turn in bundle.turns))
    late = AgentEvent("late-source", "turn-0", 0, bundle.turns[1].ended_at + timedelta(minutes=1),
                      AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic",
                      {"name": "shape_reference", "text": "A hexagon has six sides."},
                      PrivacyClassification.REDACTED)
    storage.replace_agent_run_bundle(replace(bundle, events=(late,)))
    evidence = preview_scoped(storage, tenant="local", config=audit_config(), storage_url=url)["evidencePreview"]
    ref = next(ref for ref, doc in evidence["documents"].items() if doc["kind"] == "context")
    for pair in evidence["turnPairs"]:
        original = set(pair["originalEvidenceReferences"])
        later = set(pair["laterEvidenceReferences"])
        unplaced = set(pair["unplacedEvidenceReferences"])
        assert not (original & later or original & unplaced or later & unplaced)
        assert original | later | unplaced == set(evidence["documents"])
        assert ref in later
        assert ref not in original


def test_oversized_run_is_withheld_without_projecting_a_truncated_audit(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle(tuple("A hexagon has six sides." for _ in range(21))))
    preview = preview_scoped(storage, tenant="local", config=audit_config(), storage_url=url)
    assert preview["plannedCalls"] == 0
    assert preview["evidencePreview"]["turnPairs"] == []
    assert preview["evidencePreview"]["documents"] == {}


def test_explicit_unavailable_source_does_not_satisfy_required_sources(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("A hexagon has six sides.",))
    source = AgentEvent("source", "turn-0", 0, bundle.turns[0].started_at,
                        AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic",
                        {"name": "shape", "text": "A hexagon has six sides.", "available": False},
                        PrivacyClassification.REDACTED)
    declaration = replace(source, event_id="capture", sequence=1,
                          attributes={"name": "evaluation_capture", "value": {"sources": "substantive"}})
    storage.replace_agent_run_bundle(replace(bundle, events=(source, declaration)))
    config = audit_config()
    config["rubric"]["dimensions"][0]["requiredEvidence"].append("sources")
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert preview["plannedCalls"] == 0
    assert preview["suitability"]["turns"][0]["sources"] == "missing"


def test_custom_dimension_and_ignored_rubric_extras_do_not_break_persistence(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle())
    config = audit_config()
    config["rubric"]["dimensions"][0]["name"] = "token_quality"
    config["rubric"]["ignored_secret"] = "synthetic metadata must not be persisted"
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    payload = audit_reply(preview["evidencePreview"])
    for row in payload["turns"]:
        row["dimensions"]["token_quality"] = row["dimensions"].pop("geometry_check")
    provider = AuditProvider(payload)
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "completed"
    assert result["criterionDescriptions"]["token_quality"]
    assert "ignored_secret" not in json.dumps(result)
    assert provider.calls == 1


def test_storage_invalid_reply_becomes_saved_error_and_replays(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle())
    config = audit_config()
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    payload = audit_reply(evidence)
    payload["turns"][0]["dimensions"]["geometry_check"]["original"]["reason"] = "Invalid\x00text"
    provider = AuditProvider(payload)
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "error"
    assert result["error"] == "invalid_judge_output"
    assert run_audit(storage, url, config, provider) == result
    assert provider.calls == 1


def test_source_sequence_disagreement_cannot_imply_later_recovery(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle()
    first, second = bundle.turns
    bundle = replace(bundle, turns=(replace(first, started_at=second.started_at, ended_at=second.ended_at),
                                    replace(second, started_at=first.started_at, ended_at=first.ended_at)))
    storage.replace_agent_run_bundle(bundle)
    config = audit_config("final_response")
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    assert [p["sequence"] for p in evidence["turnPairs"]] == [0, 1]
    assert not evidence["chronologyCertain"]
    result = run_audit(storage, url, config, AuditProvider(audit_reply(evidence, ("PASS", "PASS"))))
    assert result["dimensions"][0]["verdict"] == "unclear"


def test_large_valid_judge_reply_exceeding_store_limit_is_saved_error(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("x" * 970 + " A hexagon has five sides.",))
    # A long request is stored with the result but never repeated in the reply.
    bundle = replace(bundle, turns=(replace(bundle.turns[0], user_request_redacted="y" * 2_000 + " How many sides?"),))
    storage.replace_agent_run_bundle(bundle)
    config = audit_config()
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    payload = audit_reply(evidence, ("FAIL",))
    item = payload["turns"][0]["dimensions"]["geometry_check"]
    citation = item["original"]["citations"][0]
    for phase in ("original", "retrospective"):
        item[phase]["citations"] = [copy.deepcopy(citation) for _ in range(8)]
        item[phase]["reason"] = "r" * 990
    finding = item["findings"][0]
    finding["citations"] = [copy.deepcopy(citation) for _ in range(8)]
    finding["reason"] = "f" * 990
    finding["recovery"]["reason"] = "u" * 990
    item["findings"] = [copy.deepcopy(finding) for _ in range(4)]
    assert len(json.dumps(payload).encode()) < 64_000
    from verdict_eval.historical_audit import parse

    assert parse(json.dumps(payload), evidence, preview_scoped(storage, tenant="local", config=config, storage_url=url)["auditPolicy"])["dimensions"][0]["verdict"] == "fail"
    provider = AuditProvider(payload)
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "error"
    assert result["error"] == "invalid_judge_output"
    assert run_audit(storage, url, config, provider) == result
    assert provider.calls == 1


def test_blocked_summary_read_models_preserve_suitability_reason(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("A response summary.",))
    annotation = AgentEvent("capture", "turn-0", 0, bundle.turns[0].started_at,
                            AgentEventType.CONTEXT, ExecutionStatus.COMPLETED, "synthetic",
                            {"name": "evaluation_capture", "value": {"response": "summary"}},
                            PrivacyClassification.REDACTED)
    storage.replace_agent_run_bundle(replace(bundle, events=(annotation,)))
    run = build_agent_runs_bundle(url, tenant="local", run_ids=("run-a",))["runs"][0]
    session = build_agent_session_bundle(url, tenant="local", source_kind=bundle.session.source_kind,
                                         session_id=bundle.session.source_session_id)
    for row in (run, session):
        assert row["scopedEvaluationEligible"] is False
        assert row["scopedEvaluationUnavailableReason"] == "required_evidence_unavailable"
        assert row["scopedEvaluationSuitability"]["turns"][0]["response"] == "summary"
    assert session["coverage"] == "complete"


def test_saved_audit_read_is_independent_of_optional_evaluator(reliability_store, monkeypatch):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle())
    config = audit_config()
    evidence = preview_scoped(storage, tenant="local", config=config, storage_url=url)["evidencePreview"]
    result = run_audit(storage, url, config, AuditProvider(audit_reply(evidence)))
    app = create_app(storage=url, tenant_id="local")
    original_import = builtins.__import__

    def without_evaluator(name, *args, **kwargs):
        if name.startswith("verdict_eval"):
            raise ImportError("optional evaluation package unavailable")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_evaluator)

    async def read_saved():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get(f"/api/scoped/result/{result['resultId']}")
            assert response.status_code == 200
            assert response.json()["turnAssessments"] == result["turnAssessments"]
            assert response.json()["evidenceStatus"] == "current"
    asyncio.run(read_saved())


def test_identifiers_never_reach_provider_or_saved_result_in_any_encoding(reliability_store):
    storage, url = reliability_store
    email = "synthetic.owner@example.com"
    base = history_bundle()
    bundle = replace(
        base, session=replace(base.session, source_session_id=f"session-{email}"),
        run=replace(base.run, run_id=f"run-{email}", source_session_id=f"session-{email}"),
        turns=tuple(replace(turn, run_id=f"run-{email}", turn_id=f"{turn.turn_id}-{email}")
                    for turn in base.turns),
    )
    storage.replace_agent_run_bundle(bundle)
    config = {**audit_config(), "runId": bundle.run.run_id}
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], recoveries={0: 1}))
    result = run_audit(storage, url, config, provider)
    sent = json.dumps(provider.request.messages, ensure_ascii=False)
    saved = json.dumps(ControlStore(url).history("local", "scoped_evaluation", result["resultId"]))
    for rendered in (sent, saved, json.dumps(result)):
        for form in (email, "example.com", "%40", "synthetic.owner"):
            assert form not in rendered
    assert result["status"] == "completed"


@pytest.mark.parametrize("source", [
    {"status": ExecutionStatus.COMPLETED, "attributes": {"name": "shape", "text": "   \n"}},
    {"status": ExecutionStatus.COMPLETED, "attributes": {"name": "shape", "value": {"content": [" "]}}},
    {"status": ExecutionStatus.FAILED, "attributes": {"name": "shape", "text": "A hexagon has six sides."}},
    {"status": ExecutionStatus.CANCELLED, "attributes": {"name": "shape", "text": "A hexagon has six sides."}},
    {"status": ExecutionStatus.COMPLETED, "type": AgentEventType.TOOL_RESULT,
     "attributes": {"tool_name": "lookup", "result": "A hexagon has six sides.", "is_error": True}},
    {"status": ExecutionStatus.COMPLETED, "type": AgentEventType.TOOL_RESULT,
     "attributes": {"tool_name": "lookup", "result": "A hexagon has six sides."}, "usable": True},
    {"status": ExecutionStatus.COMPLETED, "attributes": {"name": "shape", "text": "A hexagon has six sides."},
     "usable": True},
])
def test_blank_failed_or_error_sources_do_not_satisfy_required_sources(reliability_store, source):
    storage, url = reliability_store
    bundle = history_bundle(("A hexagon has six sides.",))
    event = AgentEvent("source", "turn-0", 0, bundle.turns[0].started_at,
                       source.get("type", AgentEventType.CONTEXT), source["status"], "synthetic",
                       source["attributes"], PrivacyClassification.REDACTED)
    declaration = replace(event, event_id="capture", sequence=1, status=ExecutionStatus.COMPLETED,
                          event_type=AgentEventType.CONTEXT,
                          attributes={"name": "evaluation_capture", "value": {"sources": "substantive"}})
    storage.replace_agent_run_bundle(replace(bundle, events=(event, declaration)))
    config = audit_config()
    config["rubric"]["dimensions"][0]["requiredEvidence"].append("sources")
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    usable = source.get("usable", False)
    assert preview["plannedCalls"] == (1 if usable else 0)
    assert preview["suitability"]["turns"][0]["sources"] == ("substantive" if usable else "missing")


def test_finding_ids_are_assigned_once_and_stay_unique_after_redaction(reliability_store):
    storage, url = reliability_store
    storage.replace_agent_run_bundle(history_bundle(("A hexagon has five sides.", "A hexagon has five sides.")))
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    payload = audit_reply(preview["evidencePreview"], ("FAIL", "FAIL"))
    first = payload["turns"][0]["dimensions"]["geometry_check"]["findings"]
    first.append({**copy.deepcopy(first[0]), "reason": "Contact a.person@example.com about the count."})
    result = run_audit(storage, url, config, AuditProvider(payload))
    assert result["status"] == "completed"

    async def read_saved():
        app = create_app(storage=url, tenant_id="local")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            return (await client.get(f"/api/scoped/result/{result['resultId']}")).json()
    saved = asyncio.run(read_saved())
    for findings in (result["historicalFindings"], saved["historicalFindings"]):
        assert [finding["id"] for finding in findings] == ["F1", "F2", "F3"]


def test_audit_reapplies_current_sanitizer_to_text_stored_by_an_earlier_release(reliability_store):
    storage, url = reliability_store
    secret = "sk-proj-" + "Q" * 40
    storage.replace_agent_run_bundle(history_bundle(("A hexagon has six sides.",)))
    # An earlier release's sanitizer may have stored text the current one removes.
    stored = f"A hexagon has six sides. Key {secret}"
    if url.startswith("sqlite"):
        import sqlite3

        with sqlite3.connect(url.removeprefix("sqlite:///")) as connection:
            connection.execute("UPDATE agent_turns SET final_response_redacted = ?", (stored,))
    else:
        import psycopg

        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute("UPDATE agent_turns SET final_response_redacted = %s", (stored,))
    config = audit_config()
    preview = preview_scoped(storage, tenant="local", config=config, storage_url=url)
    assert secret not in json.dumps(preview)
    provider = AuditProvider(audit_reply(preview["evidencePreview"], ("PASS",)))
    result = run_audit(storage, url, config, provider)
    assert result["status"] == "completed"
    assert secret not in json.dumps(provider.request.messages)
    assert secret not in json.dumps(ControlStore(url).history("local", "scoped_evaluation", result["resultId"]))


def test_audit_only_selection_is_offered_by_the_read_models(reliability_store):
    storage, url = reliability_store
    bundle = history_bundle(("A hexagon has six sides.",))
    marker = AgentEvent("limit", "turn-0", 0, bundle.turns[0].started_at,
                        AgentEventType.OUTCOME, ExecutionStatus.UNKNOWN, "verdict:capture_limit",
                        {"name": "capture_limit", "value": "bounded"}, PrivacyClassification.REDACTED)
    storage.replace_agent_run_bundle(replace(bundle, events=(marker,)))
    run = build_agent_runs_bundle(url, tenant="local", run_ids=("run-a",))["runs"][0]
    assert run["scopedEvaluationEligible"] is False
    assert run["historicalAuditAvailable"] is True
    assert preview_scoped(storage, tenant="local", config=audit_config(), storage_url=url)["plannedCalls"] == 1
    assert preview_scoped(storage, tenant="local", config=_config(), storage_url=url)["plannedCalls"] == 0
    storage.replace_agent_run_bundle(replace(bundle, turns=(replace(bundle.turns[0], response_truncated=True),),
                                             events=(marker,)))
    assert build_agent_runs_bundle(url, tenant="local", run_ids=("run-a",))["runs"][0]["historicalAuditAvailable"] is False
