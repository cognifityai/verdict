"""Synthetic scoped audit workflow; only the provider is a test double."""
import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_scoped_evaluation_server import MockProvider, _bundle, _config
from verdict.dashboard.app import create_app
from verdict.dashboard.scoped_evaluation import execute_scoped, preview_scoped
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
from verdict.storage import SQLiteStorage
from verdict_eval.judge import Judge, Rubric, RubricDimension
from verdict_eval.judge_output import JudgeOutputError
from verdict_eval.providers import CompletionResponse


def test_malformed_provider_output_is_error_not_evidence_uncertainty():
    class Broken(MockProvider):
        def complete(self, request):
            return CompletionResponse(text='{"completion":{"verdict":"MAYBE"}}')
    judge = Judge(Broken(), 'synthetic', Rubric('test', '1', (RubricDimension('completion', 'Check output'),)))
    with pytest.raises(JudgeOutputError):
        judge.score(query='Return two items', response='One item')


def test_explicit_summary_preserves_coverage_but_withholds_judge(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'summary.db'))
    bundle = _bundle()
    turn = replace(bundle.turns[0], response_content_kind='summary')
    storage.replace_agent_run_bundle(replace(bundle, turns=(turn,)))
    reloaded = storage.get_agent_run_bundle('local', 'run-a')
    assert reloaded.turns[0].response_content_kind == 'summary'
    preview = preview_scoped(storage, tenant='local', config=_config())
    assert preview['coverage'] == 'complete'
    assert preview['evaluationSuitability']['state'] == 'unavailable'
    assert preview['plannedCalls'] == 0
    storage.close()


def test_legacy_provenance_is_unknown_and_does_not_disable_evaluation(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'legacy.db'))
    storage.replace_agent_run_bundle(_bundle())
    preview = preview_scoped(storage, tenant='local', config=_config())
    assert preview['plannedCalls'] == 1
    assert preview['evaluationSuitability']['state'] == 'limited'
    assert AgentTurn.__dataclass_fields__['response_content_kind'].default == 'unknown'
    storage.close()



def audit_config(aggregation='historical_no_errors', authority='recorded_sources'):
    config = _config()
    config.update(auditMode='historical', maxOutputTokens=4096)
    config['rubric']['dimensions'] = [{'name': 'accuracy', 'description': 'Each supplied calculation must be correct.',
                                      'aggregation': aggregation, 'evidenceAuthority': authority}]
    return config


def history_bundle(answers=('2 + 3 = 6.', '2 + 3 = 5.'), tenant='local', run='run-a'):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    turns = tuple(AgentTurn(f't{i}', run, i, now + timedelta(minutes=i*2), ExecutionStatus.COMPLETED,
                           now + timedelta(minutes=i*2+1), 'Compute 2 + 3.', answer,
                           EvidenceState.PRESENT, EvidenceState.PRESENT,
                           request_content_kind='substantive', response_content_kind='substantive')
                  for i, answer in enumerate(answers))
    events = tuple(AgentEvent(f's{i}', t.turn_id, 0, t.started_at, AgentEventType.CONTEXT,
                             ExecutionStatus.COMPLETED, 'synthetic',
                             {'text': '2 + 3 = 5.', 'available': True, 'content_kind': 'substantive'}, PrivacyClassification.REDACTED)
                   for i, t in enumerate(turns))
    return AgentRunBundle(SourceSession('session', tenant, 'synthetic', 'b'*64, now, now),
                          AgentRun(run, 'session', tenant, now, ExecutionStatus.COMPLETED,
                                   turns[-1].ended_at), turns, events)


