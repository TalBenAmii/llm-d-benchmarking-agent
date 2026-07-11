"""Self-contained, shareable report-card HTML for a reproducibility provenance bundle.

``render_report_card(bundle) -> str`` produces ONE ``.html`` string with **zero external
assets**: system-stack fonts, all CSS inlined in a ``<style>`` block, the official llm-d mark as an
inline ``<svg>``. No jinja (keeps it dependency-light + hermetic): every interpolated value is
HTML-escaped via ``_esc`` so a value in the bundle (a model name, a SHA, a config body) can never
inject markup.

Sections (per the spec): Header (model / harness / workload / spec / timestamp / agent version);
Results (BR-v0.2 headline tiles + the full percentile ladder from ``summarize_report``); Provenance
(both repo SHAs + dirty flags, resolved config in a collapsed ``<details>``, env snapshot, knowledge
hash); Reproduce (the copy-paste regenerate command + a "requires the same SHAs" caveat); and a loud
Honesty banner whenever either repo was dirty or its SHA was unavailable.

Pure mechanism: it RENDERS the already-captured, already-validated facts; it computes no metric and
fabricates nothing (a missing field is simply omitted).
"""
from __future__ import annotations

import datetime
import html
from typing import Any

from app.dig import dict_or_empty as _d
from app.dig import dig

# The official llm-d mark (llm-d.ai/img/llm-d-icon.svg, coordinates rebased + minified) as an
# INLINE <svg> element (HTML5 needs no xmlns; same mark as app/ui/index.html). Inlined, not a data
# URI / <img src>, so the document carries ZERO URLs at all (not even the SVG namespace URI) —
# keeping the "no external asset link" guarantee airtight.
_LOGO_SVG = (
    '<svg viewBox="0 0 69.37 78.7" width="37" height="42" role="img" aria-label="llm-d">'
    '<g stroke-width="1.74">'
    '<path fill="#595959" stroke="#595959" d="m52.97,43.51c-0.83,0-1.65,0.21-2.39,0.64l-10.73,6.19c-1.48,0.85-2.39,2.43-2.39,4.14v12.38c0,1.7,0.91,3.29,2.39,4.14l10.73,6.19c1.47,0.85,3.3,0.85,4.78,0l10.73-6.19c1.48-0.85,2.39-2.43,2.39-4.14v-12.38c0-1.7-0.91-3.28-2.39-4.14v0l-10.73-6.19c-0.74-0.43-1.56-0.64-2.39-0.64zm0,3.74c0.18,0,0.35,0.05,0.52,0.14l10.73,6.19v0c0.32,0.18,0.51,0.52,0.51,0.89v12.38c0,0.37-0.19,0.71-0.52,0.89l-10.73,6.19c-0.32,0.19-0.71,0.19-1.03,0l-10.73-6.19v0c-0.32-0.18-0.51-0.52-0.51-0.89v-12.38c0-0.37,0.19-0.71,0.52-0.89l10.73-6.19c0.16-0.09,0.34-0.14,0.52-0.14z" />'
    '<path fill="#595959" stroke="#595959" stroke-linecap="round" d="m64.73,35.25v22.34a1.87,1.87,0,0,0,1.87,1.87,1.87,1.87,0,0,0,1.87-1.87v-24.51z" />'
    '<path fill="#7f317f" stroke="#7f317f" d="m38.07,26.55c0.41,0.72,1,1.33,1.74,1.76l10.7,6.24c1.47,0.86,3.3,0.86,4.77,0.02l10.75-6.15c1.48-0.85,2.4-2.42,2.4-4.13l0.05-12.38c0.01-1.7-0.9-3.29-2.37-4.15l-10.7-6.24c-1.47-0.86-3.3-0.86-4.78-0.02l-10.75,6.15c-1.48,0.85-2.4,2.42-2.4,4.13v0l-0.05,12.39c0,0.85,0.22,1.67,0.63,2.39zm3.25-1.86c-0.09-0.15-0.14-0.33-0.13-0.52l0.05-12.39h0c0-0.37,0.2-0.7,0.52-0.89l10.75-6.15c0.32-0.18,0.71-0.18,1.03,0l10.7,6.24c0.32,0.19,0.51,0.52,0.51,0.9l-0.05,12.39h0c0,0.37-0.2,0.7-0.52,0.89l-10.75,6.15c-0.32,0.18-0.71,0.18-1.03,0l-10.7-6.24c-0.16-0.09-0.29-0.22-0.38-0.38z" />'
    '<path fill="#7f317f" stroke="#7f317f" stroke-linecap="round" d="m25.07,20.44,19.4-11.09a1.87,1.87,0,0,0,0.7-2.55,1.87,1.87,0,0,0-2.55-0.7l-21.27,12.17z" />'
    '<path fill="#7f317f" stroke="#7f317f" d="m31.23,47.82c0.41-0.71,0.65-1.53,0.65-2.39l0.03-12.39c0-1.7-0.91-3.29-2.38-4.14l-10.71-6.22c-1.47-0.86-3.3-0.86-4.78-0.01l-10.74,6.17c-1.48,0.85-2.39,2.43-2.4,4.13l-0.03,12.39c0,1.7,0.91,3.29,2.38,4.14l10.71,6.22c1.47,0.86,3.3,0.86,4.78,0.01v0l10.74-6.17c0.74-0.42,1.34-1.03,1.75-1.74zm-3.23-1.88c-0.09,0.15-0.22,0.28-0.38,0.38l-10.74,6.17v0c-0.32,0.18-0.71,0.18-1.03,0l-10.71-6.22c-0.32-0.19-0.51-0.52-0.51-0.89l0.03-12.39c0-0.37,0.19-0.71,0.52-0.89l10.74-6.17v0c0.32-0.18,0.71-0.18,1.03,0l10.71,6.22c0.32,0.19,0.52,0.52,0.51,0.89l-0.03,12.39c0,0.19-0.05,0.36-0.14,0.52z" />'
    '<path fill="#7f317f" stroke="#7f317f" stroke-linecap="round" d="m32.46,62.14-19.32-11.22a1.87,1.87,0,0,0-2.56,0.68,1.87,1.87,0,0,0,0.68,2.56l21.19,12.31z" />'
    '</g></svg>'
)

