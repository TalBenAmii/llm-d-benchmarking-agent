"""Shared LLM auth check for the opt-in live evals (``tests/eval`` + ``tests/flows``).

The engine runs on the Claude Agent SDK (auth via the logged-in ``claude`` CLI — keyless), so
"can this box go live" reduces to: is the configured provider one the SDK-native engine
supports? The SDK ships its own bundled CLI, so construction itself can't fail; a missing
login surfaces as the live turn erroring (which the live tests score as a failure).
"""
from __future__ import annotations

from app.config import get_settings
from app.llm.model_catalog import AGENT_SDK_PROVIDERS


def has_auth() -> bool:
    """True when the configured provider is one the SDK-native engine can drive live."""
    provider = (get_settings().llm_provider or "claude-agent-sdk").lower()
    return provider in AGENT_SDK_PROVIDERS