class AuditProvider(MockProvider):
    """Specified judge replies exercise transport/parser contracts, not judge accuracy."""
    def __init__(self, states=('FAIL', 'PASS'), recoveries=None, mutate=None):
        super().__init__()
        self.states = states
        self.recoveries = recoveries or {}
        self.mutate = mutate
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        evidence = json.loads(request.messages[1]['content'])['evidence']
        pairs, sources = evidence['pairs'], evidence['sources']
        turns, findings = [], []
        for i, (pair, state) in enumerate(zip(pairs, self.states, strict=True)):
            citation = {'reference': pair['reference'], 'quote': pair['response']}
            item = {'verdict': state, 'reason': 'Synthetic predetermined assessment.', 'evidence': [citation, {'reference': sources[i]['reference'], 'quote': '2 + 3 = 5.'}]}
            turns.append({'reference': pair['reference'], 'original': {'accuracy': copy.deepcopy(item)},
                          'retrospective': {'accuracy': copy.deepcopy(item)}})
            if state == 'FAIL':
                later = next((p for j, p in enumerate(pairs) if j > i and self.states[j] == 'PASS'), None)
                recovery = self.recoveries.get(i, 'recovered' if later else 'unresolved')
                findings.append({'id': f'f{i}', 'turnReference': pair['reference'], 'dimension': 'accuracy',
                                 'basis': 'original', 'claim': 'Incorrect calculation.',
                                 'reason': 'The answer contradicts the supplied calculation.',
                                 'evidence': [citation, {'reference': sources[i]['reference'], 'quote': '2 + 3 = 5.'}],
                                 'recovery': {'status': recovery, 'reason': 'Recovery is assessed for this finding only.',
                                              'evidence': [{'reference': later['reference'], 'quote': later['response']}]
                                              if later and recovery == 'recovered' else []}})
        result = {'turns': turns, 'findings': findings, 'holistic': {}}
        if self.mutate:
            self.mutate(result, evidence)
        return CompletionResponse(text=json.dumps(result), input_tokens=100, output_tokens=100)


def run_audit(tmp_path, provider, answers=('2 + 3 = 6.', '2 + 3 = 5.'), config=None):
    path = tmp_path / 'audit.db'
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(history_bundle(answers))
    config = config or audit_config()
    preview = preview_scoped(storage, tenant='local', config=config)
    assert preview['plannedCalls'] == 1
    result = execute_scoped(storage, storage_url=f'sqlite:///{path}', tenant='local',
                            config={**config, 'planFingerprint': preview['planFingerprint']},
                            confirm_external_egress=True, provider=provider)
    storage.close()
    return result, preview


@pytest.mark.parametrize('states,answers,expected,recoveries', [
    (('FAIL', 'PASS'), ('2 + 3 = 6.', '2 + 3 = 5.'), 'FAIL', ('recovered',)),
    (('PASS', 'PASS'), ('2 + 3 = 5.', '2 + 3 = 5.'), 'PASS', ()),
    (('FAIL', 'FAIL'), ('2 + 3 = 6.', '2 + 3 = 6.'), 'FAIL', ('unresolved', 'unresolved')),
    (('FAIL', 'PASS', 'FAIL'), ('2 + 3 = 6.', '2 + 3 = 5.', '2 + 3 = 7.'), 'FAIL', ('recovered', 'unresolved')),
    (('UNCLEAR', 'UNCLEAR'), ('The earlier answer may be wrong.', 'The allegation is unverified.'), 'UNCLEAR', ()),
])
def test_specified_history_replies_preserve_failures_and_per_finding_recovery(tmp_path, states, answers, expected, recoveries):
    provider = AuditProvider(states)
    result, preview = run_audit(tmp_path, provider, answers)
    assert result['status'] == 'completed'
    assert result['dimensions'][0]['verdict'] == expected.lower()
    assert tuple(f['recovery']['status'] for f in result['audit']['findings']) == recoveries
    assert len(result['audit']['turns']) == len(states)
    assert provider.calls == 1
    assert provider.requests[0].max_tokens == preview['requestedOutputTokens'] == 4096
    assert result['semanticSupport'] == 'unverified_judge_assessment'


def test_final_completion_and_historical_accuracy_have_explicit_different_aggregation(tmp_path):
    result, _ = run_audit(tmp_path, AuditProvider(), config=audit_config('final_turn'))
    assert result['dimensions'][0]['verdict'] == 'pass'
    assert result['audit']['turns'][0]['original']['accuracy']['verdict'] == 'FAIL'
    assert result['audit']['findings'][0]['recovery']['status'] == 'recovered'


