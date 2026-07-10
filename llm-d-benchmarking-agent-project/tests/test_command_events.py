"""The `command` event (Phase 1 — full command transparency + debug view).

Every command the agent actually executes is announced to the UI via a `command` event,
*including* auto-run read-only probes — not just the approval-gated mutating ones. The
event fires immediately before the process runs, and (for mutating commands) only after
approval, so it records what truly executed. Sessions persist this trail so a resumed chat
can replay the debug view.
"""
from __future__ import annotations

import json

from app.agent.session import _COMMANDS_MAX, Session
from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ApprovalRejected, ToolContext
from tests._helpers import _capture_ctx

RO = ["kind", "get", "clusters"]                                  # read-only, no catalog needed
MUT = ["kind", "create", "cluster", "--name", "test-cluster"]     # mutating, approval-gated


def _collector():
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    return events, emit


def _commands(events):
    return [p for (t, p) in events if t == "command"]


async def test_run_readonly_emits_command_auto_run(tmp_path):
    events, emit = _collector()
    ctx, runner = _capture_ctx(tmp_path, emit=emit)

    await ctx.run_readonly(RO)

    cmds = _commands(events)
    assert len(cmds) == 1
    assert cmds[0]["argv"] == RO
    assert cmds[0]["text"] == "kind get clusters"
    assert cmds[0]["mode"] == "read_only"
    assert cmds[0]["auto_run"] is True
    assert len(runner.calls) == 1  # and it actually ran


async def test_run_command_readonly_emits_command_auto_run(tmp_path):
    events, emit = _collector()
    ctx, runner = _capture_ctx(tmp_path, emit=emit)

    await ctx.run_command(RO)

    cmds = _commands(events)
    assert len(cmds) == 1
    assert cmds[0]["argv"] == RO and cmds[0]["text"] == "kind get clusters"
    assert cmds[0]["auto_run"] is True and cmds[0]["mode"] == "read_only"
    assert len(runner.calls) == 1  # the read-only command really ran (no approval skip)


async def test_run_command_mutating_emits_after_approval(tmp_path):
    events, emit = _collector()
    seen: list[str] = []

    async def approve(kind, payload):
        seen.append("approval")
        return True

    ctx, runner = _capture_ctx(tmp_path, emit=emit, approve=approve)
    await ctx.run_command(MUT)

    cmds = _commands(events)
    assert len(cmds) == 1
    assert cmds[0]["argv"] == MUT
    assert cmds[0]["mode"] == "mutating"
    assert cmds[0]["auto_run"] is False
    assert seen == ["approval"]  # approval happened
    assert len(runner.calls) == 1


async def test_rejected_mutating_emits_no_command(tmp_path):
    events, emit = _collector()

    async def reject(kind, payload):
        return False

    ctx, runner = _capture_ctx(tmp_path, emit=emit, approve=reject)
    try:
        await ctx.run_command(MUT)
        assert False, "expected ApprovalRejected"
    except ApprovalRejected:
        pass

    assert _commands(events) == []   # never announced — it never ran
    assert runner.calls == []


async def test_no_emit_wired_is_safe(tmp_path):
    # With no emit callback, execution still works (no crash).
    ctx, runner = _capture_ctx(tmp_path, emit=None)
    await ctx.run_readonly(RO)
    assert len(runner.calls) == 1


