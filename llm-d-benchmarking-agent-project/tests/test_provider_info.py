"""The header LLM badge's data source: the ``provider_view`` helper + the ``/api/provider``
endpoint — provider/model resolved from settings per route, ``configured`` False when the
provider failed to build, and NEVER anything beyond those three fields (no keys, no account
identity — the payload feeds an unauthenticated-by-default browser page)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.web import provider_view

_BADGE_FIELDS = {"provider", "model", "configured"}


def test_provider_view_resolves_model_per_route():
    s = get_settings()
    cases = {
        "claude-agent-sdk": s.agent_sdk_model,
        "agent-sdk": s.agent_sdk_model,
        "claude-max": s.agent_sdk_model,
        "openai": s.openai_model,
        "openai-compatible": s.openai_model,
        "vllm": s.openai_model,
        "anthropic": s.anthropic_model,
    }
    for provider, model in cases.items():
        view = provider_view(s.model_copy(update={"llm_provider": provider}), None)
        assert view == {"provider": provider, "model": model, "configured": True}


def test_provider_view_normalizes_and_defaults():
    s = get_settings()
    # Case-insensitive (get_provider lower-cases too) and empty → the anthropic default.
    assert provider_view(s.model_copy(update={"llm_provider": "Claude-Agent-SDK"}), None)[
        "model"
    ] == s.agent_sdk_model
    view = provider_view(s.model_copy(update={"llm_provider": ""}), None)
    assert view["provider"] == "anthropic" and view["model"] == s.anthropic_model


def test_provider_view_error_state_and_minimal_payload():
    s = get_settings()
    view = provider_view(s, "ANTHROPIC_API_KEY is not set")
    assert view["configured"] is False
    # The error TEXT (which can name env vars) must not leak; only the three badge fields.
    assert set(view) == _BADGE_FIELDS


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_api_provider_endpoint():
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/provider")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == _BADGE_FIELDS
        assert body["provider"] == (get_settings().llm_provider or "anthropic").lower()
        assert isinstance(body["configured"], bool)
