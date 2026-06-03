"""Read-only tools: sense the environment, enumerate the catalog, read repo docs, and
locate + validate a benchmark report. None of these mutate anything, so the agent loop
runs them automatically (no approval).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from app.tools.context import ToolContext, ToolError
from app.validation.report import load_report, summarize_report, validate_report

# Tools the quickstart depends on; presence is checked via PATH (no command run).
_TOOLCHAIN = ["docker", "podman", "kubectl", "kind", "helm", "helmfile", "jq", "yq", "git", "uv", "python3"]

_ALL_CHECKS = [
    "container_runtime", "repos", "tools", "venv",
    "kind_clusters", "kube_context", "cluster_info", "namespaces", "stack",
    "node_capacity",
]


async def probe_environment(
    ctx: ToolContext,
    *,
    checks: list[str] | str = "all",
    namespace: str | None = None,
) -> dict[str, Any]:
    """Gather precondition signals in one structured snapshot."""
    wanted = _ALL_CHECKS if (checks == "all" or not checks) else [c for c in checks if c in _ALL_CHECKS]
    out: dict[str, Any] = {}

    if "container_runtime" in wanted:
        out["container_runtime"] = await _probe_container_runtime(ctx)
    if "repos" in wanted:
        out["repos"] = _probe_repos(ctx)
    if "tools" in wanted:
        out["tools"] = {t: bool(shutil.which(t)) for t in _TOOLCHAIN}
    if "venv" in wanted:
        out["venv"] = _probe_venv(ctx)
    if "kind_clusters" in wanted:
        out["kind_clusters"] = await _probe_kind(ctx)
    if "kube_context" in wanted:
        out["kube_context"] = await _probe_kube_context(ctx)
    if "cluster_info" in wanted:
        out["cluster_info"] = await _probe_cluster_info(ctx)
    if "namespaces" in wanted:
        out["namespaces"] = await _probe_namespaces(ctx)
    if "stack" in wanted:
        out["stack"] = await _probe_stack(ctx, namespace)
    if "node_capacity" in wanted:
        out["node_capacity"] = await _probe_node_capacity(ctx)
    return out


async def _probe_container_runtime(ctx: ToolContext) -> dict[str, Any]:
    for rt in ("docker", "podman"):
        if not shutil.which(rt):
            continue
        try:
            res = await ctx.run_readonly([rt, "info"], timeout=15.0)
        except ToolError as exc:
            return {"type": rt, "present": True, "daemon_up": False, "error": str(exc)}
        if res.exit_code == 0:
            return {"type": rt, "present": True, "daemon_up": True}
        socket_err = "permission denied" in res.output.lower()
        return {
            "type": rt,
            "present": True,
            "daemon_up": False,
            "socket_permission_error": socket_err,
            "error_tail": res.output[-400:],
        }
    return {"type": None, "present": False, "daemon_up": False}


def _probe_repos(ctx: ToolContext) -> dict[str, Any]:
    s = ctx.settings
    out = {}
    for name, path in s.repo_paths.items():
        out[name] = {
            "present": (path / ".git").exists() or path.is_dir(),
            "is_git": (path / ".git").exists(),
            "path": str(path),
        }
    return out


def _probe_venv(ctx: ToolContext) -> dict[str, Any]:
    venv_py = ctx.settings.bench_repo / ".venv" / "bin" / "python"
    return {"exists": venv_py.exists(), "path": str(venv_py.parent.parent)}


async def _probe_kind(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kind"):
        return {"available": False, "clusters": []}
    res = await ctx.run_readonly(["kind", "get", "clusters"], timeout=15.0)
    clusters = [c for c in res.output.splitlines() if c.strip() and "No kind clusters" not in c]
    return {"available": True, "clusters": clusters, "exit_code": res.exit_code}


async def _probe_kube_context(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        return {"available": False, "context": None}
    res = await ctx.run_readonly(["kubectl", "config", "current-context"], timeout=10.0)
    ctx_name = res.output.strip() if res.exit_code == 0 else None
    return {"available": True, "context": ctx_name or None}


async def _probe_cluster_info(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        return {"reachable": False}
    res = await ctx.run_readonly(["kubectl", "cluster-info"], timeout=12.0)
    return {"reachable": res.exit_code == 0, "timed_out": res.timed_out}


async def _probe_namespaces(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        return {"available": False, "namespaces": []}
    res = await ctx.run_readonly(["kubectl", "get", "ns", "-o", "json"], timeout=12.0)
    names = _names_from_json(res.output) if res.exit_code == 0 else []
    return {"available": res.exit_code == 0, "namespaces": names}


async def _probe_stack(ctx: ToolContext, namespace: str | None) -> dict[str, Any]:
    """Lightweight 'is something already running here?' check for a namespace."""
    if not namespace or not shutil.which("kubectl"):
        return {"namespace": namespace, "checked": False}
    res = await ctx.run_readonly(["kubectl", "get", "pods", "-n", namespace, "-o", "json"], timeout=12.0)
    if res.exit_code != 0:
        return {"namespace": namespace, "checked": True, "exists": False, "pods": []}
    pods = _pod_summaries(res.output)
    running = [p for p in pods if p["ready"]]
    return {
        "namespace": namespace,
        "checked": True,
        "exists": True,
        "pod_count": len(pods),
        "ready_count": len(running),
        "detected": len(running) > 0,
        "pods": pods[:25],
    }


async def _probe_node_capacity(ctx: ToolContext) -> dict[str, Any]:
    """Report each node's allocatable/capacity CPU so the agent can right-size the harness
    launcher's CPU request for a small/single-node cluster (e.g. Kind). MECHANISM ONLY: it
    reports the numbers; WHETHER to lower LLMDBENCH_HARNESS_CPU_NR and to WHAT value is the
    agent's judgment, grounded in knowledge/harness_sizing.md — never a Python branch here.

    Uses the already-allowlisted read-only ``kubectl get nodes -o json``. Returns a structured,
    never-raising result; ``min_allocatable_cpu`` (the binding constraint for scheduling) is the
    minimum allocatable CPU across nodes, or None when it can't be determined."""
    if not shutil.which("kubectl"):
        return {"available": False, "nodes": [], "min_allocatable_cpu": None}
    res = await ctx.run_readonly(["kubectl", "get", "nodes", "-o", "json"], timeout=12.0)
    if res.exit_code != 0:
        return {"available": False, "nodes": [], "min_allocatable_cpu": None}
    nodes = _node_cpu_summaries(res.output)
    allocs = [n["allocatable_cpu"] for n in nodes if n["allocatable_cpu"] is not None]
    return {
        "available": True,
        "nodes": nodes,
        "min_allocatable_cpu": min(allocs) if allocs else None,
    }


