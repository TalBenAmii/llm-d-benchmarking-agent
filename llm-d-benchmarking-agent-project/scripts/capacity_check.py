#!/usr/bin/env python3
"""Capacity pre-flight bridge — runs the llm-d-benchmark repo's OWN capacity planner.

This is a thin, vetted bridge invoked *with the benchmark repo's virtualenv Python*
(which is the only interpreter where the ``planner`` package + ``transformers`` are
installed). It does NOT vendor any sizing logic: it imports and calls the repo's
``llmdbenchmark.utilities.capacity_validator.run_capacity_planner`` so the agent's
pre-flight answer is exactly the verdict the real ``standup`` would compute.

Contract (mechanism only — no judgment lives here):
  * argv[1] is a path to a JSON request file:
        {"plan_config": {...}, "ignore_failures": true|false}
  * stdout is a single JSON object:
        {"ok": true, "diagnostics": ["...", ...]}
     or, if the planner could not be imported / run:
        {"ok": false, "error": "..."}

The agent never types this command; ``app/capacity/planner.py`` builds the request
file inside the session workspace and runs this script through the allowlisted runner
(``shell=False``, scrubbed env). The allowlist constrains the single argument to a
``.json`` path, so there is no arbitrary-code surface beyond this audited file.
"""
from __future__ import annotations

import json
import logging
import sys


class _CollectingLogger:
    """Capture planner log lines (the planner logs each diagnostic) so the bridge can
    return them even on the paths where run_capacity_planner does not also collect them
    into its return value (e.g. the early 'fma/standalone disabled' info lines)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def log_info(self, msg: str, **_kw: object) -> None:
        self.lines.append(str(msg))

    def log_warning(self, msg: str, **_kw: object) -> None:
        self.lines.append(str(msg))


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit({"ok": False, "error": "usage: capacity_check.py <request.json>"})
        return 2

    try:
        with open(argv[1], encoding="utf-8") as fh:
            request = json.load(fh)
    except (OSError, ValueError) as exc:
        _emit({"ok": False, "error": f"cannot read request file: {exc}"})
        return 2

    plan_config = request.get("plan_config")
    if not isinstance(plan_config, dict):
        _emit({"ok": False, "error": "request.plan_config must be an object"})
        return 2
    ignore_failures = bool(request.get("ignore_failures", True))

    # Keep the planner's own httpx / hub chatter off stdout (stdout is our JSON channel).
    logging.disable(logging.CRITICAL)

    try:
        from llmdbenchmark.utilities.capacity_validator import run_capacity_planner
    except Exception as exc:  # ImportError or anything during import
        _emit({
            "ok": False,
            "error": (
                "could not import the benchmark repo's capacity planner "
                f"({type(exc).__name__}: {exc}). Run install.sh in the benchmark "
                "repo so its venv has the planner package installed."
            ),
        })
        return 1

    collector = _CollectingLogger()
    try:
        returned = run_capacity_planner(
            plan_config, logger=collector, ignore_failures=ignore_failures
        )
    except Exception as exc:  # planner blew up — surface it as a fact, don't crash
        _emit({"ok": False, "error": f"capacity planner raised: {type(exc).__name__}: {exc}"})
        return 1

    # run_capacity_planner returns the per-method diagnostics; the collector also has the
    # framing info lines. Prefer the returned list (it is the authoritative diagnostic set)
    # and fall back to the collected lines when the return value is empty (e.g. fma path).
    diagnostics = list(returned) if returned else list(collector.lines)
    _emit({"ok": True, "diagnostics": diagnostics})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
