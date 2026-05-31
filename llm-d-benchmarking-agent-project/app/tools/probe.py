"""Read-only tools: sense the environment, enumerate the catalog, read repo docs, and
locate + validate a benchmark report. None of these mutate anything, so the agent loop
runs them automatically (no approval).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.tools.context import ToolContext, ToolError
from app.validation.report import load_report, summarize_report, validate_report

# Tools the quickstart depends on; presence is checked via PATH (no command run).
_TOOLCHAIN = ["docker", "podman", "kubectl", "kind", "helm", "helmfile", "jq", "yq", "git", "uv", "python3"]

_ALL_CHECKS = [
    "container_runtime", "repos", "tools", "venv",
    "kind_clusters", "kube_context", "cluster_info", "namespaces", "stack",
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
    return result


# ---- helpers --------------------------------------------------------------

def _names_from_json(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [item.get("metadata", {}).get("name", "") for item in data.get("items", [])]


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
