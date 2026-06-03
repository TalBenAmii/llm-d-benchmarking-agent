"""aggregate_runs — OPTIONAL cross-run aggregation over an EXISTING results dir (Phase 51).

This surfaces the benchmark repo's standalone ``docs/analysis/aggregate_runs.py`` script —
the ONE exploratory plotting/analysis script that is parameterizable against a results dir —
WITHOUT making it part of the automated probe->standup->run->report flow. The agent already
renders the harness PNGs inline and does its own SLO/goodput/Pareto math over the validated
Benchmark Report; the interactive notebook + the ``to_be_incorporated/`` plot templates stay
pointer-only (see knowledge/analysis.md). This is the one analysis script the agent runs
itself, and only when the user has REPEATED the same benchmark and wants run-to-run variance.

Flow (all mechanism — no judgment here):
  1. Resolve the benchmark repo's ``docs/analysis`` dir (where the upstream script lives).
  2. Validate that the caller-supplied results dir exists and the output dir is confined to
     the session workspace (never the read-only repos, never the results dir).
  3. Write a JSON request into the session workspace.
  4. Run the vetted ``scripts/aggregate_runs.py`` wrapper through the allowlisted runner. The
     wrapper imports the repo's OWN ``aggregate_runs`` module (never reimplements its math),
     reads the BR v0.2 reports under the results dir, and writes
     ``aggregated_summary.{txt,json}`` ONLY under the workspace output dir. Read-only ->
     auto-runs, no approval prompt.
  5. Parse the wrapper's JSON and surface the cross-run mean/std/min/max summary.

WHEN to aggregate (>=2 repeats of the same benchmark, run-to-run variance) is JUDGMENT in
knowledge/analysis.md, never an if/elif here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.tools.context import ToolContext, ToolError

_REQUEST_FILENAME = "aggregate_request.json"
_OUTPUT_DIRNAME = "aggregated"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


async def aggregate_runs(
    ctx: ToolContext,
    *,
    results_prefix: str,
    harness: str,
    stack: str,
    run_ids: list[str],
    output_name: str | None = None,
) -> dict[str, Any]:
    """Aggregate Benchmark Report v0.2 results across REPEATED runs of the same benchmark.

    ``results_prefix`` is the EXISTING results dir holding the per-run directories; ``harness``
    + ``stack`` + ``run_ids`` select which runs to combine (the upstream naming convention is
    ``{results_prefix}/{harness}_{run_id}_{stack}``). Writes ``aggregated_summary.{txt,json}``
    (cross-run mean/std/min/max) into a subdir of the session workspace and returns the parsed
    summary. Needs >=2 runs that carry a report — fewer is reported, nothing is written.
    """
    if not isinstance(run_ids, list) or len(run_ids) < 2:
        raise ToolError(
            "aggregate_runs needs at least 2 run_ids to compute run-to-run variance "
            "(mean/std/min/max); aggregate is only meaningful across repeated runs."
        )

    analysis_dir = (ctx.settings.bench_repo / "docs" / "analysis").resolve()
    if not (analysis_dir / "aggregate_runs.py").is_file():
        raise ToolError(
            f"benchmark repo's docs/analysis/aggregate_runs.py not found under {analysis_dir} "
            "— clone/install the benchmark repo first (ensure_repos)."
        )

    results_dir = Path(results_prefix).resolve()
    if not results_dir.is_dir():
        raise ToolError(
            f"results dir {results_prefix!r} does not exist; pass an EXISTING results dir "
            "from a completed run (this aggregates results, it does not run a benchmark)."
        )

    ctx.workspace.mkdir(parents=True, exist_ok=True)
    output_dir = (ctx.workspace / (output_name or _OUTPUT_DIRNAME)).resolve()
    # The summary MUST land inside the session workspace — never the read-only repos, never the
    # results dir we are only reading. A traversing output_name would escape; refuse it.
    if not _is_within(output_dir, ctx.workspace):
        raise ToolError("output_name must stay within the session workspace (no '..' escape).")

    request_path = ctx.workspace / _REQUEST_FILENAME
    request_path.write_text(json.dumps({
        "analysis_dir": str(analysis_dir),
        "results_prefix": str(results_dir),
        "harness": harness,
        "stack": stack,
        "run_ids": run_ids,
        "output": str(output_dir),
    }))

    argv = ["aggregate_runs.py", str(request_path)]
    try:
        # Read-only per the allowlist -> auto-runs (no approval). Reading + summarising a few
        # YAML reports is fast; keep a finite budget regardless.
        res = await ctx.run_command(argv, timeout=120.0)
    except ToolError as exc:
        raise ToolError(f"cross-run aggregation could not run: {exc}") from exc

    bridge = _parse_bridge_output(res.output)
    if not bridge.get("ok"):
        return {
            "ran": False,
            "results_prefix": str(results_dir),
            "run_ids": run_ids,
            "error": bridge.get("error", "aggregation bridge returned no summary"),
            "note": (
                "No aggregated summary was produced. The usual cause is fewer than 2 of the "
                "given run_ids having a Benchmark Report under the results dir, or a missing "
                "benchmark venv. Nothing was written."
            ),
            "stdout_tail": (bridge.get("stdout_tail") or res.output[-1500:]),
        }

    return {
        "ran": True,
        "results_prefix": str(results_dir),
        "run_ids": run_ids,
        "run_count": bridge.get("run_count"),
        "output_dir": bridge.get("output_dir"),
        "summary_path": bridge.get("summary_path"),
        "summary_json_path": bridge.get("summary_json_path"),
        "metrics": bridge.get("metrics", {}),
        "note": (
            "Cross-run mean/std/min/max over repeated runs. This is EXPLORATORY aggregation "
            "(the benchmark repo's own aggregate_runs.py) — it does NOT replace analyze_results' "
            "SLO/goodput/Pareto verdicts. See knowledge/analysis.md."
        ),
    }


def _parse_bridge_output(output: str) -> dict[str, Any]:
    """The wrapper prints exactly one JSON object on stdout. Be tolerant of leading noise by
    taking the last balanced JSON object on the captured stream (mirrors capacity_check)."""
    text = (output or "").strip()
    if not text:
        return {"ok": False, "error": "aggregation bridge produced no output"}
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.rfind("{")
    while start != -1:
        try:
            return json.loads(text[start:])
        except ValueError:
            start = text.rfind("{", 0, start)
    return {"ok": False, "error": f"aggregation bridge output was not JSON: {text[-500:]}"}
