"""The header LLM badge + model-picker data source: the ``provider_view`` helper + the
``/api/provider`` endpoint — provider/model resolved from settings per route, ``model: None`` for a
provider name ``get_provider`` would refuse, ``configured`` False when the provider failed to build,
plus the switch fields (``switchable``/``effort``/``models``) that drive the UI model picker — and
NEVER anything beyond those six fields (no keys, no account identity — the payload feeds an
unauthenticated-by-default browser page)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.llm.model_catalog import model_views
from app.llm.provider import AGENT_SDK_PROVIDERS
from app.web import provider_view

_BADGE_FIELDS = {"provider", "model", "configured"}
# The picker fields added on top of the legacy three; the payload is EXACTLY these six.
_ALL_FIELDS = _BADGE_FIELDS | {"switchable", "effort", "models"}

# Deliberately spelled out (not derived from the constants): an alias vanishing from the
# dispatcher should fail HERE, loudly, not silently shrink the loop below.
_ROUTE_TO_MODEL_ATTR = {
    "claude-agent-sdk": "agent_sdk_model",
    "agent-sdk": "agent_sdk_model",
    "claude-max": "agent_sdk_model",
    "anthropic": "anthropic_model",
}


def test_alias_tables_in_sync_with_dispatcher():
    # provider_view shares get_provider's constants; this pins the constants themselves so a
    # new alias forces a conscious update of the badge expectations (and this table).
    assert set(_ROUTE_TO_MODEL_ATTR) == AGENT_SDK_PROVIDERS | {"anthropic"}


def test_provider_view_resolves_model_per_route():
    s = get_settings()
    for provider, model_attr in _ROUTE_TO_MODEL_ATTR.items():
        view = provider_view(s.model_copy(update={"llm_provider": provider}), None)
        switchable = provider in AGENT_SDK_PROVIDERS
        assert view == {
            "provider": provider, "model": getattr(s, model_attr), "configured": True,
            # Only the agent-SDK path is switchable: it carries the configured effort + served
            # catalog; every other provider reports switchable False / no effort / no models.
            "switchable": switchable,
            "effort": s.agent_sdk_effort if switchable else None,
            "models": model_views(s.agent_sdk_model) if switchable else [],
        }


def test_provider_view_normalizes_and_defaults():
    s = get_settings()
    # Case-insensitive (get_provider lower-cases too) and empty → the anthropic default.
    assert provider_view(s.model_copy(update={"llm_provider": "Claude-Agent-SDK"}), None)[
        "model"
    ] == s.agent_sdk_model
    view = provider_view(s.model_copy(update={"llm_provider": ""}), None)
    assert view["provider"] == "anthropic" and view["model"] == s.anthropic_model


def test_provider_view_unknown_provider_has_no_model():
    # get_provider RAISES for this name, so no model was ever resolved — attributing one
    # (e.g. the anthropic default) would show a concrete model id for a broken provider. An
    # unknown provider is not switchable: no effort, no models.
    s = get_settings()
    view = provider_view(s.model_copy(update={"llm_provider": "grok"}), "unknown LLM_PROVIDER")
    assert view == {"provider": "grok", "model": None, "configured": False,
                    "switchable": False, "effort": None, "models": []}


def test_provider_view_error_state_and_minimal_payload():
    s = get_settings()
    view = provider_view(s, "ANTHROPIC_API_KEY is not set")
    assert view["configured"] is False
    # The error TEXT (which can name env vars) must not leak; only the six documented fields.
    assert set(view) == _ALL_FIELDS


def test_provider_view_agent_sdk_switchable_carries_catalog():
    # The switchable agent-SDK path: switchable True, the configured effort, and a served catalog
    # that ALWAYS includes the configured default model (so the real active model is selectable).
    s = get_settings().model_copy(update={"llm_provider": "claude-agent-sdk"})
    view = provider_view(s, None)
    assert view["switchable"] is True
    assert view["effort"] == s.agent_sdk_effort
    ids = [m["id"] for m in view["models"]]
    assert s.agent_sdk_model in ids
    # Each entry is the {id,label,efforts} wire shape.
    for m in view["models"]:
        assert set(m) == {"id", "label", "efforts"} and isinstance(m["efforts"], list)


def test_provider_view_non_switchable_has_empty_models():
    s = get_settings()
    for provider in ("anthropic",):
        view = provider_view(s.model_copy(update={"llm_provider": provider}), None)
        assert view["switchable"] is False
        assert view["effort"] is None
        assert view["models"] == []


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_api_provider_endpoint(monkeypatch):
    # Pin the route end-to-end with a KNOWN settings object (the app.main.get_settings
    # monkeypatch is the seam app/web.py documents) — asserting against the ambient .env
    # would just recompute the implementation's own expression.
    import app.main as main_mod

    fixed = get_settings().model_copy(
        update={"llm_provider": "Claude-Agent-SDK", "agent_sdk_model": "pin-model-x"}
    )
    with TestClient(main_mod.app) as client:
        monkeypatch.setattr(main_mod, "get_settings", lambda: fixed)
        resp = client.get("/api/provider")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == _ALL_FIELDS
        assert body["provider"] == "claude-agent-sdk"  # normalized
        assert body["model"] == "pin-model-x"
        assert isinstance(body["configured"], bool)  # from real startup state
        # Switchable → the pinned default (unknown to the catalog) is synthesized into the served
        # list so it stays selectable, and the configured effort rides along.
        assert body["switchable"] is True
        assert body["effort"] == fixed.agent_sdk_effort
        assert "pin-model-x" in [m["id"] for m in body["models"]]
