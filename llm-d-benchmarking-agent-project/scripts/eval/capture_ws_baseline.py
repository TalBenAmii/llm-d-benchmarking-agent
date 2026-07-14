#!/usr/bin/env python3
"""Capture NORMALIZED WebSocket event-stream baselines for representative golden flows.

Drives the REAL FastAPI app over an in-process WebSocket (fastapi TestClient) with a flow's
``ScriptedProvider`` transcript and a ``CaptureRunner`` — the same hermetic sandbox
``tests/flows/harness.run_flow`` builds at the loop level, lifted to the WS layer (the swap
pattern of ``tests/eval/app_driver.install_isolated_state``). Approval gates are auto-answered
(approve, except the decline/steer baseline). No LLM, no subprocess, no cluster, no quota.

The recorded server→client stream is normalized (volatile fields stripped/replaced — see
``normalize_events``) and written to ``tests/flows/baselines/<name>.events.json``. These
baselines pin the CURRENT agent loop's wire behavior so a replacement engine can be diffed
against them event-for-event. NOT run in CI — the guard test
(``tests/flows/test_ws_baselines.py``) only asserts the committed files stay well-formed.

NOTE: unlike pytest (whose autouse fixture pre-grounds every ToolContext), captures run with
the skill-grounding gate LIVE — exactly as in production — so flows that skip fetch_key_docs
see the gate's refusal where pytest sees a plain validation error.

Run (from a worktree):

    PYTHONPATH=<worktree>/llm-d-benchmarking-agent-project REPOS_DIR=<repo-root> \\
      <repo-root>/llm-d-benchmarking-agent-project/.venv/bin/python \\
      scripts/eval/capture_ws_baseline.py [--only <baseline>] [--out <dir>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Make the project importable when run as a bare script (same as validate_flows.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from tests.flows.catalog_snapshot import frozen_catalog  # noqa: E402
from tests.flows.flows import FLOWS_BY_NAME  # noqa: E402
from tests.flows.harness import (  # noqa: E402
    CaptureRunner,
    ScriptedProvider,
    _materialize_repo_state,
)

# What each baseline is: the flow whose ScriptedProvider transcript drives the app, plus how
# approval gates are answered. The spread deliberately covers: a read-only preview (no gate at
# all), the full deploy→run→analyze happy path, the SAME path with the plan gate declined via a
# typed steer (the decline/steer wire behavior), a safety/validation refusal, an error path
# (non-zero run exit, honest no-report), and a knowledge-heavy tool-choice flow (DoE design).
BASELINES: dict[str, dict[str, Any]] = {
    "dry-run-preview": {"flow": "dry-run-preview"},
    "kind-quickstart": {"flow": "kind-quickstart"},
    "kind-quickstart-decline-steer": {"flow": "kind-quickstart", "decline": True},
    "safety-refusal": {"flow": "safety-refusal"},
    "error-run-nonzero-exit": {"flow": "error-run-nonzero-exit"},
    "doe-run-sweep": {"flow": "doe-run-sweep"},
}

# The decline/steer baseline answers the FIRST approval gate by TYPING this instead of clicking
# (the handler declines the gate AND queues the text as a steer — the wire path under test);
# every later gate is declined with a plain approval frame.
STEER_DECLINE_TEXT = ("Hold off — don't deploy or change anything. "
                      "Just tell me what you would have done.")

# Frame-count bound so a stream that never reaches `done` fails loudly instead of hanging.
DRAIN_CAP = 800

# Keys that are volatile by nature (resume cursors, wall-clock) — dropped wherever they appear.
_STRIP_KEYS = frozenset({
    "seq", "cur_seq", "duration_s", "running_elapsed_ms", "elapsed_ms",
    "created_at", "updated_at", "timestamp", "started_at", "finished_at",
})
# Long payload strings (doc bodies, welcome prose) are truncated: the baseline pins event
# order/shape/semantics, not full document text (which would churn on every knowledge edit).
_MAX_STR = 200
# Whole event types that are volatile by cadence, not content: resource_stats is the live
# poller (sample COUNT depends on wall-clock timing) and assistant_delta is token-by-token
# streaming (chunking is provider-dependent — the final assistant_text carries the full text).
# Both are NON_TURN_EVENTS (unbuffered) and are dropped from the baseline entirely.
_DROP_EVENTS = frozenset({"resource_stats", "assistant_delta"})


# ---- normalization -----------------------------------------------------------

def normalize_events(frames: list[dict[str, Any]], roots: dict[str, str]) -> list[dict[str, Any]]:
    """Strip/replace the volatile parts of a captured stream, keeping order + types + semantic
    payloads: ``seq``/timestamps/durations dropped, usage token numbers omitted, request ids →
    ``<approval-N>``, tool-call ids → ``<tc-N>``, absolute path roots (+ the session id) →
    placeholder tokens, long strings truncated."""
    tc_ids: dict[str, str] = {}
    req_ids: dict[str, str] = {}
    ordered_roots = sorted(roots.items(), key=lambda kv: len(kv[0]), reverse=True)

    def scrub(s: str) -> str:
        for real, token in ordered_roots:
            s = s.replace(real, token)
        if len(s) > _MAX_STR:
            s = s[:_MAX_STR] + " …<truncated>"
        return s

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items() if k not in _STRIP_KEYS}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str):
            return scrub(o)
        return o

    out: list[dict[str, Any]] = []
    for frame in frames:
        etype = frame.get("type")
        if etype in _DROP_EVENTS:
            continue
        if etype == "usage":
            # Real token numbers are volatile by contract; the baseline keeps only that a usage
            # event fired at this position.
            out.append({"type": etype, "data": {}})
            continue
        data = walk(frame.get("data") or {})
        if etype == "ready":
            # Persisted token tallies ride in the handshake — same volatility as `usage`.
            data.pop("usage", None)
            data.pop("context_window", None)
        if isinstance(data.get("request_id"), str):
            data["request_id"] = req_ids.setdefault(
                data["request_id"], f"<approval-{len(req_ids) + 1}>")
        for id_key in ("id", "tool_call_id"):  # tool_call/tool_result/results_card + command
            if isinstance(data.get(id_key), str):
                data[id_key] = tc_ids.setdefault(data[id_key], f"<tc-{len(tc_ids) + 1}>")
        out.append({"type": etype, "data": data})
    return out


# ---- capture -----------------------------------------------------------------

def _wait_for_background(app, timeout_s: float = 30.0) -> None:
    """Block until the connect-time background env pre-probe finishes, so its auto-run
    `command` frames land at a deterministic position (before the turn's events)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(t.done() for t in list(app.state.background_tasks)):
            return
        time.sleep(0.02)
    raise RuntimeError("the env pre-probe did not finish in time")


def capture_flow(flow, *, decline: bool = False) -> list[dict[str, Any]]:
    """Run one flow through the real app over in-process WS and return its NORMALIZED stream."""
    # Imported here (not module top) so main()'s env pinning happens before app.main's
    # import-time get_settings(); also keeps the guard test's import of this module light.
    from fastapi.testclient import TestClient

    from app.agent.session import SessionManager
    from app.config import Settings
    from app.main import app
    from app.security.policy import CommandPolicy

    with tempfile.TemporaryDirectory(prefix="ws-baseline-") as td:
        tmp = Path(td)
        repos_dir = tmp / "repos"
        _materialize_repo_state(repos_dir, flow.repo_state)
        # The same hermetic Settings run_flow builds — fully independent of the developer's .env.
        settings = Settings(
            _env_file=None,
            repos_dir=repos_dir,
            workspace_dir=tmp / "ws",
            llm_provider="anthropic",
            anthropic_api_key="not-used-in-scripted-mode",
            simulate=False,
        )
        policy = CommandPolicy.from_file(settings.command_policy_path)
        runner = CaptureRunner(settings.repo_paths, canned=flow.canned)

        def fake_which(name, *a, **k):
            return f"/usr/bin/{name}" if name in flow.tools_present else None

        frames: list[dict[str, Any]] = []
        with patch("app.tools.setup.probe.shutil.which", side_effect=fake_which), \
                TestClient(app) as client:
            # Repoint the live app at the hermetic sandbox — the install_isolated_state swap,
            # with the flow corpus's CaptureRunner/ScriptedProvider instead of SimRunner/fuzz.
            app.state.settings = settings
            app.state.policy = policy
            app.state.runner = runner
            app.state.channels = {}
            app.state.running = {}
            app.state.sessions = SessionManager(settings, policy, runner)
            app.state.provider = ScriptedProvider(flow.turns)
            app.state.provider_error = None
            with client.websocket_connect("/ws") as ws:
                ready = ws.receive_json()
                frames.append(ready)
                assert ready["type"] == "ready", f"first frame was not ready: {ready}"
                sid = ready["data"]["session_id"]
                # Pin the frozen catalog exactly as run_flow does (shadow the method too —
                # refresh=True lookups would re-scan the empty fake repo and wipe it).
                session = app.state.sessions.get(sid)
                frozen = frozen_catalog()
                session.ctx._catalog = frozen
                session.ctx.catalog = lambda *, refresh=False: frozen
                # Drain the brand-new-chat handshake, then let the background env pre-probe
                # finish so its command frames precede the turn deterministically.
                while frames[-1]["type"] != "suggestions":
                    frames.append(ws.receive_json())
                _wait_for_background(app)

                ws.send_json({"type": "user_message", "text": flow.mock_user_input})
                steered = False
                for _ in range(DRAIN_CAP):
                    ev = ws.receive_json()
                    frames.append(ev)
                    if ev["type"] == "approval_request":
                        if decline and not steered:
                            # Type INSTEAD of clicking: declines the gate + steers the turn.
                            steered = True
                            ws.send_json({"type": "user_message", "text": STEER_DECLINE_TEXT})
                        else:
                            ws.send_json({
                                "type": "approval",
                                "request_id": ev["data"]["request_id"],
                                "approved": not decline,
                            })
                    if ev["type"] == "done":
                        break
                else:
                    raise RuntimeError(f"{flow.name}: no `done` within {DRAIN_CAP} frames")

        roots = {
            str((tmp / "ws").resolve()): "<WS>",
            str(repos_dir.resolve()): "<REPOS>",
            str(tmp.resolve()): "<TMP>",
            str(_PROJECT_ROOT): "<PROJECT>",
            str(Path.home()): "<HOME>",
            sid: "<SID>",
        }
        return normalize_events(frames, roots)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", choices=sorted(BASELINES),
                        help="capture just this baseline (repeatable); default: all")
    parser.add_argument("--out", type=Path,
                        default=_PROJECT_ROOT / "tests" / "flows" / "baselines",
                        help="output directory (default: tests/flows/baselines)")
    args = parser.parse_args()

    # Pin the app's import-time/lifespan environment BEFORE app.main is imported (inside
    # capture_flow): a developer .env must not steer the provider/simulate mode, and the
    # startup self-check/GC must run against a throwaway workspace, never the real one.
    scratch = tempfile.mkdtemp(prefix="ws-baseline-app-")
    os.environ.update({
        "LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "not-used-in-scripted-mode",
        "SIMULATE": "0",
        "WORKSPACE_DIR": scratch,
    })

    names = args.only or sorted(BASELINES)
    args.out.mkdir(parents=True, exist_ok=True)
    for name in names:
        spec = BASELINES[name]
        flow = FLOWS_BY_NAME[spec["flow"]]
        decline = bool(spec.get("decline"))
        events = capture_flow(flow, decline=decline)
        doc = {
            "baseline": name,
            "flow": flow.name,
            "gates": "decline first via typed steer, then decline" if decline else "approve all",
            "captured_by": "scripts/eval/capture_ws_baseline.py",
            "events": events,
        }
        path = args.out / f"{name}.events.json"
        path.write_text(json.dumps(doc, indent=2) + "\n")
        n_gates = sum(1 for e in events if e["type"] == "approval_request")
        print(f"  {name}: {len(events)} events, {n_gates} gate(s), "
              f"ends {events[-1]['type']!r} -> {path.relative_to(_PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
