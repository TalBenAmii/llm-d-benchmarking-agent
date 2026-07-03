"""Phase 11 — structured logging + correlation IDs (hermetic).

Pins the contract the acceptance criteria require:
(a) the JSON formatter renders one valid JSON object per line with the STANDARD keys;
(b) a corr_id bound at the (simulated) WS boundary propagates — within ONE turn — to log
    records emitted by the agent loop, a tool, AND the command runner;
(c) the LOG_FORMAT=text path works.

No network / cluster / GPU: the one real command is `git rev-parse --is-inside-work-tree`
(read-only, auto-runs, and the project worktree is a git repo), exercising the runner's own
log records through the allowlist gate.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.agent.loop import AgentLoop
from app.agent.session import Session
from app.config import get_settings
from app.llm.provider import AssistantTurn, ToolCall
from app.observability.logging import (
    ContextFilter,
    JsonFormatter,
    get_corr_id,
    new_corr_id,
    setup_logging,
)
from app.observability.logging import bind as log_bind
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


# --------------------------------------------------------------------------- helpers


class _CapturingHandler(logging.Handler):
    """Records the LogRecord objects (after the ContextFilter has stamped them) AND the
    formatted text, so a test can assert both the rendered output and the per-record fields."""

    def __init__(self, formatter: logging.Formatter):
        super().__init__()
        self.addFilter(ContextFilter())
        self.setFormatter(formatter)
        self.records: list[logging.LogRecord] = []
        self.formatted: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # format() runs the formatter; the filter already ran (handler-level filters fire
        # before emit), so the correlation fields are present on the record.
        self.formatted.append(self.format(record))
        self.records.append(record)


def _attach(handler: logging.Handler):
    """Attach a handler to the root logger at DEBUG for the duration of a test."""
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    return root, prev_level


def _session(tmp_path) -> Session:
    s = get_settings()
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(s.repo_paths)
    ctx = ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")
    return Session(id="sess-xyz", ctx=ctx)


class _FakeProvider:
    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn


# --------------------------------------------------------------------------- (a) formatter


def test_json_formatter_renders_standard_keys_and_is_valid_json():
    handler = _CapturingHandler(JsonFormatter())
    root, prev = _attach(handler)
    try:
        log = logging.getLogger("app.test.fmt")
        with log_bind(corr_id="abc123", session_id="sess1", tool="probe_environment"):
            log.info("hello", extra={"exit_code": 0, "exe": "git"})
    finally:
        root.removeHandler(handler)
        root.setLevel(prev)

    assert len(handler.formatted) == 1
    line = handler.formatted[0]
    # One line, no embedded newline → newline-delimited JSON.
    assert "\n" not in line
    obj = json.loads(line)  # MUST be valid JSON
    # Standard keys always present.
    for key in ("timestamp", "level", "logger", "message"):
        assert key in obj, f"missing standard key {key}"
    assert obj["level"] == "INFO"
    assert obj["logger"] == "app.test.fmt"
    assert obj["message"] == "hello"
    # Correlation fields from the bound context.
    assert obj["corr_id"] == "abc123"
    assert obj["session_id"] == "sess1"
    assert obj["tool"] == "probe_environment"
    # Structured extras flow through as native JSON types.
    assert obj["exit_code"] == 0
    assert obj["exe"] == "git"


def test_json_formatter_omits_unset_correlation_fields():
    handler = _CapturingHandler(JsonFormatter())
    root, prev = _attach(handler)
    try:
        logging.getLogger("app.test.fmt2").warning("no context here")
    finally:
        root.removeHandler(handler)
        root.setLevel(prev)
    obj = json.loads(handler.formatted[0])
    # Unset correlation fields are absent (not empty strings) — no bogus values.
    assert "corr_id" not in obj
    assert "session_id" not in obj
    assert "tool" not in obj
    assert obj["level"] == "WARNING"


def test_json_formatter_renders_exception_and_stays_valid_json():
    handler = _CapturingHandler(JsonFormatter())
    root, prev = _attach(handler)
    try:
        log = logging.getLogger("app.test.exc")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
    finally:
        root.removeHandler(handler)
        root.setLevel(prev)
    obj = json.loads(handler.formatted[0])  # still valid JSON
    assert obj["message"] == "failed"
    assert "ValueError: boom" in obj["exc_info"]


# --------------------------------------------------------------------------- (b) propagation


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
async def test_corr_id_propagates_loop_tool_and_runner_within_one_turn(tmp_path):
    """The acceptance test: ONE corr_id bound at the WS boundary appears on records from the
    agent loop, a tool dispatch, AND the command runner — all within a single turn."""
    handler = _CapturingHandler(JsonFormatter())
    root, prev = _attach(handler)

    # A turn that runs a read-only command (git status) so the runner actually executes and
    # logs. read_only → auto-runs, no approval. The project worktree is a git repo, so the
    # command exits 0 regardless of where pytest is launched.
    turns = [
        AssistantTurn(text="Checking.", tool_calls=[ToolCall(
            "tc1", "run_shell", {"command": "git status -s"})]),
        AssistantTurn(text="Done.", tool_calls=[]),
    ]

    async def emit(_t, _p):
        return None

    async def request_approval(_kind, _payload):  # nothing mutating should reach here
        raise AssertionError("no approval expected for a read-only command")

    session = _session(tmp_path)
    loop = AgentLoop(_FakeProvider(turns))

    the_corr = new_corr_id()
    try:
        # Bind exactly as main.py does at the WS boundary, then run the turn under it.
        with log_bind(corr_id=the_corr, session_id=session.id):
            assert get_corr_id() == the_corr
            await loop.run_turn(session, "is this a git repo?",
                                emit=emit, request_approval=request_approval)
    finally:
        root.removeHandler(handler)
        root.setLevel(prev)

    # Every captured line is valid JSON.
    objs = [json.loads(line) for line in handler.formatted]
    assert objs, "no log records captured"

    # Group records by their emitting logger, keeping only those carrying OUR corr_id.
    by_logger: dict[str, list[dict]] = {}
    for o in objs:
        if o.get("corr_id") == the_corr:
            by_logger.setdefault(o["logger"], []).append(o)

    # The loop emitted turn + tool lifecycle records under the corr_id.
    loop_recs = by_logger.get("app.agent.loop", [])
    loop_msgs = {r["message"] for r in loop_recs}
    assert {"turn.start", "tool.call.start", "tool.call.result", "turn.end"} <= loop_msgs

    # The tool layer emitted the command-exec record (mode + exe + duration + exit code),
    # and it carries the tool name bound by the loop for the dispatch.
    ctx_recs = [r for r in by_logger.get("app.tools.context", []) if r["message"] == "command.exec"]
    assert ctx_recs, "no command.exec record under the corr_id"
    cmd = ctx_recs[0]
    assert cmd["exe"] == "bash"  # run_shell runs `bash -lc <command>` (bounded-cardinality label)
    assert cmd["mode"] == "read_only"
    assert cmd["exit_code"] == 0
    assert "duration_s" in cmd
    assert cmd["tool"] == "run_shell"  # the loop bound the tool name for this dispatch

    # The command RUNNER emitted its own record, under the SAME corr_id (propagated purely
    # via contextvars — nothing was threaded through).
    runner_recs = [r for r in by_logger.get("app.security.runner", [])
                   if r["message"] == "runner.exec.start"]
    assert runner_recs, "no runner.exec.start record under the corr_id"
    # The runner logs the RESOLVED binary path. run_shell runs `bash -lc "git status -s"`, so the
    # OS-level binary is bash (the tool layer logs exe="bash" to match) — assert by basename.
    assert Path(runner_recs[0]["exe"]).name == "bash"

    # ALL THREE layers share one and the same corr_id (the crux of the acceptance criterion).
    assert {"app.agent.loop", "app.tools.context", "app.security.runner"} <= set(by_logger)
    # session_id propagated too.
    assert all(r.get("session_id") == session.id for recs in by_logger.values() for r in recs)


# --------------------------------------------------------------------------- (c) text path


def test_text_format_path_emits_human_line_with_corr_id():
    setup_logging(level="INFO", log_format="text")
    root = logging.getLogger()
    # The single configured handler renders text (not JSON).
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert not isinstance(handler.formatter, JsonFormatter)

    # Capture what the text formatter produces for a bound record.
    captured: list[str] = []
    real_emit = handler.emit

    def _spy(record):
        captured.append(handler.format(record))
        real_emit(record)

    handler.emit = _spy  # type: ignore[method-assign]
    try:
        with log_bind(corr_id="textcorr", session_id="s"):
            logging.getLogger("app.test.text").info("a dev line")
    finally:
        handler.emit = real_emit  # type: ignore[method-assign]
        # Restore JSON default so we don't leak text config into other tests.
        setup_logging(level="INFO", log_format="json")

    assert captured, "text formatter produced no output"
    line = captured[0]
    # Not JSON: a compact human line carrying the corr_id and the message.
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "[textcorr]" in line
    assert "a dev line" in line
    assert "INFO" in line


def test_setup_logging_is_idempotent_replaces_handlers():
    setup_logging(level="DEBUG", log_format="json")
    root = logging.getLogger()
    first = list(root.handlers)
    assert len(first) == 1
    assert root.level == logging.DEBUG
    # Calling again does not stack handlers (replaces, not appends).
    setup_logging(level="INFO", log_format="json")
    assert len(root.handlers) == 1
    assert root.handlers[0] is not first[0]
    assert root.level == logging.INFO


def test_unknown_log_level_defaults_to_info():
    setup_logging(level="NOPE", log_format="json")
    assert logging.getLogger().level == logging.INFO
    setup_logging(level="INFO", log_format="json")  # restore
