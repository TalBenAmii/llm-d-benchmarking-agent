#!/usr/bin/env python3
"""Validate that the agent drives the RIGHT COMMANDS for each known flow — a friendly,
human-readable front-end over the same harness the CI tests use.

    # deterministic (golden transcripts, hermetic — no key/Docker/repos needed):
    python scripts/eval/validate_flows.py
    python scripts/eval/validate_flows.py --flow kind-quickstart   # just one
    python scripts/eval/validate_flows.py --show                   # print each captured command

    # live (the real configured LLM drives each flow from natural-language input):
    LLM_EVAL_LIVE=1 python scripts/eval/validate_flows.py --live        # the non-simulate "live" set
    LLM_EVAL_LIVE=1 python scripts/eval/validate_flows.py --simulate    # the SIMULATE set (deploy walks)

Both --live and --simulate spend LLM quota; each scores the real model's tool/command choices
via score_flow, with the engine's stream watchdog bounding stalls (LLM_EVAL_CALL_TIMEOUT,
default 90s) so one hung call can't stall the whole run.

Exit code is non-zero if any flow fails, so this doubles as a pre-commit / CI check.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Make the project importable when run as a bare script.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from tests.flows.flows import ALL_FLOWS, FLOWS_BY_NAME  # noqa: E402
from tests.flows.harness import (  # noqa: E402
    diff_significant,
    gating_problems,
    run_flow,
    score_flow,
)

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

# Per-flow hard cap (seconds) — a backstop ABOVE the engine's stream watchdog. The watchdog
# interrupts a single stalled call (~90s in the live eval); this bounds a whole flow that is
# slow for another reason (e.g. many sub-deadline steps looping). On expiry the flow scores a
# timeout failure rather than hanging the run; the engine's disconnect terminates the CLI.
PER_FLOW_TIMEOUT_S = float(os.getenv("LLM_EVAL_FLOW_TIMEOUT", "300"))


async def _bounded(coro):
    """Await ``coro`` under the per-flow hard cap. On breach: cancel, settle under a BOUNDED
    grace (never an unbounded await — a wedged call may ignore cancellation), and raise
    TimeoutError so the flow scores a failure instead of hanging the run."""
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=PER_FLOW_TIMEOUT_S)
    if task in done:
        return task.result()
    task.cancel()
    await asyncio.wait({task}, timeout=10)
    raise TimeoutError(f"flow exceeded the {PER_FLOW_TIMEOUT_S:g}s per-flow cap")


def _c(s: str, color: str) -> str:
    return s if not sys.stdout.isatty() else f"{color}{s}{RESET}"


async def _run_deterministic(flow):
    with tempfile.TemporaryDirectory() as td:
        run = await run_flow(flow, tmp_path=Path(td))
    problems: list[str] = []
    if not run.ended_done:
        problems.append("turn did not finish cleanly")
    problems += run.errors
    problems += gating_problems(run)
    if flow.expect_no_significant:
        if run.significant:
            problems.append(f"expected nothing to run, ran {[c.argv for c in run.significant]}")
    else:
        problems += diff_significant(run, flow.expected)
    for bad in flow.forbidden_subcommands:
        if bad in run.subcommands():
            problems.append(f"ran forbidden subcommand {bad!r}")
    return (not problems), problems, run


async def _run_live(flow, *, simulate: bool):
    with tempfile.TemporaryDirectory() as td:
        run = await run_flow(flow, tmp_path=Path(td), live=True, simulate=simulate)
    passed, notes = score_flow(run, flow)
    return passed, notes, run


async def main_async(args) -> int:
    flows = ALL_FLOWS
    if args.flow:
        if args.flow not in FLOWS_BY_NAME:
            print(f"unknown flow {args.flow!r}; known: {', '.join(FLOWS_BY_NAME)}")
            return 2
        flows = [FLOWS_BY_NAME[args.flow]]
    # --live drives the real LLM in NON-simulate mode (tool-choice / error-recovery / safety flows);
    # --simulate drives it in SIMULATE mode (the multi-step deploy walks that can only REACH
    # standup/run when the SIMULATE_NOTE waves the agent past missing hardware). Each scores only the
    # flows whose live_modes contain the active mode — exactly like tests/eval/live/test_flows_live.py.
    live_run = args.live or args.simulate
    if live_run:
        mode_key = "simulate" if args.simulate else "live"
        flows = [f for f in flows if f.live_eval and mode_key in f.live_modes]

    if live_run:
        mode = "SIMULATE (real LLM, deploy walks)" if args.simulate else "LIVE (real LLM)"
    else:
        mode = "deterministic (golden transcripts)"
    print(f"\n{_c('Flow validation', BOLD)} — {mode}\n")

    results = []
    for flow in flows:
        try:
            passed, problems, run = await _bounded(
                _run_live(flow, simulate=args.simulate) if live_run else _run_deterministic(flow))
        except TimeoutError as exc:
            # The per-flow cap fired. Score it a failure and keep going — never let one stuck
            # flow hang the whole run.
            passed, problems, run = False, [str(exc)], None
        results.append((flow, passed))
        tag = _c(" PASS ", GREEN) if passed else _c(" FAIL ", RED)
        print(f"[{tag}] {_c(flow.name, BOLD)} — {flow.title}")
        if run is not None and (args.show or live_run or not passed):
            for c in run.significant:
                print(f"        {_c('$', DIM)} {' '.join(c.argv)}  {_c('[' + c.mode + ']', DIM)}")
            if not run.significant:
                print(f"        {_c('(no deploy/benchmark commands run)', DIM)}")
        # In a live run, surface score_flow's notes even on PASS — that diagnostic IS the
        # signal here.
        if live_run and passed:
            for p in problems:
                for line in p.splitlines():
                    print(f"        {_c('• ' + line, DIM)}")
        if not passed:
            for p in problems:
                for line in p.splitlines():
                    print(f"        {_c('✗ ' + line, RED)}")
        print()

    n_pass = sum(1 for _, p in results if p)
    n = len(results)
    summary = f"{n_pass}/{n} flows passed"
    print(_c(summary, GREEN if n_pass == n else RED))
    if live_run:
        print(_c("(live eval is informational — failures mean: investigate the prompt/knowledge or the model's choice)", DIM))
    return 0 if n_pass == n else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the agent runs the right commands per flow.")
    ap.add_argument("--flow", help="validate only this flow (by name)")
    ap.add_argument("--live", action="store_true", help="drive each flow with the real LLM (non-simulate 'live' set)")
    ap.add_argument("--simulate", action="store_true",
                    help="drive each flow with the real LLM in SIMULATE mode (the 'simulate' set: deploy walks)")
    ap.add_argument("--show", action="store_true", help="print the captured commands even for passing flows")
    ap.add_argument("--list", action="store_true", help="list known flows and exit")
    args = ap.parse_args()
    if args.list:
        for f in ALL_FLOWS:
            live = "" if f.live_eval else "  (deterministic-only)"
            print(f"  {f.name:<32} {f.title}{live}")
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
