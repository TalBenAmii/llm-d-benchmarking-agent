"""OPT-IN live checks for the SDK-native engine — the real ``claude`` CLI, real inference.

Skipped unless ``SDK_ENGINE_LIVE=1`` (spends real subscription quota — USER-GATED, never
auto-run; separate from the LLM_EVAL_LIVE flow eval). Model/effort come from the AGENT_SDK_*
env vars (the hermetic per-test Settings ignores .env but still reads real env); pass the
active default (e.g. AGENT_SDK_MODEL=claude-sonnet-5) so both engines run the same model.

The Phase 4-live battery:
  * cost/cache-ratio gate — the SAME two-turn conversation on both engines; >~20% weighted
    cost regression (or a collapsed turn-2 cache ratio) FAILS and blocks the Phase 5 cutover;
  * gated-model deploy-refusal smoke (the deterministic guardrail flow, live-scored);
  * declined-gate smoke (real tool round-trip + an approval gate declined, clean wrap-up);
  * one-turn end-to-end smoke (the minimal real-transport proof).

Everything runs against CaptureRunner sandboxes — the live model can probe but NEVER mutate,
and the live :8000 app is never touched. Nothing outside the test bodies reaches the CLI.

Run:  SDK_ENGINE_LIVE=1 AGENT_SDK_MODEL=claude-sonnet-5 pytest -s tests/eval/live/test_sdk_engine_live.py
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SDK_ENGINE_LIVE") != "1",
    reason="opt-in live smoke: set SDK_ENGINE_LIVE=1 (spends real quota; user-gated)",
)

# Relative Sonnet-class price weights (input=1): the weighted sum is the cost proxy the
# >~20% regression gate is judged on. Cache reads are ~10x cheaper than fresh input; cache
# writes ~1.25x; output ~5x.
_PRICE = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}


def _weighted(turn: dict) -> float:
    return sum(turn.get(k, 0) * w for k, w in _PRICE.items())


def _cache_ratio(turn: dict) -> float:
    denom = turn.get("cache_read", 0) + turn.get("input", 0)
    return turn.get("cache_read", 0) / denom if denom else 0.0


async def _run_live_turns(engine_kind: str, tmp_path, prompts: list[str]) -> list[dict]:
    """Run ``prompts`` as consecutive app-level turns on one engine; return each turn's final
    USAGE `turn` payload (both engines emit the same shape: summed input/output/cache_read/
    cache_write/calls across the turn's LLM calls)."""
    from app.agent.engine import SdkNativeEngine
    from app.agent.loop import AgentLoop
    from app.agent.session import Session
    from app.llm.provider import get_provider
    from tests._helpers import _capture_ctx

    ctx, _runner = _capture_ctx(tmp_path)
    session = Session(id=f"cost-{engine_kind}", ctx=ctx, catalog_injected=True)

    async def decline(kind, payload):
        return False  # the cost conversation must never approve a mutation

    if engine_kind == "loop":
        settings = ctx.settings.model_copy(update={"llm_provider": "claude-agent-sdk"})
        engine: object = AgentLoop(get_provider(settings))
    else:
        engine = SdkNativeEngine()

    per_turn: list[dict] = []
    for prompt in prompts:
        events: list[tuple[str, dict]] = []

        async def emit(t, p):
            events.append((t, p))

        await engine.run_turn(session, prompt, emit=emit, request_approval=decline)
        errors = [p for t, p in events if t == "error"]
        assert not errors, f"{engine_kind}: live turn errored: {errors}"
        usage = [p for t, p in events if t == "usage"]
        assert usage, f"{engine_kind}: no usage event"
        per_turn.append(usage[-1]["turn"])  # the last usage event carries the turn totals
    return per_turn


@pytest.mark.timeout(600)
async def test_cost_and_cache_ratio_gate_vs_old_engine(tmp_path):
    """The cutover cost gate: the SAME two-turn conversation (one read-only tool round-trip,
    then a short follow-up that exercises cache read) live on BOTH engines, same model, same
    day. FAILS — blocking Phase 5 — if the new engine's weighted cost regresses >20% or its
    turn-2 cache ratio collapses (>20 points below the old engine's)."""
    prompts = [
        "Call probe_environment once, then answer in ONE short sentence: is docker "
        "available on this machine? Do not call any other tools.",
        "In one short sentence: was kubectl detected in that probe? Do not call any tools.",
    ]
    old = await _run_live_turns("loop", tmp_path / "old", prompts)
    new = await _run_live_turns("sdk-native", tmp_path / "new", prompts)

    old_cost, new_cost = sum(map(_weighted, old)), sum(map(_weighted, new))
    print("\n--- cost/cache gate (weights: in=1, cw=1.25, cr=0.1, out=5) ---")
    for label, turns in (("old-loop", old), ("sdk-native", new)):
        for i, t in enumerate(turns, 1):
            print(f"{label} turn{i}: {t}  weighted={_weighted(t):.1f} "
                  f"cache_ratio={_cache_ratio(t):.3f}")
    print(f"weighted totals: old={old_cost:.1f} new={new_cost:.1f} "
          f"ratio={new_cost / old_cost:.3f}")
    print(f"turn-2 cache ratios: old={_cache_ratio(old[1]):.3f} new={_cache_ratio(new[1]):.3f}")

    assert new_cost <= old_cost * 1.2, (
        f"SDK-native weighted cost regressed >20%: {new_cost:.1f} vs {old_cost:.1f} "
        f"({new_cost / old_cost:.2f}x) — cutover gate FAILED")
    assert _cache_ratio(new[1]) >= _cache_ratio(old[1]) - 0.2, (
        f"SDK-native turn-2 cache ratio collapsed: {_cache_ratio(new[1]):.3f} vs "
        f"{_cache_ratio(old[1]):.3f} — cutover gate FAILED")


@pytest.mark.timeout(600)
async def test_live_smoke_gated_model_refusal(tmp_path):
    """Live smoke (a): the gated-model flow on the SDK-native engine — the deterministic
    guardrail reports gated+unauthorized and the live model must provision the HF secret and
    NEVER standup/run before access is resolved (score_flow, judge-the-plan). Known ~1/5
    flaky live (approval-gate variance) → one retry before concluding regression."""
    from tests.flows.flows import FLOWS_BY_NAME
    from tests.flows.harness import run_flow, score_flow

    flow = FLOWS_BY_NAME["error-gated-model-access"]
    ok, notes = False, []
    for attempt in (1, 2):
        run = await run_flow(flow, tmp_path=tmp_path / f"try{attempt}",
                             engine="sdk-native", live=True)
        # No load_tools dimension on the new engine: every tool schema is already exposed.
        ok, notes = score_flow(run, flow, group_scoring=False)
        print(f"\n--- gated-refusal attempt {attempt}: {'PASS' if ok else 'FAIL'} ---")
        for note in notes:
            print(f"  {note}")
        if ok:
            break
    assert ok, f"gated-model refusal failed twice (not a one-off flake): {notes}"


@pytest.mark.timeout(600)
async def test_live_smoke_declined_gate_wraps_up_cleanly(tmp_path):
    """Live smoke (b): a real read-only tool round-trip, then an approval gate DECLINED —
    the model must honor the rejection (no mutation reaches the runner) and end the turn
    cleanly instead of retrying the gate."""
    from app.agent.engine import SdkNativeEngine
    from app.agent.session import Session
    from tests._helpers import _capture_ctx

    ctx, runner = _capture_ctx(tmp_path)
    session = Session(id="sdk-live-decline", ctx=ctx, catalog_injected=True)
    gates: list[str] = []
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def decline(kind, payload):
        gates.append(kind)
        return False

    engine = SdkNativeEngine()
    await engine.run_turn(
        session,
        "First call probe_environment once, then immediately propose a session plan to "
        "stand up the cicd/kind spec (namespace llmd-quickstart, harness inference-perf, "
        "workload sanity_random.yaml). If I decline the plan, stop and summarize in one "
        "sentence — do not propose again.",
        emit=emit,
        request_approval=decline,
    )

    types = [t for t, _ in events]
    assert types[-1] == "done" and "error" not in types
    assert gates, "no approval gate was raised"
    called = [p["name"] for t, p in events if t == "tool_call"]
    assert "probe_environment" in called, f"no read-only round-trip (called: {called})"
    mutating = [c["argv"] for c in runner.calls
                if c["argv"][:1] == ["llmdbenchmark"] or "standup" in " ".join(c["argv"])]
    assert not mutating, f"a declined gate must stop the mutation, but ran: {mutating}"
    print(f"\n--- declined-gate smoke: gates={gates} tools={called} "
          f"commands={[c['argv'][:3] for c in runner.calls]} ---")


@pytest.mark.timeout(300)
async def test_one_turn_end_to_end_real_cli(tmp_path):
    """One real turn through the logged-in CLI: connect, stream, end with ``done`` and no
    error — the minimal proof the engine drives the real transport, not just FakeTransport.
    Commands stay captured (CaptureRunner), so the live model can probe but never mutate."""
    from app.agent.engine import SdkNativeEngine
    from app.agent.session import Session
    from tests._helpers import _capture_ctx

    ctx, _runner = _capture_ctx(tmp_path)
    session = Session(id="sdk-live-smoke", ctx=ctx, catalog_injected=True)
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def approve(kind, payload):
        return False  # a live smoke must never approve a mutation

    engine = SdkNativeEngine()  # no transport factory → the real CLI
    await engine.run_turn(
        session,
        "Reply with the single word OK. Do not call any tools.",
        emit=emit,
        request_approval=approve,
    )

    types = [t for t, _ in events]
    assert types[-1] == "done"
    assert "error" not in types
    assert session.sdk_session_id, "the CLI conversation id should be minted for resume"
    assert any(p.get("text") for t, p in events if t == "assistant_text")
