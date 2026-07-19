"""The header LLM badge + model-picker data source: the ``provider_view`` helper + the
``/api/provider`` endpoint. The SDK-native engine runs ONLY on the Claude Agent SDK, so a
supported LLM_PROVIDER yields the full switchable view (model, effort, served catalog) and any
other name reads as unconfigured (``configured: False``, no model attributed) — and NEVER
anything beyond the six documented fields (no keys, no account identity — the payload feeds an
unauthenticated-by-default browser page)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.llm.model_catalog import AGENT_SDK_PROVIDERS, model_views
from app.web import provider_view

_BADGE_FIELDS = {"provider", "model", "configured"}
# The picker fields added on top of the badge three; the payload is EXACTLY these six.
_ALL_FIELDS = _BADGE_FIELDS | {"switchable", "effort", "models"}


def test_alias_set_pinned():
    # Deliberately spelled out (not derived): an alias vanishing from the supported set should
    # fail HERE, loudly, forcing a conscious update of the badge expectations.
    assert {"claude-agent-sdk", "agent-sdk", "claude-max"} == AGENT_SDK_PROVIDERS


def test_provider_view_supported_aliases_carry_the_full_view():
    s = get_settings()
    for provider in AGENT_SDK_PROVIDERS:
        view = provider_view(s.model_copy(update={"llm_provider": provider}))
        assert view == {
            "provider": provider, "model": s.agent_sdk_model, "configured": True,
            "switchable": True,
            "effort": s.agent_sdk_effort,
            "models": model_views(s.agent_sdk_model),
        }


def test_provider_view_normalizes_and_defaults():
    s = get_settings()
    # Case-insensitive, and empty → the claude-agent-sdk default.
    assert provider_view(s.model_copy(update={"llm_provider": "Claude-Agent-SDK"}))[
        "model"
    ] == s.agent_sdk_model
    view = provider_view(s.model_copy(update={"llm_provider": ""}))
    assert view["provider"] == "claude-agent-sdk" and view["configured"] is True


@pytest.mark.parametrize("provider", ["anthropic", "grok", "openai"])
def test_provider_view_unsupported_provider_is_unconfigured(provider):
    # The engine can't run on this provider, so no model is attributed (showing a concrete
    # model id for a broken provider would mislead) and nothing is switchable.
    s = get_settings()
    view = provider_view(s.model_copy(update={"llm_provider": provider}))
    assert view == {"provider": provider, "model": None, "configured": False,
                    "switchable": False, "effort": None, "models": []}


def test_provider_view_minimal_payload():
    # EXACTLY the six documented fields — never a key or error text.
    s = get_settings()
    for provider in ("claude-agent-sdk", "anthropic"):
        assert set(provider_view(s.model_copy(update={"llm_provider": provider}))) == _ALL_FIELDS


def test_provider_view_switchable_carries_catalog():
    # The served catalog ALWAYS includes the configured default model (so the real active model
    # is selectable), each entry in the {id,label,efforts} wire shape.
    s = get_settings().model_copy(update={"llm_provider": "claude-agent-sdk"})
    view = provider_view(s)
    assert view["switchable"] is True
    assert view["effort"] == s.agent_sdk_effort
    ids = [m["id"] for m in view["models"]]
    assert s.agent_sdk_model in ids
    for m in view["models"]:
        assert set(m) == {"id", "label", "efforts"} and isinstance(m["efforts"], list)


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
        assert body["configured"] is True
        # Switchable → the pinned default (unknown to the catalog) is synthesized into the served
        # list so it stays selectable, and the configured effort rides along.
        assert body["switchable"] is True
        assert body["effort"] == fixed.agent_sdk_effort
        assert "pin-model-x" in [m["id"] for m in body["models"]]
