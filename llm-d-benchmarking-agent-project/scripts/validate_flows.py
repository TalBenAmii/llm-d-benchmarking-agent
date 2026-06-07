#!/usr/bin/env python3
"""Validate that the agent drives the RIGHT COMMANDS for each known flow — a friendly,
human-readable front-end over the same harness the CI tests use.

    # deterministic (golden transcripts, hermetic — no key/Docker/repos needed):
    python scripts/validate_flows.py
    python scripts/validate_flows.py --flow kind-quickstart   # just one
    python scripts/validate_flows.py --show                   # print each captured command

    # live (the real configured LLM drives each flow from natural-language input):
    LLM_EVAL_LIVE=1 python scripts/validate_flows.py --live

Exit code is non-zero if any flow fails, so this doubles as a pre-commit / CI check.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

# Make the project importable when run as a bare script.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from tests.flows.flows import ALL_FLOWS, FLOWS_BY_NAME  # noqa: E402
from tests.flows.harness import (  # noqa: E402
    diff_significant,
    gating_problems,
    run_flow,
    score_flow,
)

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _c(s: str, color: str) -> str:
    return s if not sys.stdout.isatty() else f"{color}{s}{RESET}"


async def _run_deterministic(flow):
    with tempfile.TemporaryDirectory() as td:
        run = await run_flow(flow, tmp_path=Path(td))
    problems: list[str] = []
    if not run.ended_done:
        problems.append("loop did not finish cleanly")
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


async def _run_live(flow):
    from app.config import get_settings
    from app.llm.provider import get_provider

    provider = get_provider(get_settings())
    with tempfile.TemporaryDirectory() as td:
        run = await run_flow(flow, tmp_path=Path(td), provider=provider)
    passed, notes = score_flow(run, flow)
    return passed, notes, run


async def main_async(args) -> int:
    flows = ALL_FLOWS
    if args.flow:
        if args.flow not in FLOWS_BY_NAME:
            print(f"unknown flow {args.flow!r}; known: {', '.join(FLOWS_BY_NAME)}")
            return 2
        flows = [FLOWS_BY_NAME[args.flow]]
    if args.live:
        # This front-end drives flows in NON-simulate ("live") mode, so it scores only flows whose
        # live_modes include "live" (tool-choice / error-recovery / safety). Multi-step GPU-guide
        # deploys are "simulate"-only — run them via the simulate pytest path (see test_flows_live.py).
        flows = [f for f in flows if f.live_eval and "live" in f.live_modes]

    mode = "LIVE (real LLM)" if args.live else "deterministic (golden transcripts)"
    print(f"\n{_c('Flow validation', BOLD)} — {mode}\n")

    results = []
    for flow in flows:
        passed, problems, run = await (_run_live(flow) if args.live else _run_deterministic(flow))
        results.append((flow, passed))
        tag = _c(" PASS ", GREEN) if passed else _c(" FAIL ", RED)
        print(f"[{tag}] {_c(flow.name, BOLD)} — {flow.title}")
        if args.show or args.live or not passed:
            for c in run.significant:
                print(f"        {_c('$', DIM)} {' '.join(c.argv)}  {_c('[' + c.mode + ']', DIM)}")
            if not run.significant:
                print(f"        {_c('(no deploy/benchmark commands run)', DIM)}")
        if not passed:
            for p in problems:
                for line in p.splitlines():
                    print(f"        {_c('✗ ' + line, RED)}")
        print()

    n_pass = sum(1 for _, p in results if p)
    n = len(results)
    summary = f"{n_pass}/{n} flows passed"
    print(_c(summary, GREEN if n_pass == n else RED))
    if args.live:
        print(_c("(live eval is informational — failures mean: investigate the prompt/knowledge or the model's choice)", DIM))
    return 0 if n_pass == n else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the agent runs the right commands per flow.")
    ap.add_argument("--flow", help="validate only this flow (by name)")
    ap.add_argument("--live", action="store_true", help="drive each flow with the real LLM from mock input")
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
