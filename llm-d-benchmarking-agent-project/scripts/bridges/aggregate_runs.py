#!/usr/bin/env python3
"""Cross-run aggregation bridge — runs the llm-d-benchmark repo's OWN aggregate_runs.

This is a thin, vetted bridge (Phase 51) that lets the agent OPTIONALLY surface the
benchmark repo's standalone ``docs/analysis/aggregate_runs.py`` script against an EXISTING
results dir, WITHOUT making it part of the automated probe->standup->run->report flow. It
does NOT vendor or reimplement any aggregation math: it imports the repo's own
``aggregate_runs`` module from ``<bench_repo>/docs/analysis/`` and calls its ``main()`` so
the cross-run mean/std/min/max summary is exactly what the upstream script would produce.

It is READ-ONLY with respect to the analysed results: it READS the Benchmark Report v0.2
files under ``--results-prefix`` and WRITES ``aggregated_summary.{txt,json}`` ONLY under a
caller-supplied ``output`` directory that the calling tool confines to the session
workspace. The read-only sibling repos and the results dir are NEVER written.

Contract (mechanism only — no judgment lives here):
  * argv[1] is a path to a JSON request file (workspace-confined ``.json``):
        {"analysis_dir": "<bench_repo>/docs/analysis",
         "results_prefix": "<existing results dir>",
         "harness": "inference-perf",
         "stack": "llm-d-7b-base",
         "run_ids": ["...", "..."],          # >=1; the script needs >=2 with reports
         "output": "<session-workspace output dir>"}
  * stdout is a single JSON object:
        {"ok": true, "output_dir": "...", "summary_path": "...",
         "summary_json_path": "...", "metrics": {...}, "run_count": N, "stdout_tail": "..."}
     or, on any failure (bad request, module not importable, <2 runs, …):
        {"ok": false, "error": "..."}

The agent never types this command; ``app/tools/analyze/aggregate_runs.py`` builds the request file
inside the session workspace and runs this script through the allowlisted runner
(``shell=False``, scrubbed env). The allowlist constrains the single argument to a ``.json``
path with no ``..`` traversal, so there is no arbitrary-code surface beyond this audited
file and the (read-only) upstream module it imports. WHEN to aggregate (>=2 repeats of the
same benchmark, run-to-run variance) is JUDGMENT in knowledge/analysis.md, not here.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _load_module(analysis_dir: str):
    """Import the repo's OWN ``aggregate_runs`` module from ``<bench_repo>/docs/analysis``.

    Never reimplements its math — we add the directory to ``sys.path`` and import it so the
    summary is exactly the upstream script's output. The module imports only stdlib + an
    optional ``yaml`` (with a JSON fallback), so it loads under any interpreter.
    """
    d = Path(analysis_dir).resolve()
    if not (d / "aggregate_runs.py").is_file():
        raise FileNotFoundError(
            f"benchmark repo's aggregate_runs.py not found under {d}; clone/install the "
            "benchmark repo first"
        )
    sys.path.insert(0, str(d))
    import aggregate_runs as upstream  # noqa: E402  (path injected above)

    return upstream


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _emit({"ok": False, "error": "usage: aggregate_runs.py <request.json>"})
        return 2

    try:
        with open(argv[1], encoding="utf-8") as fh:
            request = json.load(fh)
    except (OSError, ValueError) as exc:
        _emit({"ok": False, "error": f"cannot read request file: {exc}"})
        return 2
    if not isinstance(request, dict):
        _emit({"ok": False, "error": "request must be a JSON object"})
        return 2

    analysis_dir = request.get("analysis_dir")
    results_prefix = request.get("results_prefix")
    harness = request.get("harness")
    stack = request.get("stack")
    run_ids = request.get("run_ids")
    output = request.get("output")
    if not all(isinstance(x, str) and x for x in (analysis_dir, results_prefix, harness, stack, output)):
        _emit({"ok": False, "error": "request needs non-empty analysis_dir/results_prefix/harness/stack/output strings"})
        return 2
    if not isinstance(run_ids, list) or not run_ids or not all(isinstance(r, str) and r for r in run_ids):
        _emit({"ok": False, "error": "request.run_ids must be a non-empty list of strings"})
        return 2

    try:
        upstream = _load_module(analysis_dir)
    except Exception as exc:  # ImportError / FileNotFoundError / anything during import
        _emit({"ok": False, "error": f"could not import the benchmark aggregate_runs module ({type(exc).__name__}: {exc})"})
        return 1

    # Drive the upstream script through its OWN argv-based main() so behaviour is identical.
    # Its prints go to stdout (which is our JSON channel), so capture them and surface only a
    # bounded tail. main() returns 0 on success, 1 when <2 runs had reports (or none found).
    script_argv = [
        "aggregate_runs.py",
        "--results-prefix", results_prefix,
        "--harness", harness,
        "--stack", stack,
        "--run-ids", *run_ids,
        "--output", output,
    ]
    buf = io.StringIO()
    old_argv = sys.argv
    try:
        sys.argv = script_argv
        with contextlib.redirect_stdout(buf):
            rc = upstream.main()
    except SystemExit as exc:  # argparse error path
        rc = exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # the upstream script raised — surface it as a fact, don't crash
        _emit({"ok": False, "error": f"aggregate_runs raised: {type(exc).__name__}: {exc}", "stdout_tail": buf.getvalue()[-1500:]})
        return 1
    finally:
        sys.argv = old_argv

    tail = buf.getvalue()[-2000:]
    if rc != 0:
        _emit({"ok": False, "error": "aggregate_runs found fewer than 2 runs with benchmark reports to aggregate", "stdout_tail": tail})
        return 1

    # The script wrote two files into `output`; read back the JSON summary for the agent.
    json_path = os.path.join(output, "aggregated_summary.json")
    text_path = os.path.join(output, "aggregated_summary.txt")
    metrics: dict = {}
    run_count: int | None = None
    try:
        with open(json_path, encoding="utf-8") as fh:
            summary = json.load(fh)
        metrics = summary.get("metrics", {}) or {}
        run_count = summary.get("aggregation", {}).get("run_count")
    except (OSError, ValueError):
        pass  # the text summary still exists; surface what we have

    _emit({
        "ok": True,
        "output_dir": output,
        "summary_path": text_path,
        "summary_json_path": json_path,
        "run_count": run_count,
        "metrics": metrics,
        "stdout_tail": tail,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