def list_catalog(ctx: ToolContext, *, kinds: list[str] | None = None, refresh: bool = True) -> dict[str, Any]:
    """Enumerate specs/harnesses/workloads/scenarios from the repo on disk."""
    cat = ctx.catalog(refresh=refresh)
    if not kinds:
        return cat
    return {k: cat.get(k) for k in kinds if k in cat} | {"present": cat["present"]}


def read_repo_doc(ctx: ToolContext, *, path: str, max_bytes: int = 40_000) -> dict[str, Any]:
    """Read a file from inside one of the two read-only repos. Path traversal is blocked."""
    repos = ctx.settings.repo_paths
    candidate = Path(path)
    resolved: Path | None = None
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        # Try "<repo-name>/<rel>" first, then each repo root.
        first = candidate.parts[0] if candidate.parts else ""
        if first in repos:
            resolved = (repos[first].parent / candidate).resolve()
        else:
            for root in repos.values():
                trial = (root / candidate).resolve()
                if trial.exists():
                    resolved = trial
                    break
    if resolved is None:
        raise ToolError(f"could not resolve repo path {path!r}")

    roots = [r.resolve() for r in repos.values()]
    if not any(_is_within(resolved, root) for root in roots):
        raise ToolError(f"path {path!r} resolves outside the read-only repos — refused")
    if not resolved.is_file():
        raise ToolError(f"not a file: {resolved}")

    data = resolved.read_text(errors="replace")
    truncated = len(data.encode()) > max_bytes
    return {
        "path": str(resolved),
        "content": data[:max_bytes],
        "truncated": truncated,
    }


