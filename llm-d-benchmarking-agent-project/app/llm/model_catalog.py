"""The switchable Anthropic model catalog for the chat-UI model picker (agent-SDK provider only).

Pure data + pure helpers: an ordered catalog of Anthropic models the user may switch the running
agent to, each with its human label and the reasoning-effort levels it supports (a subset of the
provider's master ``_EFFORT_LEVELS``). Haiku has no effort control, so its ``efforts`` is empty.

Only the agent-SDK provider is switchable; the served (visible) list is a curated subset PLUS the
configured default model, so the user's real active model always appears. Everything here is
no-I/O and deterministic — the ``/api/provider`` badge (``app.web.provider_view``) reads the served
list, and the ``/ws`` ``set_model`` handler validates a selection against it before storing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The LLM_PROVIDER values that mean "the Claude Agent SDK" — the ONLY supported engine after
# the SDK-native cutover. Anything else fails app readiness with a clear "unsupported provider"
# error (see app/main.py). Kept as a set for the .env aliases users already have.
AGENT_SDK_PROVIDERS: frozenset[str] = frozenset({"claude-agent-sdk", "agent-sdk", "claude-max"})


@dataclass(frozen=True)
class ModelInfo:
    """One switchable model: its api id, the label the picker shows, and the reasoning-effort
    levels it supports (empty ``efforts`` => no effort control, e.g. Haiku)."""
    id: str
    label: str
    efforts: tuple[str, ...]


# Ordered catalog. ``efforts`` are subsets of the provider's master ``_EFFORT_LEVELS``
# (low/medium/high/xhigh/max) — Sonnet 4.6 predates xhigh; Haiku has no effort control at all.
# A test pins each model's efforts as a subset of that master set so the two can't drift.
CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo("claude-opus-4-8", "Opus 4.8", ("low", "medium", "high", "xhigh", "max")),
    ModelInfo("claude-sonnet-5", "Sonnet 5", ("low", "medium", "high", "xhigh", "max")),
    ModelInfo("claude-sonnet-4-6", "Sonnet 4.6", ("low", "medium", "high", "max")),
    ModelInfo("claude-opus-4-7", "Opus 4.7", ("low", "medium", "high", "xhigh", "max")),
    ModelInfo("claude-haiku-4-5", "Haiku 4.5", ()),
)

# The curated set the picker shows by default. The configured default model is folded in on top
# (see ``served_models``) so the real active model is always selectable even when not curated.
_CURATED_IDS: frozenset[str] = frozenset({"claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"})

_BY_ID: dict[str, ModelInfo] = {m.id: m for m in CATALOG}


def served_models(default_id: str) -> list[ModelInfo]:
    """The visible/switchable model list: the curated set PLUS the configured default, in catalog
    order. A default that is in the catalog slots into its canonical position; a default unknown to
    the catalog is synthesized (label = its raw id, no efforts) and appended, so it stays selectable
    but offers no effort switching."""
    included = set(_CURATED_IDS)
    if default_id:
        included.add(default_id)
    out = [m for m in CATALOG if m.id in included]
    if default_id and default_id not in _BY_ID:
        out.append(ModelInfo(id=default_id, label=default_id, efforts=()))
    return out


def valid_selection(model_id: str, effort: str | None, default_id: str) -> ModelInfo | None:
    """Return the served ``ModelInfo`` iff ``model_id`` is in the served allowlist AND ``effort`` is
    valid for it — one of the model's supported efforts, or ``None`` when the model has no efforts
    (e.g. Haiku). Otherwise ``None`` (the caller rejects the frame and keeps the prior selection)."""
    info = next((m for m in served_models(default_id) if m.id == model_id), None)
    if info is None:
        return None
    if info.efforts:
        return info if effort in info.efforts else None
    return info if effort is None else None


def model_views(default_id: str) -> list[dict[str, Any]]:
    """The served list as the ``/api/provider`` wire shape: ``[{id, label, efforts:[...]}, ...]``."""
    return [{"id": m.id, "label": m.label, "efforts": list(m.efforts)} for m in served_models(default_id)]