def test_ambiguous_recovery_cannot_produce_clean_final_pass(tmp_path):
    result, _ = run_audit(tmp_path, AuditProvider(recoveries={0: 'unclear'}), config=audit_config('final_turn'))
    assert result['dimensions'][0]['verdict'] == 'unclear'


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'contradiction', 'bad_quote', 'future_original', 'missing_finding', 'bad_recovery', 'allegation_only'])
def test_contradictory_or_malformed_structured_reply_is_saved_error(tmp_path, mutation):
    def mutate(result, evidence):
        if mutation == 'missing':
            result['turns'].pop()
        elif mutation == 'duplicate':
            result['turns'][1] = result['turns'][0]
        elif mutation == 'contradiction':
            result['turns'][0]['retrospective']['accuracy']['verdict'] = 'PASS'
        elif mutation == 'bad_quote':
            result['findings'][0]['evidence'][0]['quote'] = 'This quote was never captured.'
        elif mutation == 'future_original':
            result['turns'][0]['original']['accuracy']['evidence'] = [{'reference': evidence['pairs'][1]['reference'], 'quote': evidence['pairs'][1]['response']}, {'reference': evidence['sources'][1]['reference'], 'quote': '2 + 3 = 5.'}]
        elif mutation == 'missing_finding':
            result['findings'] = []
        elif mutation == 'bad_recovery':
            result['findings'][0]['recovery']['evidence'] = [result['findings'][0]['evidence'][0]]
        elif mutation == 'allegation_only':
            result['findings'][0]['evidence'] = result['findings'][0]['evidence'][:1]
    result, _ = run_audit(tmp_path, AuditProvider(mutate=mutate))
    assert result['status'] == 'error'
    assert result['dimensions'] == []
    assert result['audit'] is None


def test_real_but_irrelevant_citation_is_not_claimed_as_semantic_validation(tmp_path):
    def mutate(result, evidence):
        for turn in result['turns']:
            turn['original']['accuracy']['reason'] = 'The source title proves this conclusion.'
    result, _ = run_audit(tmp_path, AuditProvider(mutate=mutate))
    assert result['citationValidation'] == 'reference_and_exact_quote_only'
    assert result['audit']['semanticSupport'] == 'unverified_judge_assessment'


def test_api_saved_replay_staleness_and_tenant_isolation(tmp_path, monkeypatch):
    import verdict.dashboard.scoped_evaluation as scoped
    provider = AuditProvider()
    path = tmp_path / 'workflow.db'
    url = f'sqlite:///{path}'
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(history_bundle())
    monkeypatch.setattr(scoped, '_provider', lambda _name: provider)
    config = audit_config()
    with TestClient(create_app(storage=url, tenant_id='local')) as client:
        token = client.get('/api/setup/token').json()['setupToken']
        headers = {'X-Verdict-Setup': token}
        preview = client.post('/api/scoped/preview', json=config, headers=headers).json()
        approved = {**config, 'planFingerprint': preview['planFingerprint'], 'confirmExternalEgress': True}
        denied = client.post('/api/scoped/run', json={**approved, 'confirmExternalEgress': False}, headers=headers)
        assert denied.status_code == 400
        assert provider.calls == 0
        response = client.post('/api/scoped/run', json=approved, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['dimensions'][0]['verdict'] == 'fail'
        assert client.post('/api/scoped/run', json=approved, headers=headers).json() == result
        assert provider.calls == 1
        saved = client.get('/api/scoped/result/' + result['resultId']).json()
        assert saved['stale'] is False
        changed = history_bundle()
        storage.replace_agent_run_bundle(replace(changed, events=(*changed.events, replace(changed.events[0], event_id='added-source', sequence=1, attributes={'text': 'A newly captured source.', 'content_kind': 'substantive'}))))
        stale = client.get('/api/scoped/result/' + result['resultId']).json()
        assert stale['stale'] is True
        assert stale['audit'] == saved['audit']
        assert client.post('/api/scoped/run', json=approved, headers=headers).status_code == 400
    with TestClient(create_app(storage=url, tenant_id='other')) as other:
        assert other.get('/api/scoped/result/' + result['resultId']).status_code == 404
    storage.close()

@pytest.mark.parametrize('kind', ['summary', 'counts', 'references', 'completion_summary'])
def test_known_non_substantive_source_withholds_required_source_audit(tmp_path, kind):
    storage = SQLiteStorage(str(tmp_path / 'source-kind.db'))
    bundle = history_bundle()
    events = tuple(replace(event, attributes={**event.attributes, 'content_kind': kind}) for event in bundle.events)
    storage.replace_agent_run_bundle(replace(bundle, events=events))
    preview = preview_scoped(storage, tenant='local', config=audit_config())
    assert preview['coverage'] == 'complete'
    assert preview['plannedCalls'] == 0
    assert preview['unavailableReason'] == 'required_source_evidence_unavailable'
    assert all(s['contentKind'] == kind for s in preview['evaluationSuitability']['sources'])
    storage.close()


def test_unclear_chronology_prevents_clean_pass_without_fabricating_order(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'chronology.db'))
    bundle = history_bundle(('2 + 3 = 5.', '2 + 3 = 5.'))
    turns = (bundle.turns[0], replace(bundle.turns[1], started_at=bundle.turns[0].started_at,
                                     ended_at=bundle.turns[0].ended_at))
    storage.replace_agent_run_bundle(replace(bundle, turns=turns, events=tuple(
        replace(e, occurred_at=turns[0].started_at) for e in bundle.events)))
    config = audit_config()
    preview = preview_scoped(storage, tenant='local', config=config)
    assert preview['evidencePreview']['pairedEvidence']['chronologyKnown'] is False
    result = execute_scoped(storage, storage_url=f'sqlite:///{tmp_path / "chronology.db"}', tenant='local',
                            config={**config, 'planFingerprint': preview['planFingerprint']},
                            confirm_external_egress=True, provider=AuditProvider(('PASS', 'PASS')))
    assert result['dimensions'][0]['verdict'] == 'unclear'
    storage.close()


