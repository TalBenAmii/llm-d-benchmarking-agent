"""Agent tool: discover the LIVE llm-d stack behind an endpoint (Phase 56).

OPTIONAL richer ENVIRONMENT capture. Given an OpenAI-compatible endpoint URL, this runs the
standalone stack-discovery tool — ``llm-d-discover <url> -f benchmark-report`` — which traces
the live stack and emits BR-v0.2 ``scenario.stack[]`` Component dicts (model / role / replicas /
parallelism / accelerator). It is a strict COMPLEMENT to the agent's own endpoint probing
(``probe_environment`` / ``check_endpoint_readiness``), which stays the UNCONDITIONAL default —
this adds detail when the user wants a precise capture of what is actually deployed.

Mechanism only (mirrors ``readiness.py`` — a thin async handler over ``ctx.run_command`` validated
by the allowlist — and ``capacity.py``'s fail-loud "needs the benchmark venv" pattern):

  1. Run the allowlisted, READ-ONLY ``llm-d-discover`` (auto-runs; it has its OWN read-only
     Kubernetes RBAC + env-var redaction upstream, so it never mutates the cluster).
  2. Write the raw discovery JSON into ``ctx.workspace`` (the read-only repos are never written).
  3. Parse the JSON LIST of stack-component dicts and wrap it as the BR-v0.2 scenario-capture
     shape ``{"scenario": {"stack": [...]}}`` written into the workspace (the "report path"
     ingestion: this is the scenario/stack capture, NOT a full ``run``/``results`` report —
     ``--output-format benchmark-report`` emits only the stack, with no results block).
  4. Return structured stack FACTS (component count, models, roles, parallelism) for the agent.

All JUDGMENT — WHEN to run discovery vs rely on endpoint probing — lives in
``knowledge/stack_discovery.md`` (``read_knowledge('stack_discovery')``), never a Python branch.
The secret cluster-by-URL+TOKEN route stays backend-only (as for ``llmdbenchmark``); only a
non-secret kubeconfig FILE path is expressible here.
"""
from __future__ import annotations

import json
from typing import Any

from app.dig import dict_or_empty as _d
from app.dig import find_last_json
from app.tools.context import ToolContext, ToolError

# Where the raw discovery output + the wrapped scenario-capture land in the session workspace.
_DISCOVERY_FILENAME = "stack_discovery.json"
_SCENARIO_FILENAME = "discovered_scenario.json"


def _parse_components(output: str) -> list[dict[str, Any]]:
    """The tool prints exactly one JSON LIST of BR-v0.2 component dicts on stdout. Be tolerant
    of leading log noise (it logs to stderr, but stdout may be merged) by taking the last
    balanced JSON array on the captured stream. Raises ToolError if no array is found."""
    text = (output or "").strip()
    if not text:
        raise ToolError("stack discovery produced no output")
    result = find_last_json(text, "[")
    if isinstance(result, list):
        return result
    raise ToolError(
        f"stack discovery output was not a JSON list of components: {text[-500:]}"
    )