# System font stacks only — no Google Fonts <link> (which would be an external asset).
_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; padding: 0; background: #14121a; color: #ece9f1; line-height: 1.5;
}
.wrap { max-width: 900px; margin: 0 auto; padding: 28px 22px 64px; }
header.card-head { display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }
header.card-head svg { width: 37px; height: 42px; flex: 0 0 auto; }
h1 { font-size: 1.45rem; margin: 0; }
h2 { font-size: 1.05rem; margin: 30px 0 10px; border-bottom: 1px solid #3a3447; padding-bottom: 6px; }
.sub { color: #b3acc4; font-size: .9rem; margin: 2px 0 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.tile { background: #211d2b; border: 1px solid #332d42; border-radius: 10px; padding: 10px 12px; }
.tile .k { color: #9b93b0; font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; }
.tile .v { font-size: 1.1rem; font-weight: 600; margin-top: 2px; }
table { width: 100%; border-collapse: collapse; font-size: .86rem; margin-top: 6px; }
th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid #2c2738; }
th:first-child, td:first-child { text-align: left; }
thead th { color: #9b93b0; font-weight: 600; }
code, pre, .mono {
  font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
pre { background: #0f0d14; border: 1px solid #332d42; border-radius: 8px; padding: 12px;
      overflow-x: auto; font-size: .82rem; white-space: pre-wrap; word-break: break-word; }
.cmd { background: #0f0d14; border: 1px solid #5a3a6a; border-radius: 8px; padding: 12px;
       font-size: .9rem; }
.repo { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.repo .chip { background: #211d2b; border: 1px solid #332d42; border-radius: 999px;
              padding: 4px 12px; font-size: .82rem; }
.dirty { color: #ffb4b4; border-color: #7a2a2a !important; }
.banner { background: #3a1414; border: 2px solid #b04040; color: #ffd7d7; border-radius: 10px;
          padding: 14px 16px; margin: 14px 0 4px; font-weight: 600; }
details { margin-top: 8px; }
summary { cursor: pointer; color: #c8a8e0; }
.muted { color: #8d869f; font-size: .82rem; }
footer { margin-top: 40px; color: #6f697e; font-size: .76rem; }
"""

# Latency/throughput rows + the percentile columns rendered in the ladder (mirror the UI table).
_LADDER_ROWS = (
    ("TTFT", ("latency", "ttft")),
    ("TPOT", ("latency", "tpot")),
    ("ITL", ("latency", "itl")),
    ("request latency", ("latency", "request_latency")),
    ("total tok/s", ("throughput", "total_token_rate")),
    ("output tok/s", ("throughput", "output_token_rate")),
    ("request rate", ("throughput", "request_rate")),
)
_LADDER_COLS = ("mean", "p50", "p90", "p95", "p99", "p99p9")


def _esc(v: Any) -> str:
    """HTML-escape any value to text (None -> "")."""
    if v is None:
        return ""
    return html.escape(str(v), quote=True)


def _fmt_num(v: Any) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return _esc(v)
    return f"{v:.4g}"


# ``_d`` (``dig.dict_or_empty``): coerce a non-dict child of the untrusted, on-disk bundle to ``{}``
# so a corrupt/forged bundle (e.g. ``"resolved_config": "oops"``) renders an empty section instead of
# crashing the card with ``AttributeError``. The bundle is read with only a top-level
# ``isinstance(dict)`` check, so child VALUES must be coerced before any ``.get``.


def _header(bundle: dict[str, Any]) -> str:
    summary = _d(bundle.get("report_summary"))
    model = bundle.get("model") or summary.get("model") or "model"
    created = bundle.get("created_at")
    when = ""
    if isinstance(created, (int, float)):
        when = datetime.datetime.fromtimestamp(created, tz=datetime.UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    bits = []
    for label, key in (("harness", "harness"), ("workload", "workload"),
                       ("spec", "spec"), ("namespace", "namespace")):
        val = bundle.get(key) or summary.get(key)
        if val:
            bits.append(f"{_esc(label)}: {_esc(val)}")
    sub = " · ".join(bits)
    av = _esc(bundle.get("agent_version"))
    return (
        f'<header class="card-head">{_LOGO_SVG}'
        f"<div><h1>Benchmark report — {_esc(model)}</h1>"
        f'<p class="sub">{sub}</p>'
        f'<p class="muted">captured {_esc(when)} · agent v{av}</p></div></header>'
    )


def _honesty_banner(bundle: dict[str, Any]) -> str:
    repos = _d(bundle.get("repos"))
    dirty = bool(bundle.get("dirty")) or any(_d(s).get("dirty") for s in repos.values())
    unavailable = [n for n, s in repos.items() if _d(s).get("unavailable")]
    if not dirty and not unavailable:
        return ""
    parts = ["⚠ Reproducibility warning."]
    if dirty:
        parts.append(
            "One or more repos had UNCOMMITTED changes when this run was captured — an exact "
            "re-run needs the same working tree, not just the recorded SHA."
        )
    if unavailable:
        parts.append(
            "Repo SHA was UNAVAILABLE for: " + ", ".join(_esc(n) for n in unavailable)
            + " (empty/absent at capture). The results are real, but this run was NOT captured "
            "as exactly reproducible."
        )
    return f'<div class="banner">{" ".join(parts)}</div>'


def _results(bundle: dict[str, Any]) -> str:
    s = _d(bundle.get("report_summary"))
    tiles: list[tuple[str, Any]] = []

    def add(label: str, value: Any) -> None:
        if value is not None and value != "":
            tiles.append((label, value))

    add("requests", s.get("requests_total"))
    add("success %", s.get("success_rate_pct"))
    add("TTFT mean", _stat_text(dig(s, "latency", "ttft"), "mean"))
    add("TTFT p99", _stat_text(dig(s, "latency", "ttft"), "p99"))
    add("latency mean", _stat_text(dig(s, "latency", "request_latency"), "mean"))
    add("per-token (TPOT)", _stat_text(dig(s, "latency", "tpot"), "mean"))
    add("total tok/s", _stat_text(dig(s, "throughput", "total_token_rate"), "mean"))
    add("output tok/s", _stat_text(dig(s, "throughput", "output_token_rate"), "mean"))
    add("req/s", _stat_text(dig(s, "throughput", "request_rate"), "mean"))

    tiles_html = "".join(
        f'<div class="tile"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div></div>'
        for k, v in tiles
    )
    return (
        "<h2>Results</h2>"
        f'<div class="tiles">{tiles_html}</div>'
        + _ladder(s)
    )


def _stat_text(stat: Any, key: str) -> str | None:
    if not isinstance(stat, dict):
        return None
    v = stat.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    units = stat.get("units")
    return f"{_fmt_num(v)}{(' ' + str(units)) if units else ''}"


def _ladder(summary: dict[str, Any]) -> str:
    rows_html = []
    for label, (fam, key) in _LADDER_ROWS:
        stat = dig(summary, fam, key)
        if not isinstance(stat, dict):
            continue
        units = stat.get("units")
        name = f"{label}{(' (' + str(units) + ')') if units else ''}"
        cells = "".join(
            f"<td>{_fmt_num(stat.get(c)) if isinstance(stat.get(c), (int, float)) and not isinstance(stat.get(c), bool) else '—'}</td>"
            for c in _LADDER_COLS
        )
        rows_html.append(f"<tr><th>{_esc(name)}</th>{cells}</tr>")
    if not rows_html:
        return ""
    head = "".join(f"<th>{_esc(c)}</th>" for c in _LADDER_COLS)
    return (
        "<details open><summary>All percentiles</summary>"
        f"<table><thead><tr><th>metric</th>{head}</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></details>"
    )


def _provenance(bundle: dict[str, Any]) -> str:
    repos = _d(bundle.get("repos"))
    chips = []
    for name, st in repos.items():
        st = _d(st)
        if st.get("unavailable"):
            chips.append(f'<span class="chip dirty">{_esc(name)} @ (unavailable)</span>')
            continue
        sha = st.get("sha") or "?"
        ref = st.get("ref")
        dirty = st.get("dirty")
        label = f"{_esc(name)} @ {_esc(sha)}"
        if ref:
            label += f" ({_esc(ref)})"
        if dirty:
            label += " — DIRTY"
        cls = "chip dirty" if dirty else "chip"
        chips.append(f'<span class="{cls}">{label}</span>')
    repos_html = f'<div class="repo">{"".join(chips)}</div>' if chips else ""

    cfg = _d(bundle.get("resolved_config"))
    if cfg.get("found") and cfg.get("body"):
        cfg_html = (
            f'<details><summary>Resolved run-config ({_esc(cfg.get("path"))})</summary>'
            f"<pre>{_esc(cfg.get('body'))}</pre></details>"
        )
    else:
        cfg_html = f'<p class="muted">{_esc(cfg.get("note") or "No resolved run-config captured.")}</p>'

    env = bundle.get("env_snapshot")
    if env:
        import json

        env_html = (
            "<details><summary>Environment snapshot</summary>"
            f"<pre>{_esc(json.dumps(env, indent=2, default=str))}</pre></details>"
        )
    else:
        env_html = '<p class="muted">No environment snapshot captured.</p>'

    kh = _esc(bundle.get("knowledge_version"))
    digest = _esc(bundle.get("report_digest"))
    return (
        "<h2>Provenance</h2>"
        + repos_html
        + cfg_html
        + env_html
        + f'<p class="muted">knowledge hash: <span class="mono">{kh}</span></p>'
        + f'<p class="muted">report digest: <span class="mono">{digest}</span></p>'
    )


def _reproduce(bundle: dict[str, Any]) -> str:
    cmd = bundle.get("regenerate_command") or ""
    repos = _d(bundle.get("repos"))
    sha_bits = []
    for name, st in repos.items():
        st = _d(st)
        sha_bits.append(f"{name}@{st.get('sha') or '(unavailable)'}")
    caveat = (
        "Requires " + ", ".join(_esc(b) for b in sha_bits)
        + " and a stack serving the captured model. <span class=\"mono\">-c</span> is run-only "
        "(it replays the resolved config against a live stack; it does not stand one up)."
    ) if sha_bits else ""
    return (
        "<h2>Reproduce</h2>"
        f'<div class="cmd mono">{_esc(cmd)}</div>'
        + (f'<p class="muted">{caveat}</p>' if caveat else "")
    )


def render_report_card(bundle: dict[str, Any]) -> str:
    """Render a single self-contained HTML report card for a provenance bundle."""
    bundle = bundle or {}
    model = _esc(bundle.get("model") or dig(bundle, "report_summary", "model") or "report")
    body = (
        _header(bundle)
        + _honesty_banner(bundle)
        + _results(bundle)
        + _provenance(bundle)
        + _reproduce(bundle)
        + f'<footer>Generated by the llm-d benchmarking agent · bundle '
          f'<span class="mono">{_esc(bundle.get("bundle_id"))}</span> · '
          f"this file is self-contained (no external assets).</footer>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>Benchmark report — {model}</title>"
        f"<style>{_CSS}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>'
    )
