import json

import pytest
import verdict
from fastapi.testclient import TestClient
from verdict.dashboard.app import create_app
from verdict.redaction import redact_structure
from verdict.storage import SQLiteStorage

SYNTHETIC_KEY = 'sk-' + 'FAKECANARY' * 4

def encoded_secret(level):
    value = ''.join(f'\\u{ord(c):04x}' for c in SYNTHETIC_KEY)
    for _ in range(level - 1):
        value = json.dumps(value)
    return value

def recover(value):
    for _ in range(6):
        if not isinstance(value, str):
            return None
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            break
    if isinstance(value, str):
        return value.encode('ascii').decode('unicode_escape')
    return None

@pytest.mark.parametrize('level', [1, 2, 3, 4])
@pytest.mark.parametrize('mode', ['redact', 'hash'])
def test_encoded_credentials_remain_unrecoverable(level, mode):
    raw = encoded_secret(level)
    clean = redact_structure(raw, mode=mode, secret='synthetic-test-key' if mode == 'hash' else None)
    assert recover(clean) != SYNTHETIC_KEY


@pytest.mark.parametrize('mode', ['redact', 'hash'])
def test_json_whitespace_before_encoded_string_does_not_hide_credential(mode):
    raw = ' \n' + encoded_secret(2) + ' '
    clean = redact_structure(
        raw, mode=mode, secret='synthetic-test-key' if mode == 'hash' else None,
    )
    assert recover(clean) != SYNTHETIC_KEY

@pytest.mark.parametrize('level', [2, 3, 4])
def test_sdk_sqlite_and_run_api_do_not_expose_encoded_credentials(tmp_path, level):
    path = tmp_path / 'synthetic.db'
    storage = SQLiteStorage(str(path))
    verdict.init(storage=storage,tenant_id='__verdict_local__',instrumentors=[])
    try:
        with verdict.agent_run(name='synthetic-encoded-output') as run:
            with run.turn(user_input='Inspect a wholly synthetic encoded value') as turn:
                with turn.tool('synthetic_tool') as tool:
                    tool.set_output(encoded_secret(level))
        [bundle] = storage.list_agent_run_bundles('__verdict_local__')
        with TestClient(create_app(storage=str(path))) as client:
            response = client.get('/api/runs/' + bundle.run.run_id)
        assert response.status_code == 200
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)
        assert all(recover(value) != SYNTHETIC_KEY for value in strings(response.json()))
        result = next(event.attributes['result'] for event in bundle.events
                      if event.event_type.value == 'tool_result')
        assert recover(result) != SYNTHETIC_KEY
    finally:
        verdict.shutdown()
        storage.close()
