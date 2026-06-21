"""Read-only Benchmark Report location + parsing.

Find the newest Benchmark Report v0.2 under a results dir (or the session workspace), validate
it against the repo's live schema, summarize it for non-experts, and surface any chart images
the harness rendered next to it. Read-only, auto-runs (no approval).

Split out of app/tools/probe.py (which had grown into a ~1,100-line module spanning three
unrelated tool families) so the report surface is independently navigable. probe.py re-exports
``locate_and_parse_report`` (and ``_discover_charts``, used by tests) for backwards
compatibility; new code should import them from here.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.tools.context import ToolContext, ToolError
from app.validation.report import load_report, summarize_report, validate_report


def locate_and_parse_report(
    ctx: ToolContext,
    *,
    results_dir: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Find the newest Benchmark Report v0.2 in the given dir (or the session workspace),
    validate it against the repo schema, and return a non-expert summary."""
    search_roots: list[Path] = []
    if results_dir:
        search_roots.append(Path(results_dir))
    if session_id:
        search_roots.append(_session_root(ctx, session_id))
    search_roots.append(ctx.workspace)

    report_path = _find_report(search_roots)
    if report_path is None:
        # Simulate mode: no real report exists (nothing was benchmarked), so synthesize a
        # clearly-labeled summary the agent can narrate. Does NOT read the live schema —
        # the bench repo may be absent in this mode.
        if ctx.settings.simulate:
            return {
                "found": True, "simulated": True, "valid": True,
                "summary": {"requests": 120, "success_rate": 1.0,
                            "throughput_tokens_per_s": 5000, "ttft_ms_p50": 130, "ttft_ms_p90": 210,
                            "itl_ms_mean": 47},
                # No real benchmark ran, so there is no genuine generation time. Surface
                # generated_at: null explicitly (rather than omitting it) so the agent cannot
                # mistake this synthetic payload for a freshly-timestamped run and label it
                # "today's report" (sim-2 09:10). `simulated: true` already says it isn't real.
                "generated_at": None,
                "generated_at_source": "none (synthetic simulate-mode report — not a real run)",
                "note": "synthetic results — simulate mode; nothing was actually benchmarked",
            }
        return {
            "found": False,
            "reason": "no benchmark_report_v0.2 file located",
            "searched": [str(p) for p in search_roots],
        }

    report = load_report(report_path)
    validation = validate_report(report, ctx.settings.benchmark_report_schema_path)
    generated_at, gen_source = _report_generated_at(report, report_path)
    result: dict[str, Any] = {
        "found": True,
        "report_path": str(report_path),
        "valid": validation.valid,
        "schema_version": validation.schema_version,
        "errors": validation.errors,
        "schema_deviations": validation.deviations,
        # When this report was produced (ISO-8601). Prefer the report's OWN run.time.end/start
        # (authoritative experiment time); fall back to the report FILE mtime when the report
        # carries no time block. Lets the agent tell a stale leftover report from a fresh one
        # instead of adopting the user's "today's report" framing (sim-2 09:10).
        "generated_at": generated_at,
        "generated_at_source": gen_source,
    }
    if validation.valid:
        result["summary"] = summarize_report(report)
    # Surface any per-run chart images the harness rendered next to the report (inference-perf
    # writes latency/throughput PNGs into a sibling analysis/ tree). Pure mechanism: glob the
    # files and hand the UI session-relative paths it can fetch from the artifact route. Empty
    # on the CPU-sim quickstart / guidellm, which render no charts — never fabricated.
    charts = _discover_charts(report_path, ctx.workspace.parent)
    if charts:
        result["charts"] = charts
    return result


# ---- helpers --------------------------------------------------------------

_CHART_SUFFIXES = (".png", ".svg")


def _session_root(ctx: ToolContext, session_id: str) -> Path:
    """Resolve ``<sessions_root>/<session_id>`` and CONTAIN it inside the sessions root.

    ``session_id`` is an UNVALIDATED agent-supplied string. Without containment a value like
    ``"../../../../etc"`` (or an absolute path) escapes ``ctx.workspace.parent`` (the per-session
    ``sessions/`` dir) and lets ``_find_report`` glob+read a ``benchmark_report_v0.2*`` file
    ANYWHERE on disk — a path-traversal arbitrary-file read. Mirrors the BUG-028 containment for
    probe.py spec paths: resolve, then require the result to stay within the root. Raises
    :class:`ToolError` (the loop relays it as a clean ``{"error": ...}``) on escape — never a raw
    exception. The legitimate case (a plain session id) is unchanged."""
    sessions_root = ctx.workspace.parent.resolve()
    candidate = (ctx.workspace.parent / session_id).resolve()
    if candidate != sessions_root and not candidate.is_relative_to(sessions_root):
        raise ToolError(
            f"invalid session_id {session_id!r}: must name a session within the sessions "
            "root (no path traversal, absolute paths, or '..')"
        )
    return candidate


