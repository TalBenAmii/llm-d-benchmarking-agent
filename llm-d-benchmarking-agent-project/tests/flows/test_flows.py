"""Deterministic, hermetic flow validation — this is what GATES CI.

For each flow we replay its golden transcript through the real agent loop (no API key, no
Docker, no kind, no repos) and assert the agent produces exactly the right commands, with
correct read-only/mutating classification and approval gating. Plus direct allowlist
assertions (deny-by-default holds) and a drift guard against the live catalog.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.security.allowlist import Allowlist

from .catalog_snapshot import HARNESSES, SPECS, WORKLOADS, frozen_catalog
from .flows import ALL_FLOWS, FLOWS_BY_NAME
from .harness import diff_significant, gating_problems, run_flow

_FLOW_IDS = [f.name for f in ALL_FLOWS]

# The benchmark repo's canonical on-disk location (sibling of this project) — used ONLY by
# the drift guard. We resolve it directly rather than via settings, because settings'
# repos_dir can be misconfigured (e.g. a blank REPOS_DIR resolving to '.'), which would
# make the guard silently skip and stop protecting us.
_BENCH_REPO = Path(__file__).resolve().parents[2].parent / "llm-d-benchmark"


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_FLOW_IDS)
async def test_flow_runs_the_right_commands(flow, tmp_path):
    run = await run_flow(flow, tmp_path=tmp_path)

    # The loop completed cleanly (no crash, no step-limit blow-up).
    assert run.ended_done, f"[{flow.name}] loop did not finish: events={run.events[-3:]}"
    assert not run.errors, f"[{flow.name}] loop emitted errors: {run.errors}"

    # The universal safety invariant: mutating ⇒ approval-gated; read-only ⇒ auto-run.
    assert not (g := gating_problems(run)), f"[{flow.name}] gating violations:\n" + "\n".join(g)

    # Phase-1 invariant: a `command` event is emitted for EVERY executed command (read-only
    # probes included), in order — and the same trail is recorded on the session for replay.
    cmd_events = [p for (t, p) in run.events if t == "command"]
    assert [c["argv"] for c in cmd_events] == [c.argv for c in run.commands], (
        f"[{flow.name}] command-event/exec mismatch:\n"
        f"  events: {[c['argv'] for c in cmd_events]}\n"
        f"  ran:    {[c.argv for c in run.commands]}"
    )
    assert [c["argv"] for c in run.session.commands] == [c.argv for c in run.commands], (
        f"[{flow.name}] session.commands trail does not match executed commands"
    )

    # The right commands, in order (significant = llmdbenchmark/install.sh/git/helm).
    if flow.expect_no_significant:
        assert not run.significant, (
            f"[{flow.name}] expected NOTHING to run, but captured: {[c.argv for c in run.significant]}"
        )
    else:
        assert not (d := diff_significant(run, flow.expected)), f"[{flow.name}] command mismatch:\n" + "\n".join(d)

    # Forbidden subcommands / executables must not appear.
    subs = run.subcommands()
    for bad in flow.forbidden_subcommands:
        assert bad not in subs, f"[{flow.name}] forbidden subcommand {bad!r} was run (subcommands={subs})"
    exes = {c.exe for c in run.commands}
    for bad in flow.forbidden_exes:
        assert bad not in exes, f"[{flow.name}] forbidden executable {bad!r} was run"

    # Read-only-only flows: nothing mutating, and no command approval prompt at all.
    if flow.expect_all_readonly:
        muts = [c.argv for c in run.commands if c.mode == "mutating"]
        assert not muts, f"[{flow.name}] expected read-only-only, but these mutate: {muts}"
        cmd_approvals = [r for r in run.approval_requests if r["kind"] == "command"]
        assert not cmd_approvals, f"[{flow.name}] a command approval was requested in a read-only preview"

    # The agent surfaced the expected guidance (e.g. offering `kind delete`).
    joined = " ".join(run.assistant_texts).lower()
    for needle in flow.assistant_text_contains:
        assert needle.lower() in joined, f"[{flow.name}] assistant text missing {needle!r}"

    # Existing-stack flow: probe must actually have reported a running stack.
    if flow.expect_stack_detected:
        probe = run.tool_result("probe_environment") or {}
        assert probe.get("stack", {}).get("detected") is True, (
            f"[{flow.name}] probe did not detect a running stack: {probe.get('stack')}"
        )

    # Refusal flow: the named tools must have errored/refused (nothing silently slipped through).
    for name in flow.expect_tool_errors_for:
        assert run.tool_errored(name), f"[{flow.name}] expected tool {name!r} to refuse, but it didn't"


@pytest.mark.parametrize("flow", [f for f in ALL_FLOWS if f.allowlist_checks],
                         ids=[f.name for f in ALL_FLOWS if f.allowlist_checks])
def test_flow_allowlist_assertions(flow):
    """Direct policy assertions attached to a flow (deny-by-default + positive controls)."""
    allowlist = Allowlist.from_file(get_settings().allowlist_path)
    cat = {"specs": SPECS, "harnesses": HARNESSES, "workloads": WORKLOADS}
    for chk in flow.allowlist_checks:
        d = allowlist.validate(chk.argv, catalog=cat)
        assert d.allowed == chk.allowed, (
            f"[{flow.name}] {chk.argv}: expected allowed={chk.allowed} ({chk.why}); "
            f"got allowed={d.allowed} reason={d.reason!r}"
        )
        if chk.allowed and chk.mode is not None:
            assert d.mode == chk.mode, (
                f"[{flow.name}] {chk.argv}: expected mode={chk.mode!r}, got {d.mode!r}"
            )


def test_every_flow_is_uniquely_named():
    assert len(FLOWS_BY_NAME) == len(ALL_FLOWS), "duplicate flow name(s)"


def test_expected_commands_reference_real_catalog_items():
    """Every spec/harness/workload a flow's expected commands name must exist in the
    frozen snapshot (catches a typo'd fixture before it can mask a real regression)."""
    cat = frozen_catalog()
    specs, harnesses, workloads = set(cat["specs"]), set(cat["harnesses"]), set(cat["workloads"])
    for flow in ALL_FLOWS:
        for exp in flow.expected:
            argv = exp.argv
            if argv[0] != "llmdbenchmark":
                continue
            for flag, universe in (("--spec", specs), ("-l", harnesses), ("-w", workloads)):
                if flag in argv:
                    val = argv[argv.index(flag) + 1]
                    assert val == "*" or val in universe, (
                        f"[{flow.name}] {flag} {val!r} not in the frozen catalog"
                    )


@pytest.mark.skipif(
    not (_BENCH_REPO / "config" / "specification").is_dir(),
    reason="llm-d-benchmark repo not checked out — run locally to guard against catalog drift",
)
def test_snapshot_matches_live():
    """Drift guard: when the real repo IS present, every name the flows rely on must still
    exist upstream. This is what keeps the frozen snapshot honest."""
    from app.tools.catalog import build_catalog

    live = build_catalog(_BENCH_REPO)
    live_specs, live_harnesses, live_workloads = set(live["specs"]), set(live["harnesses"]), set(live["workloads"])

    referenced_specs, referenced_harnesses, referenced_workloads = set(), set(), set()
    for flow in ALL_FLOWS:
        if flow.required_spec:
            referenced_specs.add(flow.required_spec)
        for exp in flow.expected:
            argv = exp.argv
            if argv and argv[0] == "llmdbenchmark":
                if "--spec" in argv:
                    referenced_specs.add(argv[argv.index("--spec") + 1])
                if "-l" in argv:
                    referenced_harnesses.add(argv[argv.index("-l") + 1])
                if "-w" in argv:
                    referenced_workloads.add(argv[argv.index("-w") + 1])

    assert referenced_specs <= live_specs, f"specs gone from upstream: {referenced_specs - live_specs}"
    assert referenced_harnesses <= live_harnesses, f"harnesses gone upstream: {referenced_harnesses - live_harnesses}"
    assert referenced_workloads <= live_workloads, f"workloads gone upstream: {referenced_workloads - live_workloads}"