def test_repeated_turn_ids_across_runs_get_distinct_source_qualified_references(tmp_path):
    path = tmp_path / 'repeated.db'
    url = f'sqlite:///{path}'
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(history_bundle(run='run-a'))
    storage.replace_agent_run_bundle(history_bundle(run='run-b'))
    config = {**audit_config(), 'scope': 'session', 'sessionId': 'session', 'sourceKind': 'synthetic'}
    config.pop('runId')
    preview = preview_scoped(storage, tenant='local', config=config, storage_url=url)
    refs = [p['reference'] for p in preview['evidencePreview']['pairedEvidence']['pairs']]
    assert len(refs) == len(set(refs)) == 4
    assert 'turn:synthetic/session/run-a/t0' in refs
    assert 'turn:synthetic/session/run-b/t0' in refs
    storage.close()


def test_budget_authority_and_meaning_are_bound_to_approval(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'identity.db'))
    storage.replace_agent_run_bundle(history_bundle())
    first = preview_scoped(storage, tenant='local', config=audit_config())
    budget = preview_scoped(storage, tenant='local', config={**audit_config(), 'maxOutputTokens': 8192})
    authority = preview_scoped(storage, tenant='local', config=audit_config(authority='public_knowledge'))
    final = preview_scoped(storage, tenant='local', config=audit_config('final_turn'))
    assert len({p['evaluatorFingerprint'] for p in (first, budget, authority, final)}) == 4
    assert budget['upperStaticListPriceUsd'] > first['upperStaticListPriceUsd']
    provider = AuditProvider()
    with pytest.raises(ValueError, match='current'):
        execute_scoped(storage, storage_url=f'sqlite:///{tmp_path / "identity.db"}', tenant='local',
                        config={**audit_config('final_turn'), 'planFingerprint': first['planFingerprint']},
                        confirm_external_egress=True, provider=provider)
    assert provider.calls == 0
    storage.close()


def test_oversized_selection_withheld_without_truncation(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'oversize.db'))
    storage.replace_agent_run_bundle(history_bundle(('x' * 5000, 'y' * 5000)))
    preview = preview_scoped(storage, tenant='local', config=audit_config())
    assert preview['plannedCalls'] == 0
    assert preview['unavailableReason'] == 'evidence_limit'
    storage.close()


def test_explicit_writing_requirement_actual_omission_can_remain_fail(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'writing.db'))
    bundle = history_bundle(('Only one sentence.',))
    storage.replace_agent_run_bundle(replace(bundle, turns=(replace(bundle.turns[0], user_request_redacted='Write exactly three bullet points.'),)))
    config = _config()
    config['rubric']['dimensions'][0]['description'] = 'Return three bullet points as explicitly requested.'
    class WritingProvider(MockProvider):
        def complete(self, request):
            self.calls += 1
            assert request.messages[0]['role'] == 'system'
            return CompletionResponse(text='{"completion":{"verdict":"FAIL","reason":"[turn:t0] is a complete answer with one sentence and no bullet points."}}')
    preview = preview_scoped(storage, tenant='local', config=config)
    result = execute_scoped(storage, storage_url=f'sqlite:///{tmp_path / "writing.db"}', tenant='local',
                            config={**config, 'planFingerprint': preview['planFingerprint']},
                            confirm_external_egress=True, provider=WritingProvider())
    assert result['dimensions'][0]['verdict'] == 'fail'
    storage.close()


