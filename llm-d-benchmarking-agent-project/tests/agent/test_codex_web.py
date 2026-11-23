"""Provider metadata, readiness and model selection use the signed-in Codex catalog."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.web import provider_view

MODELS = [{'id': 'test-codex', 'label': 'Test Codex', 'efforts': ['low', 'high']}]


@pytest.mark.parametrize('authenticated', [False, True])
def test_codex_provider_metadata_has_no_identity(authenticated):
    settings = get_settings().model_copy(update={'llm_provider': 'codex-sdk',
                                                 'codex_model': 'test-codex'})
    view = provider_view(settings, {'authenticated': authenticated, 'models': MODELS,
                                    'email': 'private@example.com', 'token': 'secret'})
    assert view == {'provider': 'codex-sdk', 'model': 'test-codex',
                    'configured': authenticated, 'switchable': True,
                    'effort': settings.codex_effort, 'models': MODELS}


@pytest.mark.parametrize('authenticated', [False, True])
def test_readiness_checks_codex_login(monkeypatch, tmp_path, authenticated):
    import app.main as main

    settings = get_settings().model_copy(update={'llm_provider': 'codex-sdk',
                                                 'workspace_dir': tmp_path})
    monkeypatch.setattr(main, 'get_settings', lambda: settings)

    async def discover(_):
        return {'authenticated': authenticated, 'models': MODELS if authenticated else []}

    monkeypatch.setattr(main, 'discover_codex', discover)
    with TestClient(main.app) as client:
        assert client.get('/healthz').status_code == 200
        response = client.get('/readyz')
        assert response.status_code == (200 if authenticated else 503)
        assert response.json()['codex_authenticated'] is authenticated
        assert client.get('/api/provider').json()['configured'] is authenticated


def test_websocket_selects_codex_models_only(monkeypatch, tmp_path):
    import app.main as main

    settings = get_settings().model_copy(update={'llm_provider': 'codex-sdk',
                                                 'workspace_dir': tmp_path})
    monkeypatch.setattr(main, 'get_settings', lambda: settings)

    async def discover(_):
        return {'authenticated': True, 'models': MODELS}

    monkeypatch.setattr(main, 'discover_codex', discover)
    with TestClient(main.app) as client, client.websocket_connect('/ws') as ws:
        ready = ws.receive_json()
        sid = ready['data']['session_id']
        ws.send_json({'type': 'set_model', 'model': 'test-codex', 'effort': 'high'})
        ws.send_json({'type': 'ping'})
        while ws.receive_json()['type'] != 'pong':
            pass
        assert main.app.state.sessions.get_or_load(sid).model_override == 'test-codex'
        ws.send_json({'type': 'set_model', 'model': 'claude-sonnet-5', 'effort': 'high'})
        while True:
            message = ws.receive_json()
            if message['type'] == 'error':
                assert message['data']['kind'] == 'protocol_error'
                break
        assert main.app.state.sessions.get_or_load(sid).model_override == 'test-codex'


def test_catalog_refresh_makes_new_models_selectable_without_restart(monkeypatch, tmp_path):
    import app.main as main

    settings = get_settings().model_copy(update={'llm_provider': 'codex-sdk',
                                                 'workspace_dir': tmp_path})
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    newest = {'id': 'gpt-6.2-sol', 'label': 'GPT-6.2-Sol', 'efforts': ['low', 'high']}
    no_effort = {'id': 'future-model', 'label': 'Future model', 'efforts': []}
    calls = []

    async def discover(_):
        calls.append(True)
        models = MODELS if len(calls) == 1 else [newest, no_effort, *MODELS]
        return {'authenticated': True, 'models': models}

    monkeypatch.setattr(main, 'discover_codex', discover)
    with TestClient(main.app) as client, client.websocket_connect('/ws') as ws:
        sid = ws.receive_json()['data']['session_id']
        assert client.get('/api/provider').json()['models'] == MODELS
        assert len(calls) == 1
        main.app.state.codex_catalog_checked_at -= main._CODEX_CATALOG_TTL + 1
        assert client.get('/api/provider').json()['models'] == [newest, no_effort, *MODELS]
        assert client.get('/api/provider').json()['models'][0] == newest
        assert len(calls) == 2
        for model, effort in [('gpt-6.2-sol', 'high'), ('future-model', None)]:
            ws.send_json({'type': 'set_model', 'model': model, 'effort': effort})
            ws.send_json({'type': 'ping'})
            while ws.receive_json()['type'] != 'pong':
                pass
            session = main.app.state.sessions.get_or_load(sid)
            assert (session.model_override, session.effort_override) == (model, effort)


def test_failed_catalog_refresh_preserves_choices_and_retries_after_ttl(monkeypatch, tmp_path):
    import app.main as main

    settings = get_settings().model_copy(update={'llm_provider': 'codex-sdk',
                                                 'workspace_dir': tmp_path})
    monkeypatch.setattr(main, 'get_settings', lambda: settings)
    calls = []

    async def discover(_):
        calls.append(True)
        if len(calls) == 2:
            return {'authenticated': False, 'models': []}
        return {'authenticated': True, 'models': MODELS}

    monkeypatch.setattr(main, 'discover_codex', discover)
    with TestClient(main.app) as client:
        main.app.state.codex_catalog_checked_at -= main._CODEX_CATALOG_TTL + 1
        assert client.get('/api/provider').json()['models'] == MODELS
        assert client.get('/api/provider').json()['configured']
        assert len(calls) == 2
        main.app.state.codex_catalog_checked_at -= main._CODEX_CATALOG_TTL + 1
        assert client.get('/api/provider').json()['models'] == MODELS
        assert len(calls) == 3
