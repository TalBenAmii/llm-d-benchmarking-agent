"""(B) Autonomous exploratory bug-hunter — the LLM action selector + the run driver.

Drives the REAL app over the same HTTP+WS surface the deterministic fuzzer drives (reusing
``tests/eval/app_driver.py`` wholesale) in an OPEN-ENDED way: an LLM chooses the next ACTION to
play (the agent itself runs scripted-but-real, so only the explorer LLM spends quota — one
small call per action). The DETERMINISTIC invariant battery is the authoritative bug oracle; a
hit is a real finding. The LLM's role is choice + advisory triage only — it can NEVER flip a
build red (see ``oracle.md`` / ``bug_report.py``).

Reproducibility (the property the deterministic fuzzer has and we preserve): the selector is
PROMPT-SEEDED (seed + action index injected; zero variance asked for) and every chosen action is
LOGGED, so a finding records ``seed`` + ``repro_actions`` and replays through the deterministic
``Player`` with NO LLM. Without ``use_llm``, a DETERMINISTIC fallback selector (the existing
seeded RNG) is used → the bug-hunter degrades to exactly today's fuzzer.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from app.dig import find_last_json

from .app_driver import (
    Player,
    check_isolation,
    check_session_invariants,
    install_isolated_state,
)
from .bug_report import Finding, finding_from_invariant

ORACLE_PATH = Path(__file__).resolve().parent / "oracle.md"

# The action vocabulary the LLM (and the RNG fallback) may choose from — the parameterless
# ``Player.act_*`` methods. (Each method internally seeds its own params from the Player's RNG,
# so the selector picks only the action NAME; this keeps replay deterministic through the same
# Player.) Ordered so the index is stable for the seeded fallback.
ACTION_NAMES = (
    "act_new_chat",
    "act_send_message",
    "act_reconnect_midturn",
    "act_switch_chat",
    "act_cancel",
    "act_ping",
    "act_malformed",
    "act_list_namespaces",
    "act_delete_namespace",
    "act_delete_session",
    "act_set_auto_approve",
    "act_list_jobs",
    "act_reopen_after_delete",
)

# The weighted pool the deterministic fuzzer uses — its fallback selector reproduces it exactly,
# so "no key" degrades to today's seeded fuzzer with no behavior change. MUST stay in lock-step with
# ``Player.step()``'s dispatch table (same actions + weights).
_FALLBACK_WEIGHTS = {
    "act_new_chat": 3, "act_send_message": 6, "act_reconnect_midturn": 3,
    "act_switch_chat": 4, "act_cancel": 2, "act_ping": 2, "act_malformed": 2,
    "act_list_namespaces": 1, "act_delete_namespace": 1, "act_delete_session": 1,
    "act_set_auto_approve": 1, "act_list_jobs": 1, "act_reopen_after_delete": 1,
}


def _collect_invariant_problems(player: Player) -> list[str]:
    """Run the SAME invariant battery ``Player.check_all`` runs, but RETURN the problems instead
    of raising — so the bug-hunter can record a finding (with repro) and keep exploring rather
    than abort on the first hit. The invariant functions are the authoritative oracle."""
    problems: list[str] = []
    for sid in player.session_ids:
        problems += check_session_invariants(player.app, sid)
    problems += check_isolation(player.app, player.session_ids)
    return problems


def _state_summary(player: Player) -> dict[str, Any]:
    """A compact, JSON-safe snapshot of the live driver state for the LLM selector prompt."""
    cur = player._cur_sid  # noqa: SLF001 — test introspection
    s = player.app.state.sessions.get(cur) if cur else None
    return {
        "open_session": cur[:8] if cur else None,
        "n_sessions": len(player.session_ids),
        "namespaces": sorted(player.namespaces),
        "current_busy": player._is_busy(cur),  # noqa: SLF001
        "current_messages": len(s.messages) if s else 0,
        "last_actions": player.trace[-6:],
    }


class DeterministicSelector:
    """The no-LLM fallback: the EXACT seeded weighted choice the fuzzer uses. ``choose`` ignores
    the state summary and returns a method name from the weighted pool via the Player's RNG, so a
    run with this selector is byte-identical to a self-play fuzz run of the same seed."""

    def __init__(self, rng: random.Random):
        self._pool: list[str] = []
        for name, weight in _FALLBACK_WEIGHTS.items():
            self._pool += [name] * weight
        self._rng = rng

    async def choose(self, action_index: int, summary: dict[str, Any]) -> str:
        return self._rng.choice(self._pool)


class LLMActionSelector:
    """Picks the next action via one bare SDK call — PROMPT-SEEDED for reproducibility.

    Each call gets the action vocabulary, a compact state summary, the seed + action index, and
    the oracle's action-selection guidance. The model returns the next action NAME as JSON. Zero
    variance is requested in the prompt (no temperature knob — see judge.py's note). On any
    parse/quota hiccup it falls back to the deterministic selector so a run never wedges."""

    def __init__(self, *, seed: int, rng: random.Random, oracle_body: str):
        self._seed = seed
        self._oracle = oracle_body
        self._fallback = DeterministicSelector(rng)

    def _messages(self, action_index: int, summary: dict[str, Any]) -> tuple[str, str]:
        system = (
            "You are an exploratory tester hunting for STATE-CORRUPTION bugs in a chat app by "
            "choosing the next UI action to play. You do NOT execute anything — you only pick the "
            "next action name; a deterministic driver runs it and an invariant oracle checks it.\n\n"
            "=== ORACLE POLICY ===\n" + self._oracle
        )
        user = (
            f"seed={self._seed} action_index={action_index} (use these to be REPRODUCIBLE — same "
            f"seed+index → same choice).\n"
            f"Available actions: {list(ACTION_NAMES)}\n"
            f"Current state: {json.dumps(summary)}\n\n"
            'Respond with ONLY a JSON object: {"action": "<one action name>"}'
        )
        return system, user

    async def choose(self, action_index: int, summary: dict[str, Any]) -> str:
        from tests.eval._llm import llm_text

        system, user = self._messages(action_index, summary)
        try:
            raw = await llm_text(system, user)
            obj = find_last_json(raw or "", "{")
            action = (obj or {}).get("action") if isinstance(obj, dict) else None
            if action in ACTION_NAMES:
                return action
        except Exception:  # noqa: BLE001 — any model/parse failure degrades, never wedges a run
            pass
        return await self._fallback.choose(action_index, summary)


async def _run_one_seed(
    app, client, tmp_path, seed: int, actions_budget: int, selector_factory,
) -> tuple[list[Finding], int]:
    """Drive ONE seed: install isolated state, build a Player, and for each action ask the
    selector for the next action NAME, play it via the Player, then collect (not raise) any
    invariant violations into deterministic findings. Returns (findings, actions_played)."""
    rng = random.Random(seed)
    primer = install_isolated_state(app, tmp_path)
    player = Player(app, client, primer, rng)
    selector = selector_factory(rng)
    findings: list[Finding] = []
    played = 0
    try:
        for i in range(actions_budget):
            name = await selector.choose(i, _state_summary(player))
            getattr(player, name)()
            played += 1
            for problem in _collect_invariant_problems(player):
                findings.append(finding_from_invariant(
                    problem, seed=seed, action_index=i, repro_actions=list(player.trace),
                ))
    finally:
        player.finish()
    return findings, played


async def run_bughunt(
    app,
    client_factory,
    tmp_path: Path,
    *,
    seeds: list[int],
    actions_budget: int = 30,
    use_llm: bool = False,
) -> tuple[list[Finding], int]:
    """Run the bug-hunt across ``seeds``. ``client_factory`` is a no-arg callable returning a
    fresh ``TestClient`` context manager per seed (the caller owns app import). Without
    ``use_llm`` the DETERMINISTIC fallback selector is used (degrades to the seeded fuzzer).

    Returns (all_findings, total_actions). The CALLER assembles + writes the report and decides
    the gate (only deterministic ``severity >= high`` findings gate). The worst-case quota is
    bounded + printable up front: ``len(seeds) * actions_budget`` selector calls (one per action),
    plus zero without ``use_llm``."""
    oracle_body = ORACLE_PATH.read_text()
    all_findings: list[Finding] = []
    total_actions = 0
    for seed in seeds:
        # Re-seed the selector per seed so its prompt-seed matches the run seed (reproducibility).
        def _seeded_factory(rng: random.Random, _seed=seed):
            if not use_llm:
                return DeterministicSelector(rng)
            return LLMActionSelector(seed=_seed, rng=rng, oracle_body=oracle_body)

        with client_factory() as client:
            findings, played = await _run_one_seed(
                app, client, tmp_path / f"s{seed}", seed, actions_budget, _seeded_factory,
            )
        all_findings += findings
        total_actions += played
    return all_findings, total_actions


def max_selector_calls(seeds: list[int], actions_budget: int, *, has_provider: bool) -> int:
    """The worst-case number of LLM selector calls a run will make (printed up front so the quota
    cost is never a surprise). Zero with the deterministic fallback."""
    return len(seeds) * actions_budget if has_provider else 0