def test_sdk_provenance_and_recursive_canaries_survive_real_durable_capture(tmp_path):
    import verdict
    from verdict.evidence import agent_run_bundle_from_json, agent_run_bundle_to_json
    path = tmp_path / 'sdk-provenance.db'
    verdict.init(storage=f'sqlite:///{path}', service_name='synthetic-audit', capture_content=True)
    try:
        with verdict.agent_run(name='synthetic-run', session_id='synthetic-session') as run:
            run_id = run.run_id
            with run.turn(user_input='Compute 2 + 3.', content_kind='substantive') as turn:
                turn.record_context(name='source', value={'nested': {'email': 'audit.person@example.com',
                                                                    'api_key': 'opaque-audit-canary'}}, content_kind='references')
                with turn.tool('calculator', arguments={'sum': [2, 3]}) as tool:
                    tool.set_output('5', content_kind='substantive')
                turn.set_output('2 + 3 = 5.', content_kind='substantive')
    finally:
        verdict.shutdown()
    storage = SQLiteStorage(str(path))
    bundle = storage.get_agent_run_bundle('__verdict_local__', run_id)
    assert bundle.turns[0].request_content_kind == 'substantive'
    assert bundle.turns[0].response_content_kind == 'substantive'
    assert agent_run_bundle_from_json(agent_run_bundle_to_json(bundle)) == bundle
    assert next(e for e in bundle.events if e.event_type == AgentEventType.CONTEXT).attributes['content_kind'] == 'references'
    assert next(e for e in bundle.events if e.event_type == AgentEventType.TOOL_RESULT).attributes['content_kind'] == 'substantive'
    config = {**audit_config(), 'runId': run_id}
    preview = preview_scoped(storage, tenant='__verdict_local__', config=config)
    serialized = json.dumps(preview)
    assert 'audit.person' not in serialized
    assert 'opaque-audit-canary' not in serialized
    assert 'audit.person' not in agent_run_bundle_to_json(bundle)
    assert 'opaque-audit-canary' not in agent_run_bundle_to_json(bundle)
    with TestClient(create_app(storage=f'sqlite:///{path}', tenant_id='__verdict_local__')) as client:
        token = client.get('/api/setup/token').json()['setupToken']
        api_preview = client.post('/api/scoped/preview', json=config, headers={'X-Verdict-Setup': token})
        assert api_preview.status_code == 200
        assert 'audit.person' not in api_preview.text
        assert 'opaque-audit-canary' not in api_preview.text
    storage.close()


def test_live_disposable_postgres_audit_roundtrip_and_api(tmp_path, monkeypatch):
    import os
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'packages/verdict/tests'))
    import verdict.dashboard.scoped_evaluation as scoped
    from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
    from verdict.storage.postgres import PostgresStorage
    dsn, reason = validate_test_dsn(os.environ.get('VERDICT_TEST_POSTGRES_DSN'), allow_any_database=False)
    if not dsn:
        pytest.skip(reason)
    with isolated_test_dsn(dsn) as url:
        storage = PostgresStorage(url, min_pool=1, max_pool=2)
        storage.replace_agent_run_bundle(history_bundle())
        assert storage.get_agent_run_bundle('local', 'run-a').turns[0].response_content_kind == 'substantive'
        provider = AuditProvider()
        config = audit_config()
        preview = preview_scoped(storage, tenant='local', config=config, storage_url=url)
        approved = {**config, 'planFingerprint': preview['planFingerprint']}
        result = execute_scoped(storage, storage_url=url, tenant='local', config=approved,
                                confirm_external_egress=True, provider=provider)
        assert result['status'] == 'completed'
        assert result['dimensions'][0]['verdict'] == 'fail'
        storage.close()
        storage = PostgresStorage(url, min_pool=1, max_pool=2)
        replay = execute_scoped(storage, storage_url=url, tenant='local', config=approved,
                                confirm_external_egress=True, provider=provider)
        assert replay == result
        assert provider.calls == 1
        monkeypatch.setattr(scoped, '_provider', lambda _name: provider)
        with TestClient(create_app(storage=url, tenant_id='local')) as client:
            saved = client.get('/api/scoped/result/' + result['resultId'])
            assert saved.status_code == 200, saved.text
            assert saved.json()['audit'] == result['audit']
            assert saved.json()['stale'] is False
        storage.close()