def _summarize_stack(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull plain FACTS out of the BR-v0.2 ``scenario.stack[]`` component dicts — mechanism
    only, no verdict. Each component carries ``metadata`` + ``standardized`` (and ``native``);
    inference-engine components additionally carry role / replicas / model / accelerator +
    nested parallelism. We surface counts, the distinct models, the roles, and the per-engine
    replicas + parallelism so the agent can describe what is actually deployed. Whatever a
    given shape MEANS for the benchmark is the LLM's call over knowledge/stack_discovery.md."""
    models: list[str] = []
    roles: list[str] = []
    engines: list[dict[str, Any]] = []
    tools: list[str] = []
    # Defensive: `_parse_components` validates only that the stream is a JSON *list*, not that each
    # element is a dict. A garbled stream with a non-dict element (or a non-dict standardized/model/
    # accelerator) must be coerced/skipped, never crash _summarize_stack with AttributeError.
    # `_d` (dig.dict_or_empty, imported above) coerces any non-dict to {}.
    for comp in components:
        comp = _d(comp)
        std = _d(comp.get("standardized"))
        meta = _d(comp.get("metadata"))
        tool = std.get("tool")
        if isinstance(tool, str) and tool not in tools:
            tools.append(tool)
        if std.get("kind") == "inference_engine":
            model = _d(std.get("model")).get("name")
            role = std.get("role")
            if isinstance(model, str) and model not in models:
                models.append(model)
            if isinstance(role, str) and role not in roles:
                roles.append(role)
            accel = _d(std.get("accelerator"))
            engines.append({
                "label": meta.get("label"),
                "model": model,
                "role": role,
                "replicas": std.get("replicas"),
                "tool_version": std.get("tool_version"),
                "accelerator": {
                    "model": accel.get("model"),
                    "count": accel.get("count"),
                    "parallelism": accel.get("parallelism"),
                },
            })
    return {
        "component_count": len(components),
        "inference_engine_count": len(engines),
        "models": models,
        "roles": roles,
        "tools": tools,
        "inference_engines": engines,
    }


async def discover_stack(
    ctx: ToolContext,
    *,
    endpoint_url: str,
    kubeconfig: str | None = None,
    context: str | None = None,
    filter_type: str | None = None,
) -> dict[str, Any]:
    """Discover the live llm-d stack behind ``endpoint_url`` as BR-v0.2 stack components.

    Read-only; auto-runs. Runs ``llm-d-discover <url> -f benchmark-report``, writes the raw
    output and the wrapped ``{"scenario": {"stack": [...]}}`` capture into the session
    workspace, and returns structured stack FACTS. This COMPLEMENTS — never replaces —
    ``probe_environment`` / ``check_endpoint_readiness``; WHEN to use it is
    ``read_knowledge('stack_discovery')``. The discovery tool must be installed in the
    benchmark venv (``pip install -e llm-d-benchmark/llm_d_stack_discovery``); if it isn't, the
    runner raises a clear "not set up yet" error and this returns a ``ran: False`` result.
    """
    argv = ["llm-d-discover", endpoint_url, "-f", "benchmark-report"]
    if kubeconfig:
        argv += ["-k", kubeconfig]
    if context:
        argv += ["-c", context]
    if filter_type:
        argv += ["--filter", filter_type]

    ctx.workspace.mkdir(parents=True, exist_ok=True)

    try:
        # Read-only per the allowlist -> auto-runs (no approval). A live trace is bounded; the
        # allowlist also pins a 120s deadline.
        res = await ctx.run_command(argv, timeout=120.0)
    except ToolError as exc:
        # e.g. the discovery tool isn't installed in the benchmark venv yet.
        return {
            "ran": False,
            "endpoint_url": endpoint_url,
            "error": str(exc),
            "note": (
                "Stack discovery could not run. The standalone tool is a self-contained "
                "subpackage that install.sh does NOT install; if the benchmark venv lacks it, "
                "run `pip install -e llm-d-benchmark/llm_d_stack_discovery` into that venv. "
                "Endpoint probing (probe_environment / check_endpoint_readiness) still works "
                "as the default — see knowledge/stack_discovery.md."
            ),
        }

    if res.exit_code != 0:
        # The CLI exits non-zero if discovery hit errors (e.g. unreachable endpoint / cluster).
        return {
            "ran": False,
            "endpoint_url": endpoint_url,
            "exit_code": res.exit_code,
            "error": "llm-d-discover exited non-zero; the endpoint or cluster may be unreachable.",
            "stdout_tail": (res.output or "")[-1500:],
            "note": (
                "Discovery failed; fall back to endpoint probing (probe_environment / "
                "check_endpoint_readiness). See knowledge/stack_discovery.md."
            ),
        }

    components = _parse_components(res.output)

    # The "report path" ingestion: wrap the discovered components as the BR-v0.2
    # scenario-capture shape and persist both the raw output and the wrapped capture into the
    # session workspace (the read-only repos are never written).
    scenario_capture = {"scenario": {"stack": components}}
    raw_path = ctx.workspace / _DISCOVERY_FILENAME
    raw_path.write_text(res.output)
    scenario_path = ctx.workspace / _SCENARIO_FILENAME
    scenario_path.write_text(json.dumps(scenario_capture, indent=2))

    summary = _summarize_stack(components)
    return {
        "ran": True,
        "endpoint_url": endpoint_url,
        "scenario_capture_path": str(scenario_path),
        "discovery_output_path": str(raw_path),
        "stack": summary,
        "note": (
            "Discovered the LIVE stack as BR-v0.2 scenario.stack components. This is richer "
            "ENVIRONMENT capture that COMPLEMENTS endpoint probing (probe_environment / "
            "check_endpoint_readiness) — it does not replace it. Call "
            "read_knowledge('stack_discovery') for WHEN to use this and how to read it."
        ),
    }
