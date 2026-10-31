import json
import re

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


def recover_embedded(value):
    """Try the supported quoted-string and Unicode layers in surrounding prose."""
    pending = [value]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, str) or current in seen:
            continue
        seen.add(current)
        if SYNTHETIC_KEY in current:
            return True
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), current)
        if decoded != current:
            pending.append(decoded)
        for match in re.finditer(r'"(?:\\.|[^"\\])*"', current):
            try:
                pending.append(json.loads(match.group()))
            except ValueError:
                pass
    return False


@pytest.mark.parametrize('level', [0, 1, 2, 3])
@pytest.mark.parametrize('mode', ['redact', 'hash'])
def test_encoded_credential_in_serialized_prose_fragment(level, mode):
    fragment = ''.join(f'\\u{ord(c):04x}' for c in SYNTHETIC_KEY[:3]) + SYNTHETIC_KEY[3:]
    for _ in range(level):
        fragment = json.dumps(fragment)
    raw = 'Synthetic tool returned ' + fragment + '; inspect the result.'
    clean = redact_structure(raw, mode=mode, secret='synthetic-test-key' if mode == 'hash' else None)
    assert not recover_embedded(clean)
    assert 'Synthetic tool returned ' in clean and '; inspect the result.' in clean
    assert redact_structure(clean, mode=mode,
                            secret='synthetic-test-key' if mode == 'hash' else None) == clean


@pytest.mark.parametrize('shape', ['unclosed', 'double_bare', 'double_unclosed'])
def test_malformed_or_bare_double_escaped_credential_is_not_recoverable(shape):
    fragment = ''.join(f'\\u{ord(c):04x}' for c in SYNTHETIC_KEY[:3]) + SYNTHETIC_KEY[3:]
    if shape == 'unclosed':
        fragment = json.dumps(fragment)[:-1]
    elif shape == 'double_bare':
        fragment = fragment.replace('\\', '\\\\')
    else:
        fragment = json.dumps(json.dumps(fragment))[:-1]
    clean = redact_structure('Begin ' + fragment + '; continue.')
    assert not recover_embedded(clean)
    assert redact_structure(clean) == clean


def test_many_serialized_fragments_fail_closed_at_work_limit():
    raw = 'prefix ' + ('"\\u0061" ' * 4097)
    assert redact_structure(raw) == '<REDACTED>'


def test_plain_quotes_do_not_consume_encoded_fragment_budget():
    raw = ('"plain" ' * 4097) + r'caf\u00e9 https://example.invalid'
    clean = redact_structure(raw)
    assert clean.startswith('"plain" ')
    assert clean.endswith('café <URL>')


def test_oversize_serialized_prose_fails_closed():
    raw = 'prefix "\\u0061" ' + 'a' * 1_000_000
    assert redact_structure(raw) == '<REDACTED>'

@pytest.mark.parametrize('level', [1, 2, 3, 4])
@pytest.mark.parametrize('mode', ['redact', 'hash'])
def test_encoded_credentials_remain_unrecoverable(level, mode):
    raw = encoded_secret(level)
    clean = redact_structure(raw, mode=mode, secret='synthetic-test-key' if mode == 'hash' else None)
    assert recover(clean) != SYNTHETIC_KEY


@pytest.mark.parametrize('level', [1, 2, 3, 4])
@pytest.mark.parametrize('mode', ['redact', 'hash'])
def test_encoded_credentials_in_mapping_keys_remain_unrecoverable(level, mode):
    key = encoded_secret(level)
    clean = redact_structure(
        {key: 'benign'}, mode=mode,
        secret='synthetic-test-key' if mode == 'hash' else None,
    )
    assert all(recover(sanitized_key) != SYNTHETIC_KEY for sanitized_key in clean)


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