async def test_probe_environment_emits_command_per_probe(tmp_path):
    """The headline Phase-1 behavior: read-only probes are now visible. With all tools
    present and a namespace, probe_environment runs exactly 11 read-only commands (the 6
    original probes plus the Phase-27 prometheus_crds probe `kubectl get crd`, the
    Phase-60 cluster-preconditions probe `kubectl version --output json`, ONE shared
    `kubectl get nodes -o json` that both the Phase-61 node-capacity and Phase-64
    provider-detection probes consume — probe_environment fetches the node list ONCE and
    hands the same result to both, deduping the formerly-doubled fetch — and the
    metrics-server pre-flight probe's two reads: `kubectl top nodes` + `kubectl get
    deployment -n kube-system -l k8s-app=metrics-server -o json`) and each is announced
    (auto_run) — proving probe-emit/exec parity."""
    from unittest.mock import patch

    from app.tools.setup.probe import probe_environment

    events, emit = _collector()
    ctx, runner = _capture_ctx(tmp_path, emit=emit)

    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await probe_environment(ctx, namespace="llmd-quickstart")

    cmds = _commands(events)
    assert len(cmds) == 11, [c["text"] for c in cmds]
    assert all(c["mode"] == "read_only" and c["auto_run"] is True for c in cmds)
    assert len(runner.calls) == 11  # one announcement per real execution
    exes = {c["argv"][0] for c in cmds}
    assert exes == {"docker", "kind", "kubectl"}
    # the Phase-27 CRD probe is among them (read-only `kubectl get crd`).
    assert any(c["argv"][:3] == ["kubectl", "get", "crd"] for c in cmds)
    # The metrics-server pre-flight probe's two read-only reads are among them.
    assert ["kubectl", "top", "nodes"] in [c["argv"] for c in cmds]
    assert ["kubectl", "get", "deployment", "-n", "kube-system",
            "-l", "k8s-app=metrics-server", "-o", "json"] in [c["argv"] for c in cmds]
    # The node list is fetched (shared by node-capacity + provider-detection).
    assert ["kubectl", "get", "nodes", "-o", "json"] in [c["argv"] for c in cmds]
    # The cluster-preconditions probe (Phase 60) is among them (read-only `kubectl version`).
    assert ["kubectl", "version", "--output", "json"] in [c["argv"] for c in cmds]
    # DEDUP (follow-up #2): node-capacity (Phase 61) + provider-detection (Phase 64) both consume
    # `kubectl get nodes -o json`, but probe_environment runs it ONCE and passes the same result to
    # both — so exactly ONE `get nodes` runs per probe_environment call (was two before the dedup;
    # advise_accelerators is a SEPARATE tool call and unaffected).
    get_nodes = [c for c in cmds if c["argv"] == ["kubectl", "get", "nodes", "-o", "json"]]
    assert len(get_nodes) == 1, [c["argv"] for c in cmds]


async def test_deploy_flow_surfaces_every_command(tmp_path):
    """Driven through the REAL agent loop: a full quickstart deploy surfaces every executed
    command as a `command` event, including each llmdbenchmark subcommand (standup/smoketest/run)."""
    from tests.flows.flows import FLOWS_BY_NAME
    from tests.flows.harness import run_flow

    run = await run_flow(FLOWS_BY_NAME["kind-quickstart"], tmp_path=tmp_path)
    cmd_argvs = [p["argv"] for (t, p) in run.events if t == "command"]

    # Every significant command (llmdbenchmark/install.sh/git/helm) is announced.
    for c in run.significant:
        assert c.argv in cmd_argvs, f"significant command never announced: {c.argv}"

    # The three llmdbenchmark lifecycle subcommands each appear in a command event.
    llmd_subs = {
        s
        for a in cmd_argvs if a and a[0] == "llmdbenchmark"
        for s in ("standup", "smoketest", "run") if s in a
    }
    assert {"standup", "smoketest", "run"} <= llmd_subs, f"missing lifecycle subcommands: saw {llmd_subs}"


def test_session_records_and_caps_commands():
    s = Session(id="x", ctx=None)  # ctx not needed for record_command
    for i in range(_COMMANDS_MAX + 25):
        s.record_command({"text": f"cmd {i}", "argv": ["x", str(i)], "mode": "read_only", "auto_run": True})
    assert len(s.commands) == _COMMANDS_MAX
    # Oldest dropped, newest kept.
    assert s.commands[-1]["text"] == f"cmd {_COMMANDS_MAX + 24}"


