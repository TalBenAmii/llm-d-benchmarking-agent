"""check_capacity — pre-flight feasibility for a planned deployment.

The agent calls this at the plan gate (after proposing a SessionPlan, before standing
anything up) to answer "will this fit?" using the benchmark repo's OWN capacity planner.
It catches the OOM / won't-load / can't-serve cases *before* a 10-minute standup fails
opaquely — the proposal's "Configuration Explorer / Capacity Planner pre-flight".

Flow (all mechanism):
  1. Render the plan_config for the spec (scenario merged over repo defaults) + apply the
     agent's conversation-derived overrides (bigger model, longer context, a real GPU…).
  2. Write the request as a JSON file inside the session workspace.
  3. Run the vetted ``capacity_check.py`` bridge through the allowlisted runner, using the
     benchmark venv's Python (the only one with the ``planner`` package). Read-only ->
     auto-runs, no approval prompt.
  4. Parse the bridge's JSON and classify the planner's diagnostics into a verdict.

This is read-only: it reads repo files, does arithmetic, and may look up a model config
on HuggingFace. It never touches the cluster. Judgment about what to do with an
infeasible verdict lives in ``knowledge/capacity.md``.

Alongside the "will it fit?" verdict the bridge also returns a "can your token pull the
weights?" gated-access block (from the repo's OWN ``check_model_access``): ``gated`` /
``authorized`` / ``gated_reason`` are threaded onto the verdict so the agent sees the
PUBLIC / GATED+AUTHORIZED / GATED+UNAUTHORIZED facts at the plan gate. What to *say* for
each — and whether to offer Phase 30 secret-provisioning when gated+unauthorized — is the
agent's judgment, read from ``knowledge/capacity.md``, never an if/elif here. The HF token
stays backend-only (scrubbed child env) and never appears in the result or events.
"""
from __future__ import annotations

import json
from typing import Any

from app.capacity.planner import (
    CapacityError,
    classify_diagnostics,
    merge_gated_access,
    plan_config_for_spec,
)
from app.dig import dig, parse_bridge_dict
from app.tools.context import ToolContext, ToolError
from app.tools.gated_access import record_capacity_verdict

_REQUEST_FILENAME = "capacity_request.json"


async def check_capacity(
    ctx: ToolContext,
    *,
    spec: str,
    overrides: dict[str, Any] | None = None,
    enforce: bool = False,
) -> dict[str, Any]:
    """Pre-validate a spec's deployment against model + GPU capacity constraints.

    ``overrides`` lets the agent reflect the conversation onto the plan_config (e.g.
    ``{"model": "meta-llama/Llama-3.1-8B", "max_model_len": 8192, "gpu_memory_gb": 80}``).
    ``enforce`` mirrors the inverse of the repo's ``ignoreFailedValidation`` — when True,
    the planner tags shortfalls as ERROR (deployment-halting) rather than advisory.
    """
    bench_repo = ctx.settings.bench_repo
    try:
        plan_config, applied = plan_config_for_spec(bench_repo, spec, overrides=overrides)
    except CapacityError as exc:
        raise ToolError(str(exc)) from exc

    # ignore_failures is the planner's flag: True => advisory WARNING tags; False => ERROR
    # tags that would halt a real standup. enforce=True asks for the strict (halting) read.
    ignore_failures = not enforce

    ctx.workspace.mkdir(parents=True, exist_ok=True)
    request_path = ctx.workspace / _REQUEST_FILENAME
    request_path.write_text(
        json.dumps({"plan_config": plan_config, "ignore_failures": ignore_failures})
    )

    argv = ["capacity_check.py", str(request_path)]
    try:
        # Read-only per the allowlist -> auto-runs (no approval). The bridge is bounded;
        # a HuggingFace lookup is the slow part, so give it a generous-but-finite budget.
        res = await ctx.run_command(argv, timeout=120.0)
    except ToolError as exc:
        # e.g. the benchmark venv isn't installed yet -> the planner package is missing.
        raise ToolError(
            f"capacity pre-flight could not run: {exc}. If the benchmark venv isn't set "
            "up yet, run run_setup (install.sh) first."
        ) from exc

    bridge = _parse_bridge_output(res.output)
    if not bridge.get("ok"):
        return {
            "spec": spec,
            "ran": False,
            "applied_overrides": applied,
            "enforced": enforce,
            "error": bridge.get("error", "capacity bridge returned no diagnostics"),
            "note": (
                "Could not compute a capacity verdict. This is usually a missing benchmark "
                "venv (run_setup) or no network for the model-config lookup. Proceed with "
                "caution; the planner's verdict is unavailable."
            ),
            "stdout_tail": res.output[-1500:],
        }

    verdict = classify_diagnostics(bridge.get("diagnostics", []))
    # Thread the bridge's gated-access facts onto the verdict (pure field copy, no policy).
    # gated_access may be absent (legacy bridge) or None (no model id) — both leave the
    # verdict's gated/authorized/gated_reason at their None/"" defaults.
    merge_gated_access(verdict, bridge.get("gated_access"))
    verdict_facts = verdict.as_dict()
    # Record the gated-access verdict for the model just checked so the command guardrail can
    # REFUSE deploying it while it's gated+unauthorized (mechanism — see app/tools/gated_access.py).
    # Key by the model the agent explicitly checked (overrides['model']); fall back to the spec's
    # resolved model name so a default-model check is also tracked.
    checked_model = (overrides or {}).get("model") or dig(plan_config, "model", "name")
    record_capacity_verdict(
        ctx,
        model=checked_model,
        gated=verdict_facts.get("gated"),
        authorized=verdict_facts.get("authorized"),
        gated_reason=verdict_facts.get("gated_reason", ""),
    )
    return {
        "spec": spec,
        "ran": True,
        "applied_overrides": applied,
        "enforced": enforce,
        **verdict_facts,
        "note": _verdict_note(verdict),
        "gated_note": _GATED_NOTE,
    }