def test_earlier_cross_run_source_cannot_count_as_later_recovery(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'cross-run.db'))
    first = history_bundle(('2 + 3 = 5.',), run='run-a')
    second = history_bundle(('2 + 3 = 6.',), run='run-b')
    later = timedelta(minutes=3)
    second = replace(second, run=replace(second.run, started_at=second.run.started_at+later,
                                        ended_at=second.run.ended_at+later),
                     turns=tuple(replace(t, started_at=t.started_at+later, ended_at=t.ended_at+later) for t in second.turns),
                     events=tuple(replace(e, occurred_at=e.occurred_at+later) for e in second.events))
    storage.replace_agent_run_bundle(first)
    storage.replace_agent_run_bundle(second)
    config = {**audit_config(), 'scope': 'session', 'sessionId': 'session', 'sourceKind': 'synthetic'}
    config.pop('runId')
    url = f'sqlite:///{tmp_path / "cross-run.db"}'
    preview = preview_scoped(storage, tenant='local', config=config, storage_url=url)
    assert preview['evidencePreview']['pairedEvidence']['chronologyKnown'] is True
    def mutate(result, evidence):
        result['findings'][0]['recovery'] = {'status': 'recovered', 'reason': 'This source is earlier and cannot establish recovery.',
                                           'evidence': [{'reference': evidence['sources'][0]['reference'], 'quote': '2 + 3 = 5.'}]}
    result = execute_scoped(storage, storage_url=url, tenant='local', config={**config, 'planFingerprint': preview['planFingerprint']},
                            confirm_external_egress=True, provider=AuditProvider(('PASS', 'FAIL'), mutate=mutate))
    assert result['status'] == 'error'
    assert result['audit'] is None
    storage.close()


def test_later_user_request_alone_cannot_establish_response_recovery(tmp_path):
    def mutate(result, evidence):
        result['findings'][0]['recovery']['evidence'] = [{'reference': evidence['pairs'][1]['reference'],
                                                        'quote': evidence['pairs'][1]['request']}]
    result, _ = run_audit(tmp_path, AuditProvider(mutate=mutate))
    assert result['status'] == 'error'
    assert result['audit'] is None


def test_retrospective_finding_does_not_justify_original_failure(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'phases.db'))
    bundle = history_bundle()
    storage.replace_agent_run_bundle(bundle)
    config = audit_config()
    preview = preview_scoped(storage, tenant='local', config=config)
    original_refs = preview['evidencePreview']['pairedEvidence']['pairs'][0]['originalEvidenceReferences']
    assert len(original_refs) == 2
    def mutate(result, evidence):
        result['findings'][0]['basis'] = 'retrospective'
    result = execute_scoped(storage, storage_url=f'sqlite:///{tmp_path / "phases.db"}', tenant='local',
                            config={**config, 'planFingerprint': preview['planFingerprint']},
                            confirm_external_egress=True, provider=AuditProvider(mutate=mutate))
    assert result['status'] == 'error'
    assert result['audit'] is None
    storage.close()


def test_original_factual_pass_requires_original_source_authority(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'original-source.db'))
    bundle = history_bundle(('2 + 3 = 5.', '2 + 3 = 5.'))
    events = tuple(replace(e, occurred_at=e.occurred_at+timedelta(minutes=2)) for e in bundle.events)
    storage.replace_agent_run_bundle(replace(bundle, events=events))
    config = audit_config()
    preview = preview_scoped(storage, tenant='local', config=config)
    def valid_except_original_authority(reply, evidence):
        reply['turns'][0]['original']['accuracy']['evidence'] = reply['turns'][0]['original']['accuracy']['evidence'][:1]
        reply['turns'][1]['original']['accuracy']['evidence'][1] = {'reference': evidence['sources'][0]['reference'], 'quote': '2 + 3 = 5.'}
    result = execute_scoped(storage, storage_url=f'sqlite:///{tmp_path / "original-source.db"}', tenant='local',
                            config={**config, 'planFingerprint': preview['planFingerprint']},
                            confirm_external_egress=True, provider=AuditProvider(('PASS', 'PASS'), mutate=valid_except_original_authority))
    assert result['status'] == 'error'
    storage.close()