def fetch_key_docs(
    ctx: ToolContext,
    *,
    task: str | None = None,
    max_bytes_each: int = 20_000,
) -> dict[str, Any]:
    """Fetch the LIVE content of the authoritative repo docs pinned in
    knowledge/key_docs.yaml (optionally filtered to one task, e.g. 'quickstart').

    The *list* of docs is hard-coded (in key_docs.yaml); the *content* is read live from
    the cloned repos, so it is never a stale vendored copy. Read-only. Call this before
    proposing a deployment plan so the flow/flags come from the real procedure."""
    kfile = ctx.settings.knowledge_dir / "key_docs.yaml"
    if not kfile.is_file():
        return {"docs": [], "note": f"key_docs.yaml not found at {kfile}"}
    try:
        spec = yaml.safe_load(kfile.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ToolError(f"key_docs.yaml is not valid YAML: {exc}") from exc

    entries = spec.get("docs", []) if isinstance(spec, dict) else []
    if task:
        entries = [e for e in entries if e.get("task") == task]

    fetched: list[dict[str, Any]] = []
    for entry in entries:
        rel = entry.get("path", "")
        item: dict[str, Any] = {"path": rel, "task": entry.get("task"), "why": entry.get("why")}
        try:
            doc = read_repo_doc(ctx, path=rel, max_bytes=max_bytes_each)
            item.update(found=True, resolved=doc["path"], content=doc["content"], truncated=doc["truncated"])
        except ToolError as exc:
            item.update(found=False, reason=str(exc))
        fetched.append(item)

    tasks: set[str] = {
        str(e["task"])
        for e in spec.get("docs", [])
        if isinstance(e, dict) and e.get("task")
    }
    available = sorted(tasks)
    return {
        "task": task,
        "available_tasks": available,
        "docs": fetched,
        "found_count": sum(1 for d in fetched if d.get("found")),
    }


def _knowledge_files(ctx: ToolContext) -> list[Path]:
    """Every knowledge file (basename order), or empty if the dir is missing."""
    kdir = ctx.settings.knowledge_dir
    if not kdir.is_dir():
        return []
    files = list(kdir.glob("*.md")) + list(kdir.glob("*.yaml")) + list(kdir.glob("*.yml"))
    return sorted(files, key=lambda p: p.name)


def read_knowledge(ctx: ToolContext, *, name: str) -> dict[str, Any]:
    """Return the FULL text of ONE knowledge guide by its basename (e.g. 'capacity' or
    'capacity.md'). The system prompt inlines the core guides and indexes the rest; call
    this to load an on-demand guide BEFORE interpreting that kind of result. Read-only,
    auto-runs. Strictly validated: no path traversal, no absolute paths, no '..'."""
    files = _knowledge_files(ctx)
    valid = [f.name for f in files]
    requested = (name or "").strip()
    if not requested:
        return {"error": "missing 'name'", "valid_topics": valid}

    # Reject any path-bearing or traversal input outright — only a bare basename is allowed.
    if "/" in requested or "\\" in requested or ".." in requested or Path(requested).is_absolute():
        return {"error": f"invalid knowledge name {name!r}: pass a bare topic basename, "
                         f"not a path", "valid_topics": valid}

    # Match on exact basename, or on the stem (so 'capacity' -> 'capacity.md').
    match: Path | None = None
    for f in files:
        if f.name == requested or f.stem == requested:
            match = f
            break
    if match is None:
        return {"error": f"unknown knowledge topic {name!r}", "valid_topics": valid}

    return {"name": match.name, "topic": match.stem, "content": match.read_text()}


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
        search_roots.append(ctx.workspace.parent / session_id)
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
                "note": "synthetic results — simulate mode; nothing was actually benchmarked",
            }
        return {
            "found": False,
            "reason": "no benchmark_report_v0.2 file located",
            "searched": [str(p) for p in search_roots],
        }

    report = load_report(report_path)
    validation = validate_report(report, ctx.settings.benchmark_report_schema_path)
    result: dict[str, Any] = {
        "found": True,
        "report_path": str(report_path),
        "valid": validation.valid,
        "schema_version": validation.schema_version,
        "errors": validation.errors,
        "schema_deviations": validation.deviations,
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


def _discover_charts(report_path: Path, sessions_root: Path) -> list[dict[str, str]]:
    """Find chart images the harness rendered for this run, addressable via the artifact route.

    inference-perf writes plots (latency_vs_qps.png, throughput_vs_latency.png, …) into an
    ``analysis/`` tree beside the report. We locate the run's session dir (the path component
    directly under ``<workspace>/sessions``) so each chart can be expressed as a session-relative
    path the ``/api/sessions/<sid>/artifact`` route serves. Returns ``[]`` when the report isn't
    under the per-session workspace, or when the run produced no charts (CPU-sim / guidellm)."""
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
        charts.append({
            "title": img.stem.replace("_", " ").strip().capitalize(),
            "session_id": sid,
            "path": str(img.resolve().relative_to(session_dir)),
        })
    return charts

def _names_from_json(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [item.get("metadata", {}).get("name", "") for item in data.get("items", [])]


def _parse_cpu_quantity(value: Any) -> float | None:
    """Parse a Kubernetes CPU quantity into whole cores. K8s expresses CPU either as a bare
    number ("4", "0.5") or in millicores ("250m" == 0.25 cores). Returns None for anything
    unparseable (defensive — the agent treats absent CPU as 'unknown', never as zero)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("m"):
            return float(text[:-1]) / 1000.0
        return float(text)
    except ValueError:
        return None


def _node_cpu_summaries(text: str) -> list[dict[str, Any]]:
    """Per-node {name, allocatable_cpu, capacity_cpu} from `kubectl get nodes -o json`.
    Allocatable is what the scheduler can actually place against (capacity minus reserved);
    it is the figure that decides whether the launcher pod's CPU request fits."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("items", []):
        status = item.get("status", {})
        out.append({
            "name": item.get("metadata", {}).get("name", ""),
            "allocatable_cpu": _parse_cpu_quantity(status.get("allocatable", {}).get("cpu")),
            "capacity_cpu": _parse_cpu_quantity(status.get("capacity", {}).get("cpu")),
        })
    return out


def _pod_summaries(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for item in data.get("items", []):
        status = item.get("status", {})
        phase = status.get("phase")
        conds = {c.get("type"): c.get("status") for c in status.get("conditions", [])}
        out.append({
            "name": item.get("metadata", {}).get("name", ""),
            "phase": phase,
            "ready": conds.get("Ready") == "True" and phase == "Running",
        })
    return out


def _find_report(roots: list[Path]) -> Path | None:
    patterns = ["**/benchmark_report_v0.2*.yaml", "**/benchmark_report_v0.2*.json",
                "**/benchmark_report_v0.2*.yml"]
    candidates: list[Path] = []
    for root in roots:
        if not root or not root.exists():
            continue
        for pat in patterns:
            candidates.extend(root.glob(pat))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
