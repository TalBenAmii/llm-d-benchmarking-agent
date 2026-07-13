#!/usr/bin/env python3
"""Capacity pre-flight bridge — runs the llm-d-benchmark repo's OWN capacity planner.

This is a thin, vetted bridge invoked *with the benchmark repo's virtualenv Python*
(which is the only interpreter where the ``planner`` package + ``transformers`` are
installed). It does NOT vendor any sizing logic: it imports and calls the repo's
``llmdbenchmark.utilities.capacity_validator.run_capacity_planner`` so the agent's
pre-flight answer is exactly the verdict the real ``standup`` would compute.

Alongside the sizing verdict it also answers "can your token pull these weights?" by
calling the repo's OWN ``llmdbenchmark.utilities.huggingface.check_model_access`` /
``GatedStatus`` (never reimplementing the gating check) and returning a token-free
``gated_access`` block. The HF token is read from the (already scrubbed) child env via
``os.environ["HF_TOKEN"]``; it is NEVER echoed into the result — only the upstream
util's ``detail`` text is (which the util never fills with the token value).

Contract (mechanism only — no judgment lives here):
  * argv[1] is a path to a JSON request file:
        {"plan_config": {...}, "ignore_failures": true|false}
  * stdout is a single JSON object:
        {"ok": true, "diagnostics": ["...", ...], "gated_access": {...}|null}
     where ``gated_access`` (present on success; null when no model id is declared, or
     a token-free degraded block when the gating util is unavailable) is:
        {"gated": bool|null, "authorized": bool|null, "reason": "...",
         "models": [{"model": "...", "gated": "...", "authorized": bool|null,
                     "reason": "..."}, ...]}
     or, if the planner could not be imported / run:
        {"ok": false, "error": "..."}

The agent never types this command; ``app/capacity/planner.py`` builds the request
file inside the session workspace and runs this script through the policy-allowed runner
(``shell=False``, scrubbed env). The policy constrains the single argument to a
``.json`` path, so there is no arbitrary-code surface beyond this audited file.
"""
from __future__ import annotations

import json
import logging
import os
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


def _gated_block(plan_config: dict) -> dict | None:
    """Reuse the repo's OWN gating check to answer "can your token pull these weights?".

    Returns a token-free, per-model + aggregate verdict, or ``None`` when no model id is
    declared. NEVER reimplements the gating logic — it imports and calls
    ``llmdbenchmark.utilities.huggingface.check_model_access`` (and reads ``GatedStatus`` /
    ``AccessStatus``). Any HF/import failure degrades to a token-free
    ``{"gated": None, "authorized": None, "reason": "gated check unavailable: <Exc>"}``
    block (NO traceback, NO token) and never crashes the capacity verdict.

    The model id is extracted exactly as the repo's ``capacity_validator._extract_params``
    does: ``model.huggingfaceId`` or ``model.name``, comma-split into one or more models.
    """
    try:
        model_cfg = plan_config.get("model", {}) or {}
        raw = model_cfg.get("huggingfaceId") or model_cfg.get("name", "")
        models = [m.strip() for m in str(raw).split(",") if m.strip()]
        if not models:
            return None

        # Token comes from the (already scrubbed) child env only. Mirror the repo's own
        # placeholder handling; NEVER put `token` in the returned dict.
        token = os.environ.get("HF_TOKEN") or None
        if token in ("", "REPLACE_TOKEN"):
            token = None

        from llmdbenchmark.utilities.huggingface import (
            AccessStatus,
            GatedStatus,
            check_model_access,
        )

        per_model: list[dict] = []
        for model_id in models:
            r = check_model_access(model_id, hf_token=token)
            if r.gated == GatedStatus.NOT_GATED:
                authorized: bool | None = None
            elif r.access is None:
                authorized = None
            else:
                authorized = r.access == AccessStatus.AUTHORIZED
            per_model.append({
                "model": model_id,
                "gated": r.gated.value,        # GatedStatus enum value (mechanism)
                "authorized": authorized,
                "reason": r.detail,            # upstream text; never contains the token
            })

        any_gated = any(m["gated"] == GatedStatus.GATED.value for m in per_model)
        gated_models = [m for m in per_model if m["gated"] == GatedStatus.GATED.value]
        blocked = [m for m in gated_models if m["authorized"] is not True]
        if not any_gated:
            agg_authorized: bool | None = None
        elif blocked:
            agg_authorized = False
        elif any(m["authorized"] is True for m in gated_models):
            agg_authorized = True
        else:
            agg_authorized = None  # gating status unknown for every gated model

        # Surface the first blocking detail when something is unauthorized, else the first.
        reason = (blocked[0]["reason"] if blocked else per_model[0]["reason"])
        return {
            "gated": any_gated,
            "authorized": agg_authorized,
            "reason": reason,
            "models": per_model,
        }
    except Exception as exc:  # HF/import/network failure — degrade, never crash, no token
        return {
            "gated": None,
            "authorized": None,
            "reason": f"gated check unavailable: {type(exc).__name__}",
            "models": [],
        }


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
    # Pair the sizing verdict with the "can your token pull these weights?" pre-flight,
    # using the repo's own gating check. Token-free; degrades on any HF/import failure.
    gated_access = _gated_block(plan_config)
    _emit({"ok": True, "diagnostics": diagnostics, "gated_access": gated_access})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
