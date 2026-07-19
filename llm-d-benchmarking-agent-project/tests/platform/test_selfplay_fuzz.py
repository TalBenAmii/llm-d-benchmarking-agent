"""Self-play fuzz / property harness for the agent's HTTP + WebSocket surface.

GOAL (todo item): "Make the agent interact with and view the application and randomly
'play with it' to find bugs." This is a deterministic, *seedable* randomized harness that
drives the real FastAPI app (``app.main:app``) exactly like a user would — over the real
``/ws`` WebSocket and the real ``/api/sessions`` + ``/api/namespaces`` HTTP routes — under
``SIMULATE=1`` with a scripted :class:`FuzzProvider` (no network, no cluster, no live LLM).

It is a PROPERTY test, not an example test: rather than asserting one scripted outcome, it
generates a random sequence of *valid-but-arbitrary* user operations (new chat, send a
message that triggers a scripted multi-tool turn, approve/reject a gate, cancel, disconnect
mid-turn, reconnect at a random point, switch between several concurrent chats, create/delete
namespaces, ping, send a malformed frame) and after EVERY action re-checks a set of
INVARIANTS. A bug in connection-resume / approval-persistence / state-isolation surfaces as a
failing seed with a printed action trace, reproducible by re-running that exact seed.

The reusable mechanism — the :class:`FuzzProvider`, the isolated-state installer, the
:class:`Player` action vocabulary, and the invariant battery — was **factored out** into
``tests/eval/app_driver.py`` (so the LLM-driven exploratory bug-hunter can drive the SAME
real app). This module imports that mechanism and selects each action with a *seeded RNG*
(the deterministic path). Behavior is byte-identical to before the factor-out: identical seed
→ identical action sequence → identical assertions.

Why these substitutions (and only these):
  * ``SimRunner`` (``SIMULATE=1``) → every *mutating command* becomes a synthetic no-op, so a
    standup/run never touches a cluster, yet the agent loop still runs end-to-end. The upfront
    ``propose_session_plan`` approval gate is STILL gated (it is not a command), which is what
    gives us approve/reject/cancel paths to fuzz.
  * a tmp-dir-backed ``SessionManager`` so each fuzz run starts from an empty, isolated session
    store on disk — the "two sessions never share state" + "reload-from-disk matches history"
    invariants are then crisp and independent of any leftover chats.
  * ``FuzzProvider`` — a scripted LLM that, per turn, deterministically (seeded) plays EITHER a
    read-only turn or a turn that calls ``propose_session_plan`` then a mutating
    ``execute_llmdbenchmark`` standup (a real registry tool, valid against the real policy),
    so approval gates actually fire under the real loop.

Everything else is the REAL app: the real ``/ws`` handler, the real ``Channel`` (resume
buffer + pending-approval restore), the real ``SessionManager`` persistence, the real
inbound-frame validation, the real agent loop + tool dispatch + policy.
"""
from __future__ import annotations

import os
import random

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

# The reusable real-app driver (factored out of this module — see tests/eval/app_driver.py).
# Imported under the names this module historically used so its references are unchanged.
from tests.eval.app_driver import Player as _Player
from tests.eval.app_driver import install_isolated_state as _install_isolated_state

# The bench repo must be present (the agent loop reads the live catalog for plan validation).
# In a worktree this is satisfied via REPOS_DIR (see tests/CLAUDE.md); skip cleanly otherwise.
pytestmark = pytest.mark.skipif(
    not get_settings().bench_repo.is_dir(), reason="bench repo not present"
)


# --------------------------------------------------------------------------------------------
# The parametrized property test.
# --------------------------------------------------------------------------------------------

# Fixed seeds → reproducible. A failure prints its seed; re-run that seed to reproduce exactly.
_SEEDS = [1, 7, 13, 16, 42, 101, 777, 2024]
_ACTIONS_PER_RUN = 24  # ~10-40 band; kept modest so the whole parametrization stays a few seconds


@pytest.mark.parametrize("seed", _SEEDS)
def test_selfplay_fuzz(seed: int, tmp_path) -> None:
    """Drive the real app with a seeded random action sequence; assert invariants after each.

    Deterministic: identical seed → identical action sequence → identical assertions. No wall
    clock, no real randomness, no network/cluster/LLM. A violation raises with the seed's full
    action trace so the failing sequence is reproducible.
    """
    from app.main import app

    rng = random.Random(seed)
    with TestClient(app) as client:
        provider = _install_isolated_state(app, tmp_path)
        player = _Player(app, client, provider, rng)
        try:
            for _ in range(_ACTIONS_PER_RUN):
                player.step()
        finally:
            player.finish()


_SDK_SEEDS = [1, 42, 777]


@pytest.mark.parametrize("seed", _SDK_SEEDS)
def test_selfplay_fuzz_sdk_native(seed: int, tmp_path, monkeypatch) -> None:
    """The SAME seeded self-play battery with the app on the SDK-native engine
    (``AGENT_ENGINE=sdk-native`` over a scripted FakeTransport — see
    ``app_driver.SdkFuzzScripts``): the handshake / gate-resume / state-isolation invariants
    must hold identically. A trimmed seed set — the flow corpus carries the exhaustive
    per-flow engine parity; this guards the WS-handler↔engine wiring (steer/cancel/parked
    gates) end to end."""
    from app.main import app

    monkeypatch.setenv("AGENT_ENGINE", "sdk-native")
    rng = random.Random(seed)
    with TestClient(app) as client:
        primer = _install_isolated_state(app, tmp_path, engine="sdk-native")
        player = _Player(app, client, primer, rng)
        try:
            for _ in range(_ACTIONS_PER_RUN):
                player.step()
        finally:
            player.finish()


@pytest.mark.skipif(
    os.environ.get("FUZZ_SOAK") != "1", reason="opt-in soak: set FUZZ_SOAK=1 to run"
)
def test_selfplay_fuzz_soak(tmp_path) -> None:
    """Opt-in longer soak (40 actions × more seeds). Behind a skip so the default suite stays
    fast; set FUZZ_SOAK=1 when you want a deeper pass."""
    from app.main import app

    for seed in range(20):
        rng = random.Random(seed)
        with TestClient(app) as client:
            provider = _install_isolated_state(app, tmp_path / f"s{seed}")
            player = _Player(app, client, provider, rng)
            try:
                for _ in range(40):
                    player.step()
            finally:
                player.finish()