def _verdict_note(verdict: Any) -> str:
    """The human-facing verdict summary. Three states, not two: feasible / infeasible /
    INCONCLUSIVE — the last is when the planner BYPASSED VRAM sizing (0-replica spec or an
    un-fetchable model/GPU), so a clean run was NOT a fit verdict (real-2 #2). We must not
    let that read as feasible:true."""
    if verdict.feasible is None:
        return (
            "INCONCLUSIVE: " + (verdict.inconclusive_reason or "the planner did not size this "
            "deployment, so feasibility was NOT evaluated.") + " Do NOT treat this as feasible. "
            "See knowledge/capacity.md."
        )
    if verdict.feasible:
        return "Feasible per the benchmark repo's own capacity planner."
    return (
        "INFEASIBLE: the planner predicts this deployment would fail. See "
        "knowledge/capacity.md for how to read the errors and what to change before "
        "you stand anything up."
    )


# A static, NON-BRANCHING note attached to EVERY check_capacity result (gated or not) — it
# does not read the facts in Python (no if/elif). It states the conditional rule the model
# must apply, placed RIGHT NEXT TO the verdict so a model holding the gated/authorized facts
# reads the hard-stop directive at the exact decision moment (the always-on HARD_RULE alone
# proved insufficient for a flaky model). The full per-status wording (PUBLIC / GATED+
# AUTHORIZED / GATED+UNAUTHORIZED) still lives in knowledge/capacity.md — this is the cue, not
# a substitute for it. "capacity.md" must stay in the string (test_capacity_gated pins it).
_GATED_NOTE = (
    "Gated-model access facts (gated/authorized/gated_reason) are attached. APPLY THIS RULE "
    "NOW, before any mutating step: IF gated is true AND authorized is false, this is a HARD "
    "STOP — do NOT ensure_repos / run_setup / standup / run / smoketest. Read gated_reason and "
    "act: if it says NO token is configured cluster-side, CALL the provision_hf_secret tool NOW "
    "(it is approval-gated, so CALLING it IS how you propose it — the user consents at the "
    "approval prompt; do NOT merely describe it in prose or defer it via suggest_next_steps, and "
    "do NOT hand-roll a kubectl secret via run_shell), then RE-RUN check_capacity. If instead "
    "gated_reason says the configured token merely LACKS access, do NOT provision — point the "
    "user to huggingface.co/<model> to request access, then re-run. Proceed to standup ONLY once "
    "a fresh check_capacity returns authorized true. If gated is false (public) or authorized is "
    "true, just continue to the sizing verdict — say nothing about tokens. Full wording: the "
    "'Gated-model access pre-flight' section of knowledge/capacity.md."
)


def _parse_bridge_output(output: str) -> dict[str, Any]:
    """Parse the capacity bridge's single stdout JSON object (tolerant of leading log noise).

    Thin wrapper over the shared ``dig.parse_bridge_dict`` (shared with aggregate_runs)."""
    return parse_bridge_dict(output, "capacity")
