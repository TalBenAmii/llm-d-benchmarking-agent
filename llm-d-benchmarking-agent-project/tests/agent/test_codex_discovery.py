"""Model discovery consumes every SDK page without leaking account metadata."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.llm import codex_options


def model(name, *, hidden=False):
    return {'model': name, 'displayName': name, 'hidden': hidden,
            'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}


class CatalogClient:
    def __init__(self, pages):
        self.pages = pages
        self.cursors = []
        self.closed = False

    def start(self):
        pass

    def initialize(self):
        pass

    def account_read(self):
        return SimpleNamespace(model_dump=lambda **_: {
            'account': {'type': 'chatgpt', 'email': 'private@example.com'}})

    def request(self, method, params, *, response_model):
        assert method == 'model/list'
        assert params['includeHidden'] is False
        self.cursors.append(params['cursor'])
        page = self.pages[params['cursor']]
        if isinstance(page, Exception):
            raise page
        return response_model.model_validate(page)

    def close(self):
        self.closed = True


async def test_discovery_reads_all_pages_and_filters_hidden_models(monkeypatch):
    client = CatalogClient({
        None: {'data': [model('older-model')], 'nextCursor': 'next'},
        'next': {'data': [model('gpt-6.2-sol'), model('gpt-6.2-astra'),
                          model('older-model'), model('hidden-model', hidden=True)]},
    })
    monkeypatch.setattr(codex_options, 'CodexClient', lambda _: client)
    result = await codex_options.discover_codex(get_settings())
    assert result == {'authenticated': True, 'models': [
        {'id': name, 'label': name, 'efforts': ['high']}
        for name in ['older-model', 'gpt-6.2-sol', 'gpt-6.2-astra']
    ]}
    assert client.cursors == [None, 'next']
    assert client.closed


@pytest.mark.parametrize('last_page', [
    RuntimeError('private account or transport detail'),
    {'data': [], 'nextCursor': 'next'},
])
async def test_incomplete_catalog_is_not_returned_as_success(monkeypatch, last_page):
    client = CatalogClient({
        None: {'data': [model('older-model')], 'nextCursor': 'next'},
        'next': last_page,
    })
    monkeypatch.setattr(codex_options, 'CodexClient', lambda _: client)
    assert await codex_options.discover_codex(get_settings()) == {
        'authenticated': False, 'models': []}
    assert client.closed