def _report_generated_at(report: dict[str, Any], report_path: Path) -> tuple[str | None, str]:
    """When was this report produced? Returns ``(iso8601_or_None, source)``.

    Prefers the report's OWN ``run.time.end`` (then ``start``) — the authoritative experiment
    timestamp the harness wrote (BR v0.2 ``RunTime``, ISO-8601 ``date-time``). Falls back to
    the report FILE mtime (UTC ISO-8601) when the report carries no usable time block, so the
    agent ALWAYS gets a concrete "when" to compare against "today" rather than guessing.
    Pure mechanism — it never fabricates a time; the mtime fallback is clearly labelled."""
    run = report.get("run") if isinstance(report, dict) else None
    time_block = run.get("time") if isinstance(run, dict) else None
    if isinstance(time_block, dict):
        for field in ("end", "start"):
            val = time_block.get(field)
            if isinstance(val, str) and val.strip():
                return val, f"report run.time.{field}"
            # PyYAML may hand back a datetime despite the report-loader's string resolver
            # (e.g. a JSON report, or a non-default loader) — normalize to ISO-8601.
            if isinstance(val, datetime):
                return val.isoformat(), f"report run.time.{field}"
    try:
        mtime = report_path.stat().st_mtime
    except OSError:
        return None, "unavailable"
    iso = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
    return iso, "report file mtime (report carried no run.time — may be a stale leftover)"


def _discover_charts(report_path: Path, sessions_root: Path) -> list[dict[str, str]]:
    """Find chart images the harness rendered for this run, addressable via the artifact route.

    inference-perf writes plots (latency_vs_qps.png, throughput_vs_latency.png, …) into an
    ``analysis/`` tree beside the report. We locate the run's session dir (the path component
    directly under ``<workspace>/sessions``) so each chart can be expressed as a session-relative
    path the ``/api/sessions/<sid>/artifact`` route serves. Returns ``[]`` when the report isn't
    under the per-session workspace, or when the run produced no charts (CPU-sim / guidellm).

    The CLI's optional ``--analyze`` (Phase 40) writes three EXTRA plot families into nested
    subdirs of ``analysis/`` — ``distributions/`` (per-request), ``session/`` (session-lifecycle),
    and ``graphs/`` (Prometheus time-series). ``rglob`` already finds them; we carry the family
    subdir (the path component(s) between ``analysis/`` and the file) into each chart's ``title``
    and an explicit ``family`` field so the UI can GROUP them and the three families don't collide
    on bare filenames. Pure mechanism — no judgment, no per-family branching."""
    try:
        rel_to_sessions = report_path.resolve().relative_to(sessions_root.resolve())
    except ValueError:
        return []  # report located via an explicit results_dir outside the session workspace
    if not rel_to_sessions.parts:
        return []
    sid = rel_to_sessions.parts[0]
    session_dir = (sessions_root / sid).resolve()
    # Walk up from the report to the nearest ancestor that holds an ``analysis/`` dir (the run
    # dir), without escaping the session dir.
    run_dir = report_path.resolve().parent
    analysis: Path | None = None
    while True:
        if (run_dir / "analysis").is_dir():
            analysis = run_dir / "analysis"
            break
        if run_dir == session_dir or run_dir.parent == run_dir:
            break
        run_dir = run_dir.parent
    if analysis is None:
        return []
    charts: list[dict[str, str]] = []
    for img in sorted(analysis.rglob("*")):
        if img.suffix.lower() not in _CHART_SUFFIXES or not img.is_file():
            continue
        # The family is the subdir(s) of analysis/ holding this image (e.g. "distributions",
        # "session", "graphs" from --analyze; "" for charts written directly into analysis/).
        # Carrying it into the title keeps the three --analyze families from colliding on bare
        # filenames and lets the UI group them; pure mechanism, no per-family branching.
        family = str(img.resolve().parent.relative_to(analysis.resolve()))
        if family == ".":
            family = ""
        name = img.stem.replace("_", " ").strip().capitalize()
        if family:
            family_label = family.replace("_", " ").replace("/", " / ").title()
            title = f"{family_label}: {name}"
        else:
            title = name
        chart: dict[str, str] = {
            "title": title,
            "session_id": sid,
            "path": str(img.resolve().relative_to(session_dir)),
        }
        if family:
            chart["family"] = family
        charts.append(chart)
    return charts


def _find_report(roots: list[Path]) -> Path | None:
    """Pick the report to summarize, honouring root PRECEDENCE.

    ``roots`` is ordered most-specific-first (an explicit ``results_dir``, then an explicit
    ``session_id`` dir, then the broad session workspace). The first root that contains ANY
    report wins, and within that root the newest-by-mtime is chosen; later, broader roots are
    only consulted as a FALLBACK. Merging every root into one global newest-by-mtime pool
    would silently let an UNRELATED, more-recent report elsewhere in the workspace override the
    run the caller explicitly pointed at via ``results_dir`` — returning another run's metrics
    under the caller's chosen dir (a wrong-report selection)."""
    patterns = ["**/benchmark_report_v0.2*.yaml", "**/benchmark_report_v0.2*.json",
                "**/benchmark_report_v0.2*.yml"]
    for root in roots:
        if not root or not root.exists():
            continue
        candidates: list[Path] = []
        for pat in patterns:
            candidates.extend(root.glob(pat))
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    return None