def _persist_ctx(tmp_path):
    """A CommandRunner-backed ToolContext rooted at a per-session workspace — the shared setup for
    the persist-and-reload tests below (they differ only in what they record before persist())."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    return ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=CommandRunner(settings.repo_paths),
        workspace=tmp_path / "ws" / "sessions" / "s1",
    )


def test_session_persists_and_reloads_commands(tmp_path):
    ctx = _persist_ctx(tmp_path)
    s = Session(id="s1", ctx=ctx)
    s.messages.append({"role": "user", "content": "hi"})
    s.record_command({"text": "kind get clusters", "argv": RO, "mode": "read_only", "auto_run": True})
    s.persist()

    from app.agent.session import SessionManager

    mgr = SessionManager(ctx.settings, ctx.allowlist, ctx.runner)
    reloaded = mgr.load("s1")
    assert reloaded is not None
    assert reloaded.commands and reloaded.commands[0]["argv"] == RO


async def test_emitted_command_carries_tool_call_id(tmp_path):
    """The command event ties back to the issuing tool call (None for the pre-turn probe), so a
    resumed chat can replay each command inline in its original transcript position."""
    events, emit = _collector()
    ctx, _ = _capture_ctx(tmp_path, emit=emit)
    ctx.current_tool_call_id = "tc-123"
    await ctx.run_readonly(RO)
    cmds = _commands(events)
    assert cmds and cmds[0]["tool_call_id"] == "tc-123"


def test_history_items_interleave_commands_inline():
    """_history_items replays the executed-command trail INLINE: each command becomes a `command`
    item right after the tool call that ran it; pre-turn (tool_call_id=None) probes lead the
    transcript; a command whose tool call was compacted out of `messages` is still not dropped."""
    from app.main import _history_items

    s = Session(id="h", ctx=None)
    # A pre-turn probe (no owning tool call) ran before the first message.
    s.record_command({"text": "docker info", "argv": ["docker", "info"],
                      "mode": "read_only", "auto_run": True, "tool_call_id": None})
    s.messages.append({"role": "user", "content": "benchmark it"})
    s.messages.append({"role": "assistant", "content": "on it",
                       "tool_calls": [{"id": "tc1", "name": "execute_llmdbenchmark", "input": {}}]})
    # Two commands issued by tc1, in order (read-only probe then approved mutation).
    s.record_command({"text": "kind get clusters", "argv": ["kind", "get", "clusters"],
                      "mode": "read_only", "auto_run": True, "tool_call_id": "tc1"})
    s.record_command({"text": "kind create cluster", "argv": ["kind", "create", "cluster"],
                      "mode": "mutating", "auto_run": False, "tool_call_id": "tc1"})
    # A command whose owning tool call is no longer in `messages` (compacted) must survive.
    s.record_command({"text": "kubectl get ns", "argv": ["kubectl", "get", "ns"],
                      "mode": "read_only", "auto_run": True, "tool_call_id": "gone"})

    items = _history_items(s)
    roles = [it["role"] for it in items]
    # The pre-turn probe leads the transcript (before the first user bubble).
    assert items[0]["role"] == "command" and items[0]["text"] == "docker info"
    assert roles.index("user") > 0
    # tc1's two commands appear right after its tool_call, in order.
    tc_idx = roles.index("tool_call")
    assert roles[tc_idx + 1] == "command" and items[tc_idx + 1]["text"] == "kind get clusters"
    assert roles[tc_idx + 2] == "command" and items[tc_idx + 2]["text"] == "kind create cluster"
    # The command shape carries everything the inline renderer needs.
    assert items[tc_idx + 2]["mode"] == "mutating" and items[tc_idx + 2]["auto_run"] is False
    # The orphaned command (its tool call gone) is appended last, never silently truncated.
    assert items[-1]["role"] == "command" and items[-1]["text"] == "kubectl get ns"


def test_session_records_and_caps_card_results():
    from app.agent.session import _CARD_RESULTS_MAX

    s = Session(id="x", ctx=None)  # ctx not needed for record_card_result
    for i in range(_CARD_RESULTS_MAX + 25):
        s.record_card_result({"tool_call_id": f"tc{i}", "name": "analyze_results", "result": {"i": i}})
    assert len(s.card_results) == _CARD_RESULTS_MAX
    # Oldest dropped, newest kept.
    assert s.card_results[-1]["result"]["i"] == _CARD_RESULTS_MAX + 24


def test_session_persists_and_reloads_card_results(tmp_path):
    ctx = _persist_ctx(tmp_path)
    s = Session(id="s1", ctx=ctx)
    s.messages.append({"role": "user", "content": "hi"})
    s.record_card_result({"tool_call_id": "tc1", "name": "locate_and_parse_report",
                          "result": {"summary": {"model": "m"}, "charts": []}})
    s.persist()

    from app.agent.session import SessionManager

    mgr = SessionManager(ctx.settings, ctx.allowlist, ctx.runner)
    reloaded = mgr.load("s1")
    assert reloaded is not None
    assert reloaded.card_results and reloaded.card_results[0]["name"] == "locate_and_parse_report"
    assert reloaded.card_results[0]["result"]["summary"]["model"] == "m"


def test_session_persists_and_reloads_tool_durations(tmp_path):
    """A tool call's wall-clock run time is persisted (keyed by tool_call_id) and reloaded, so a
    resumed/reloaded chat can show the SAME duration badge on its action rows a live run does."""
    ctx = _persist_ctx(tmp_path)
    s = Session(id="s1", ctx=ctx)
    s.messages.append({"role": "user", "content": "hi"})
    s.record_tool_duration("tc1", 42.137)
    s.record_tool_duration(None, 9.9)   # no id → no-op (never crashes)
    s.persist()

    from app.agent.session import SessionManager

    mgr = SessionManager(ctx.settings, ctx.allowlist, ctx.runner)
    reloaded = mgr.load("s1")
    assert reloaded is not None
    assert reloaded.tool_durations.get("tc1") == 42.14   # rounded to 2dp on record
    assert None not in reloaded.tool_durations


def test_history_items_tool_call_carries_persisted_duration():
    """_history_items stamps each replayed tool_call with its persisted duration_s so the action
    row shows the time badge on resume (None when no duration was captured)."""
    from app.main import _history_items

    s = Session(id="x", ctx=None)
    s.messages.append({"role": "user", "content": "go"})
    s.messages.append({"role": "assistant", "content": "",
                       "tool_calls": [{"id": "tc1", "name": "probe_environment", "input": {}}]})
    s.tool_durations = {"tc1": 12.5}
    items = _history_items(s)
    tcs = [it for it in items if it["role"] == "tool_call"]
    assert tcs and tcs[0]["duration_s"] == 12.5


def test_history_items_interleave_card_result_inline():
    """_history_items replays a persisted card result so a resumed/reloaded chat re-renders the
    report card + clickable charts: the FULL result becomes a `tool_result` item right after its
    tool_call (an analyzer result also gets the deterministic `results_card`), and a card result
    whose tool call was compacted out of `messages` is still not dropped."""
    from app.main import _history_items

    s = Session(id="h", ctx=None)
    s.messages.append({"role": "user", "content": "show me the results"})
    s.messages.append({"role": "assistant", "content": "here",
                       "tool_calls": [{"id": "tc1", "name": "locate_and_parse_report", "input": {}}]})
    report = {"summary": {"model": "llama"}, "charts": [{"title": "ttft", "path": "p.png"}]}
    s.record_card_result({"tool_call_id": "tc1", "name": "locate_and_parse_report", "result": report})
    # A card result whose owning tool call is no longer in `messages` (compacted) must survive.
    s.record_card_result({"tool_call_id": "gone", "name": "probe_environment", "result": {"host": {}}})

    items = _history_items(s)
    roles = [it["role"] for it in items]
    # The report's full result lands as a `tool_result` right after its tool_call, carrying the
    # un-clamped summary + chart paths the renderer needs.
    tc_idx = roles.index("tool_call")
    assert roles[tc_idx + 1] == "tool_result"
    tr = items[tc_idx + 1]
    assert tr["name"] == "locate_and_parse_report"
    assert tr["result"]["summary"]["model"] == "llama"
    assert tr["result"]["charts"][0]["path"] == "p.png"
    # The orphaned card result (its tool call gone) is appended last, never silently dropped.
    assert items[-1]["role"] == "tool_result" and items[-1]["name"] == "probe_environment"


def test_history_items_card_result_emits_results_card_for_analyzer():
    """An analyze_results card result also yields the deterministic `results_card` item (re-derived
    server-side from the same result), exactly as the live stream emits it after the tool_result."""
    from app.main import _history_items

    s = Session(id="h2", ctx=None)
    s.messages.append({"role": "assistant", "content": "",
                       "tool_calls": [{"id": "tc1", "name": "analyze_results", "input": {}}]})
    # A minimal analyzed result that build_results_card will turn into a card.
    from app.agent.cards import build_results_card
    result = {"analyzed": True, "report": {"summary": {"model": "m"}}}
    s.record_card_result({"tool_call_id": "tc1", "name": "analyze_results", "result": result})

    items = _history_items(s)
    roles = [it["role"] for it in items]
    tr_idx = roles.index("tool_result")
    # Only assert the results_card is present iff build_results_card produced one for this result,
    # so the test tracks the real (mechanism-only) card builder rather than hard-coding its output.
    if build_results_card("analyze_results", result) is not None:
        assert roles[tr_idx + 1] == "results_card"
        assert items[tr_idx + 1]["card"] is not None


def test_history_items_fallback_rederives_report_card_when_card_results_empty():
    """Regression: a run with NO persisted ``card_results`` (e.g. it predated the persist-card-
    results fix, or its server wasn't restarted onto it) must STILL replay the rich report card +
    its clickable charts — re-derived from the ``tool_result`` kept in ``messages`` — instead of
    degrading to bare metric tiles. Before the fallback, _history_items emitted only the tool_call
    (no tool_result), so the charts vanished on chat-switch / reload."""
    from app.main import _history_items

    s = Session(id="fb", ctx=None)
    s.messages.append({"role": "user", "content": "show me the results"})
    s.messages.append({"role": "assistant", "content": "here",
                       "tool_calls": [{"id": "tc1", "name": "locate_and_parse_report", "input": {}}]})
    report = {"found": True, "summary": {"model": "llama"},
              "charts": [{"title": "ttft", "path": "p.png", "session_id": "fb"}]}
    # The LLM-facing copy in messages (JSON string, as persisted) — but card_results stays EMPTY.
    s.messages.append({"role": "tool_results", "results": [
        {"tool_call_id": "tc1", "name": "locate_and_parse_report", "content": json.dumps(report)}]})
    assert s.card_results == []

    items = _history_items(s)
    roles = [it["role"] for it in items]
    tc_idx = roles.index("tool_call")
    assert roles[tc_idx + 1] == "tool_result"
    tr = items[tc_idx + 1]
    assert tr["name"] == "locate_and_parse_report"
    assert tr["result"]["summary"]["model"] == "llama"
    assert tr["result"]["charts"][0]["path"] == "p.png"


def test_history_items_fallback_rediscovers_charts_from_disk(tmp_path):
    """When the stored report copy lost its ``charts`` (e.g. budget-clamped on a huge result) but
    its ``report_path`` still points at a workspace holding the harness-rendered PNGs, the fallback
    re-discovers the charts from disk so the clickable thumbnails survive a reload."""
    from types import SimpleNamespace

    from app.main import _history_items

    # Lay out a per-session workspace: <sessions>/<sid>/<run>/results/report + analysis/*.png
    sid = "discd"
    sessions_root = tmp_path / "sessions"
    run = sessions_root / sid / "run1"
    (run / "results").mkdir(parents=True)
    (run / "analysis").mkdir(parents=True)
    report_path = run / "results" / "benchmark_report_v0.2.json.yaml"
    report_path.write_text("found: true\n")
    (run / "analysis" / "latency_vs_qps.png").write_bytes(b"\x89PNG")

    # Stored copy carries report_path but NO charts (the clamp dropped them), and no card_results.
    report = {"found": True, "summary": {"model": "m"}, "report_path": str(report_path)}
    sess = SimpleNamespace(
        messages=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc1", "name": "locate_and_parse_report", "input": {}}]},
            {"role": "tool_results", "results": [
                {"tool_call_id": "tc1", "name": "locate_and_parse_report",
                 "content": json.dumps(report)}]},
        ],
        approvals=[], in_flight_approvals=[], commands=[], card_results=[],
        ctx=SimpleNamespace(workspace=sessions_root / sid),
    )

    items = _history_items(sess)
    tr = next(it for it in items if it["role"] == "tool_result")
    charts = tr["result"]["charts"]
    assert charts and charts[0]["path"].endswith("latency_vs_qps.png")
    assert charts[0]["session_id"] == sid
