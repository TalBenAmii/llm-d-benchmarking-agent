"""Shared LLM-provider auth check for the opt-in live evals (``tests/eval`` + ``tests/flows``).

The same provider-agnostic guard was copy-pasted across the three live-eval modules; it lives here
once. The key-based provider (anthropic) raises ``ProviderError`` without its key, while the keyless
one (``claude-agent-sdk`` — auth via the logged-in ``claude`` CLI) constructs successfully, so this
gates on "can the configured provider be built", not on a specific key being set.
"""
from __future__ import annotations

from app.config import get_settings
from app.llm.provider import ProviderError, get_provider


def has_auth() -> bool:
    """True when the configured provider can be built — i.e. auth is in place."""
    try:
        get_provider(get_settings())
        return True
    except ProviderError:
        return False