@pytest.mark.parametrize('source_text,status,is_error', [
    ('', ExecutionStatus.COMPLETED, False),
    ('   ', ExecutionStatus.COMPLETED, False),
    ('Source fetch failed.', ExecutionStatus.FAILED, True),
    ('Source fetch failed.', ExecutionStatus.COMPLETED, True),
])
def test_known_empty_or_failed_sources_do_not_satisfy_required_evidence(tmp_path, source_text, status, is_error):
    storage = SQLiteStorage(str(tmp_path / 'unusable-source.db'))
    bundle = history_bundle()
    events = tuple(replace(e, event_type=AgentEventType.TOOL_RESULT, status=status,
                           attributes={'result': source_text, 'is_error': is_error, 'content_kind': 'substantive'})
                   for e in bundle.events)
    storage.replace_agent_run_bundle(replace(bundle, events=events))
    preview = preview_scoped(storage, tenant='local', config=audit_config())
    assert preview['coverage'] == 'complete'
    assert preview['plannedCalls'] == 0
    assert preview['unavailableReason'] == 'required_source_evidence_unavailable'
    storage.close()


@pytest.mark.parametrize('provider_error', [False, True])
def test_audit_provider_call_uses_judge_workload_and_restores_outer_context(tmp_path, provider_error):
    from verdict.client import get_context_workload, workload_context
    class ObservingProvider(AuditProvider):
        def complete(self, request):
            self.observed = get_context_workload()
            if provider_error:
                raise RuntimeError('Synthetic provider outage')
            return super().complete(request)
    provider = ObservingProvider()
    with workload_context('application'):
        result, _ = run_audit(tmp_path, provider)
        assert get_context_workload() == 'application'
    assert provider.observed == 'judge'
    assert result['status'] == ('error' if provider_error else 'completed')


def test_nested_canaries_are_absent_from_saved_judge_result_and_api(tmp_path):
    def mutate(result, evidence):
        for turn in result['turns']:
            for phase in ('original', 'retrospective'):
                turn[phase]['accuracy']['reason'] = 'email audit.person@example.com api_key=opaque-audit-canary'
        result['findings'][0]['reason'] = 'email audit.person@example.com api_key=opaque-audit-canary'
    result, _ = run_audit(tmp_path, AuditProvider(mutate=mutate))
    assert result['status'] == 'completed'
    assert 'audit.person' not in json.dumps(result)
    assert 'opaque-audit' not in json.dumps(result)
    path = tmp_path / 'audit.db'
    assert b'audit.person' not in path.read_bytes()
    assert b'opaque-audit' not in path.read_bytes()
    with TestClient(create_app(storage=f'sqlite:///{path}', tenant_id='local')) as client:
        response = client.get('/api/scoped/result/' + result['resultId'])
        assert response.status_code == 200
        assert 'audit.person' not in response.text
        assert 'opaque-audit' not in response.text


@pytest.mark.parametrize('field', ['turns', 'findings', 'holistic'])
@pytest.mark.parametrize('value', [None, True, 0, 'PASS', [['nested', {'unexpected': 'value'}]]])
def test_structured_parser_rejects_wrong_container_types_at_durable_boundary(tmp_path, field, value):
    result, _ = run_audit(tmp_path, AuditProvider(mutate=lambda reply, evidence: reply.update({field: value})))
    assert result['status'] == 'error'
    assert result['dimensions'] == []
    assert result['audit'] is None


def test_audit_rubric_allowlist_and_redaction_bind_preview_provider_replay_and_api(tmp_path):
    config = audit_config()
    config['rubric']['dimensions'][0]['privateNotes'] = {'opaque': 'synthetic-unrecognized-private-canary'}
    config['rubric']['dimensions'][0]['description'] += ' Contact audit.person@example.com; api_key=opaque-audit-canary'
    provider = AuditProvider()
    result, preview = run_audit(tmp_path, provider, config=config)
    assert result['status'] == 'completed'
    for value in (preview, result, [m['content'] for m in provider.requests[0].messages]):
        serialized = json.dumps(value)
        assert 'privateNotes' not in serialized
        assert 'synthetic-unrecognized-private-canary' not in serialized
        assert 'audit.person' not in serialized
        assert 'opaque-audit' not in serialized
    path = tmp_path / 'audit.db'
    with TestClient(create_app(storage=f'sqlite:///{path}', tenant_id='local')) as client:
        saved = client.get('/api/scoped/result/' + result['resultId'])
        assert saved.status_code == 200
        assert saved.json()['stale'] is False
        assert 'privateNotes' not in saved.text
        token = client.get('/api/setup/token').json()['setupToken']
        replay = client.post('/api/scoped/preview', json=config, headers={'X-Verdict-Setup': token}).json()
        assert replay['savedResult'] == result
    assert provider.calls == 1


