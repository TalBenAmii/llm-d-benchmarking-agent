"""Phase 61 — right-size the harness launcher CPU request for small/Kind clusters.

The benchmark launcher pod requests 16 CPUs by default (`LLMDBENCH_HARNESS_CPU_NR`). On a
single-node Kind cluster that node usually can't satisfy it, so the launcher silently sits in
`FailedScheduling`/`Pending`. This feature lets the agent (1) READ the node's allocatable CPU
via `probe_environment(node_capacity)`, and (2) lower the request by passing a backend-only
`harness_cpu_nr` flag to `execute_llmdbenchmark`, which becomes the `LLMDBENCH_HARNESS_CPU_NR`
ENV VAR on the launcher subprocess — never a CLI flag, never surfaced to the browser.

The JUDGMENT (whether/what to lower) lives in knowledge/harness_sizing.md; these tests pin the
MECHANISM: node-CPU probing, the env reaching the child, and the env never reaching the UI.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.tools.context import ToolError
from app.tools.run.execute import execute_llmdbenchmark
from app.tools.access.knowledge_access import read_knowledge
from app.tools.setup.probe import _parse_cpu_quantity, probe_environment
from tests._helpers import _ctx
from tests.flows.harness import CaptureRunner

# A single-node Kind cluster: 4 allocatable cores — far below the harness default of 16.
SMALL_NODE_JSON = json.dumps({
    "items": [{
        "metadata": {"name": "kind-control-plane"},
        "status": {"allocatable": {"cpu": "4"}, "capacity": {"cpu": "4"}},
    }],
})

# A real multi-node cluster whose smallest node has 32 cores — the default 16 fits.
LARGE_NODE_JSON = json.dumps({
    "items": [
        {"metadata": {"name": "node-a"},
         "status": {"allocatable": {"cpu": "32"}, "capacity": {"cpu": "32"}}},
        {"metadata": {"name": "node-b"},
         "status": {"allocatable": {"cpu": "48"}, "capacity": {"cpu": "48"}}},
    ],
})

# Millicore + fractional forms K8s actually emits, to exercise the quantity parser.
MILLICORE_NODE_JSON = json.dumps({
    "items": [{
        "metadata": {"name": "tiny"},
        "status": {"allocatable": {"cpu": "1500m"}, "capacity": {"cpu": "2"}},
    }],
})


def _last_run_call(runner: CaptureRunner):
    return next(c for c in reversed(runner.calls) if c["argv"][:1] == ["llmdbenchmark"])


# ---- (1) node-CPU probe ----------------------------------------------------

async def test_probe_node_capacity_small_node(tmp_path):
    ctx, _ = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["node_capacity"])
    cap = out["node_capacity"]
    assert cap["available"] is True
    assert cap["min_allocatable_cpu"] == 4.0
    assert cap["nodes"] == [
        {"name": "kind-control-plane", "allocatable_cpu": 4.0, "capacity_cpu": 4.0}
    ]


async def test_probe_node_capacity_large_cluster_min_is_smallest(tmp_path):
    ctx, _ = _ctx(tmp_path, nodes_json=LARGE_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["node_capacity"])
    cap = out["node_capacity"]
    # The binding constraint is the SMALLEST allocatable node (the scheduler picks one node).
    assert cap["min_allocatable_cpu"] == 32.0
    assert {n["name"] for n in cap["nodes"]} == {"node-a", "node-b"}


async def test_probe_node_capacity_in_all_and_uses_allowlisted_readonly(tmp_path):
    """node_capacity runs as part of 'all' and reaches the runner via the read-only,
    already-allowlisted `kubectl get nodes -o json` (no allowlist change needed)."""
    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, namespace="llmd")  # checks defaults to "all"
    assert "node_capacity" in out
    assert ["kubectl", "get", "nodes", "-o", "json"] in [c["argv"] for c in runner.calls]


async def test_probe_node_capacity_no_kubectl(tmp_path):
    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: None):
        out = await probe_environment(ctx, checks=["node_capacity"])
    assert out["node_capacity"] == {"available": False, "nodes": [], "min_allocatable_cpu": None}
    # Nothing ran (no kubectl on PATH) — the probe degraded gracefully.
    assert runner.calls == []


async def test_probe_node_capacity_unreachable_cluster(tmp_path):
    """A non-zero kubectl exit (unreachable cluster) yields a structured unavailable result."""
    ctx, runner = _ctx(tmp_path, nodes_json="")  # no canned output

    async def boom(argv, **kw):
        from app.security.runner import RunResult
        return RunResult(exit_code=1, duration_s=0.0, real_argv=list(argv), cwd=None,
                         output="The connection to the server was refused")

    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"), \
            patch.object(ctx, "run_readonly", side_effect=boom):
        out = await probe_environment(ctx, checks=["node_capacity"])
    assert out["node_capacity"]["available"] is False
    assert out["node_capacity"]["min_allocatable_cpu"] is None


def test_parse_cpu_quantity_handles_millicore_and_bare():
    assert _parse_cpu_quantity("250m") == 0.25
    assert _parse_cpu_quantity("1500m") == 1.5
    assert _parse_cpu_quantity("4") == 4.0
    assert _parse_cpu_quantity("0.5") == 0.5
    assert _parse_cpu_quantity(None) is None
    assert _parse_cpu_quantity("") is None
    assert _parse_cpu_quantity("garbage") is None


async def test_probe_node_capacity_parses_millicore_node(tmp_path):
    ctx, _ = _ctx(tmp_path, nodes_json=MILLICORE_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["node_capacity"])
    cap = out["node_capacity"]
    assert cap["min_allocatable_cpu"] == 1.5
    assert cap["nodes"][0]["capacity_cpu"] == 2.0


# ---- (2) per-run env plumbing into the launcher subprocess -----------------

async def test_small_node_run_carries_lowered_harness_cpu_nr(tmp_path):
    """On a small node the agent lowers the request: the launcher subprocess child_env carries
    the chosen LLMDBENCH_HARNESS_CPU_NR so the run schedules instead of going Pending."""
    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await execute_llmdbenchmark(
            ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
            harness="inference-perf", workload="sanity_random.yaml",
            flags={"harness_cpu_nr": 3},
        )
    call = _last_run_call(runner)
    assert call["extra_env"] == {"LLMDBENCH_HARNESS_CPU_NR": "3"}
    # It is an ENV VAR, NOT a CLI flag: nothing CPU-related is in argv.
    assert not any("CPU" in tok or tok == "3" for tok in call["argv"])


async def test_large_node_run_omits_harness_cpu_nr_default_16(tmp_path):
    """On a large node the agent omits harness_cpu_nr; the launcher keeps the default 16,
    so no LLMDBENCH_HARNESS_CPU_NR override is placed in the child env."""
    ctx, runner = _ctx(tmp_path, nodes_json=LARGE_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await execute_llmdbenchmark(
            ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
            harness="inference-perf", workload="sanity_random.yaml",
            flags={},  # no harness_cpu_nr
        )
    call = _last_run_call(runner)
    assert call["extra_env"] is None  # default 16 stands — no env override


async def test_harness_cpu_nr_actually_reaches_built_child_env(tmp_path):
    """End-to-end at the runner boundary: the per-run override merges LAST into the built env,
    so the real child process would carry LLMDBENCH_HARNESS_CPU_NR=2 (the scheduling fix)."""
    from app.security.runner import CommandRunner

    runner = CommandRunner({})
    # The real env-builder must place the per-execution override into the child env.
    env = runner._build_env({"LLMDBENCH_HARNESS_CPU_NR": "2"})
    assert env["LLMDBENCH_HARNESS_CPU_NR"] == "2"
    # And it wins over a global os.environ value (override merged last).
    with patch.dict("os.environ", {"LLMDBENCH_HARNESS_CPU_NR": "16"}):
        env2 = runner._build_env({"LLMDBENCH_HARNESS_CPU_NR": "2"})
    assert env2["LLMDBENCH_HARNESS_CPU_NR"] == "2"


# ---- (2b) harness_mem: the launcher MEMORY request, sibling of harness_cpu_nr ----

async def test_run_carries_harness_mem_env(tmp_path):
    """harness_mem plumbs the launcher pod's memory request as LLMDBENCH_HARNESS_CPU_MEM on the
    subprocess child_env (a backend-only ENV VAR, never a CLI flag), alongside the CPU request."""
    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await execute_llmdbenchmark(
            ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
            harness="inference-perf", workload="sanity_random.yaml",
            flags={"harness_cpu_nr": 3, "harness_mem": "48Gi"},
        )
    call = _last_run_call(runner)
    assert call["extra_env"] == {
        "LLMDBENCH_HARNESS_CPU_NR": "3", "LLMDBENCH_HARNESS_CPU_MEM": "48Gi",
    }
    # It is an ENV VAR, NOT a CLI flag: nothing memory-related is in argv.
    assert not any("48Gi" in tok or "MEM" in tok for tok in call["argv"])


async def test_harness_mem_default_omitted_when_unset(tmp_path):
    """Omit harness_mem and the launcher keeps the upstream default 32Gi — no env override."""
    ctx, runner = _ctx(tmp_path, nodes_json=LARGE_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await execute_llmdbenchmark(
            ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
            harness="inference-perf", workload="sanity_random.yaml", flags={},
        )
    assert _last_run_call(runner)["extra_env"] is None


async def test_harness_mem_rejects_malformed_quantity(tmp_path):
    """A value that is not a Kubernetes memory quantity is rejected AT THE BOUNDARY with a clean,
    self-correctable ToolError — never forwarded to become a late pod-apply failure."""
    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        with pytest.raises(ToolError, match="Kubernetes memory quantity"):
            await execute_llmdbenchmark(
                ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
                harness="inference-perf", workload="sanity_random.yaml",
                flags={"harness_mem": "48 gigs"},
            )
    # Rejected before the launcher ran — nothing benchmark-related was dispatched.
    assert not any(c["argv"][:1] == ["llmdbenchmark"] for c in runner.calls)


async def test_harness_mem_never_appears_in_browser_command_events(tmp_path):
    """Scrub invariant (shared with harness_cpu_nr): the memory value must NOT appear in any
    browser-facing `command` event, even though it IS applied to the backend child env."""
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON, emit=emit)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await execute_llmdbenchmark(
            ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
            harness="inference-perf", workload="sanity_random.yaml",
            flags={"harness_mem": "48Gi"},
        )
    assert _last_run_call(runner)["extra_env"] == {"LLMDBENCH_HARNESS_CPU_MEM": "48Gi"}
    for _t, p in [(t, p) for (t, p) in events if t == "command"]:
        blob = json.dumps(p)
        assert "LLMDBENCH_HARNESS_CPU_MEM" not in blob and "48Gi" not in blob
        assert "extra_env" not in p and "env" not in p


# ---- (3) the value never reaches the browser ------------------------------

async def test_harness_cpu_nr_never_appears_in_browser_command_events(tmp_path):
    """The scrub invariant: the chosen LLMDBENCH_HARNESS_CPU_NR must NOT appear in any
    browser-facing `command` event (which carries only argv/text/mode/auto_run), even though
    it IS applied to the backend child env."""
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx, runner = _ctx(tmp_path, nodes_json=SMALL_NODE_JSON, emit=emit)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await execute_llmdbenchmark(
            ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
            harness="inference-perf", workload="sanity_random.yaml",
            flags={"harness_cpu_nr": 3},
        )
    # It DID reach the backend child env...
    assert _last_run_call(runner)["extra_env"] == {"LLMDBENCH_HARNESS_CPU_NR": "3"}
    # ...but NOT the UI: no command event mentions the env var or its value, and no event
    # payload carries an "env"/"extra_env" key at all.
    cmd_events = [p for (t, p) in events if t == "command"]
    assert cmd_events, "expected at least one command event"
    for p in cmd_events:
        blob = json.dumps(p)
        assert "LLMDBENCH_HARNESS_CPU_NR" not in blob
        assert "extra_env" not in p and "env" not in p
        # The argv/text the UI sees never contains the chosen value either.
        assert "3" not in p["text"].split()


# ---- (4) the judgment lives in knowledge, not Python ----------------------

def test_harness_sizing_knowledge_documents_the_distinction(tool_ctx):
    """The harness-aware sizing judgment must live in knowledge/, discoverable via
    read_knowledge — never as if/elif in Python."""
    out = read_knowledge(tool_ctx, name="harness_sizing")
    assert out["name"] == "harness_sizing.md"
    body = out["content"]
    # Both harness classes and the env var are documented.
    assert "inference-perf" in body and "vllm-benchmark" in body
    assert "LLMDBENCH_HARNESS_CPU_NR" in body
    # The headline distinction (multi-process vs single-process headroom) is present.
    low = body.lower()
    assert "multi-process" in low and "single-process" in low