def test_ordinary_scoped_rubric_has_one_safe_identity_before_and_after_persistence(tmp_path, monkeypatch):
    import verdict.dashboard.scoped_evaluation as scoped
    path = tmp_path / 'ordinary-safe-rubric.db'
    url = f'sqlite:///{path}'
    storage = SQLiteStorage(str(path))
    storage.replace_agent_run_bundle(_bundle())
    storage.close()
    config = _config()
    config['rubric']['dimensions'][0]['description'] = 'The answer must meet the request. Contact audit.person@example.com.'
    config['rubric']['dimensions'][0]['privateNotes'] = {'opaque': 'synthetic-unrecognized-private-canary'}
    provider = MockProvider()
    monkeypatch.setattr(scoped, '_provider', lambda _name: provider)
    with TestClient(create_app(storage=url, tenant_id='local')) as client:
        token = client.get('/api/setup/token').json()['setupToken']
        headers = {'X-Verdict-Setup': token}
        preview = client.post('/api/scoped/preview', json=config, headers=headers).json()
        approved = {**config, 'planFingerprint': preview['planFingerprint'], 'confirmExternalEgress': True}
        result = client.post('/api/scoped/run', json=approved, headers=headers).json()
        assert result['status'] == 'completed'
        saved = client.get('/api/scoped/result/' + result['resultId']).json()
        assert saved['stale'] is False
        assert client.post('/api/scoped/run', json=approved, headers=headers).json() == result
        assert provider.calls == 1
        assert 'audit.person' not in json.dumps(preview)
        assert 'privateNotes' not in json.dumps(preview)
        assert 'synthetic-unrecognized-private-canary' not in json.dumps(result)


def test_sensitive_embedded_identifier_withholds_before_reference_encoding(tmp_path):
    from urllib.parse import unquote
    storage = SQLiteStorage(str(tmp_path / 'identity-privacy.db'))
    bundle = history_bundle()
    bundle = replace(bundle, session=replace(bundle.session, source_session_id='audit.person@example.com'),
                     run=replace(bundle.run, source_session_id='audit.person@example.com'))
    storage.replace_agent_run_bundle(bundle)
    preview = preview_scoped(storage, tenant='local', config=audit_config(authority='public_knowledge'))
    assert preview['coverage'] == 'complete'
    assert preview['plannedCalls'] == 0
    assert preview['unavailableReason'] == 'identifier_requires_redaction'
    assert 'audit.person' not in unquote(json.dumps(preview))
    storage.close()


def test_sensitive_lookup_selector_is_rejected_before_egress(tmp_path):
    storage = SQLiteStorage(str(tmp_path / 'selector-privacy.db'))
    bundle = history_bundle()
    bundle = replace(bundle, session=replace(bundle.session, source_session_id='audit.person@example.com'),
                     run=replace(bundle.run, source_session_id='audit.person@example.com'))
    storage.replace_agent_run_bundle(bundle)
    config = {**audit_config(), 'scope': 'session', 'sourceKind': 'synthetic', 'sessionId': 'audit.person@example.com'}
    config.pop('runId')
    with pytest.raises(ValueError, match='identifiers require redaction'):
        preview_scoped(storage, tenant='local', config=config, storage_url=f'sqlite:///{tmp_path / "selector-privacy.db"}')
    storage.close()


@pytest.mark.parametrize('finding_ids', [
    ('review.finding.0@example.com', 'review.finding.1@example.com'),
    ('review.finding.0@example.com', 'f2'),
    ('4111111111111111', '5555555555554444'),
    ('api_key=opaque-audit-canary', 'f2'),
])
def test_sensitive_finding_ids_are_saved_as_technical_errors_without_identity_collapse(tmp_path, finding_ids):
    def mutate(reply, evidence):
        for finding, identity in zip(reply['findings'], finding_ids, strict=True):
            finding['id'] = identity
    provider = AuditProvider(('FAIL', 'PASS', 'FAIL'), mutate=mutate)
    result, _ = run_audit(tmp_path, provider, answers=('2 + 3 = 6.', '2 + 3 = 5.', '2 + 3 = 7.'))
    assert result['status'] == 'error'
    assert result['audit'] is None
    assert result['dimensions'] == []
    path = tmp_path / 'audit.db'
    with TestClient(create_app(storage=f'sqlite:///{path}', tenant_id='local')) as client:
        saved = client.get('/api/scoped/result/' + result['resultId'])
        assert saved.status_code == 200
        assert saved.json()['status'] == 'error'
        assert saved.json()['audit'] is None
        assert all(identity not in saved.text for identity in finding_ids if identity != 'f2')
    assert provider.calls == 1
