"""Mutating tools for preparing the environment: clone the read-only repos if missing,
and run the benchmark repo's install.sh to build its venv.

Both go through the approval gate (via ``ctx.run_command``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import BENCH_REPO_NAME, GUIDE_REPO_NAME
from app.tools.context import ToolContext, ToolError

_KNOWN_REPOS = (BENCH_REPO_NAME, GUIDE_REPO_NAME)


async def ensure_repos(ctx: ToolContext, *, repos: list[str] | None = None, ref: str | None = None) -> dict[str, Any]:
    """Clone any missing repos into the canonical sibling location. Idempotent;
    never deletes or overwrites an existing directory."""
    repos = repos or list(_KNOWN_REPOS)
    repos_dir = ctx.settings.resolved_repos_dir
    results: list[dict[str, Any]] = []

    # ALWAYS refresh the catalog before returning — even if a later repo's clone is rejected at
    # the approval gate (ApprovalRejected), denied (ToolError), or over-quota (QuotaError), which
    # propagate straight out of ``ctx.run_command`` mid-loop. An EARLIER repo in the same call may
    # already have been cloned successfully; without this ``finally`` the early exception would skip
    # the refresh and leave the per-context catalog cache STALE (empty, present=False). Downstream
    # callers that read ``ctx.catalog()`` WITHOUT refresh — ``plan.validate_plan`` and the
    # ``catalog_for_allowlist`` check inside every ``run_command``/``run_readonly`` — would then
    # reject every valid spec/harness/workload/ref for the rest of the turn even though the repo is
    # on disk and usable. (This is exactly the "after cloning, call ctx.catalog(refresh=True) or
    # later tools see the stale catalog" hazard, realized by an early-exit path.)
    try:
        for name in repos:
            if name not in _KNOWN_REPOS:
                results.append({"repo": name, "action": "unknown_repo",
                                "note": f"only {list(_KNOWN_REPOS)} are supported"})
                continue
            path = ctx.settings.repo_paths[name]
            if (path / ".git").exists():
                results.append({"repo": name, "action": "already_present", "path": str(path)})
                continue
            if path.exists() and any(path.iterdir()):
                results.append({
                    "repo": name, "action": "partial_detected", "path": str(path),
                    "note": "directory exists but is not a git repo; leaving it untouched — "
                            "please remove/fix it manually if you want a fresh clone",
                })
                continue

            url = f"https://github.com/llm-d/{name}"
            argv = ["git", "clone", url]
            if ref:
                argv += ["--branch", ref]
            repos_dir.mkdir(parents=True, exist_ok=True)
            res = await ctx.run_command(argv, cwd=repos_dir, timeout=900.0)
            ok = res.exit_code == 0 and (path / ".git").exists()
            entry: dict[str, Any] = {
                "repo": name,
                "action": "cloned" if ok else "clone_failed",
                "path": str(path),
                "exit_code": res.exit_code,
            }
            if not ok:
                entry["log_tail"] = res.output[-600:]
            results.append(entry)
    finally:
        ctx.catalog(refresh=True)  # the catalog may now be populated

    return {"results": results}


async def run_setup(ctx: ToolContext, *, use_uv: bool = True, force: bool = False) -> dict[str, Any]:
    """Run ``./install.sh [--uv]`` in the benchmark repo to build its venv and verify
    system tools. Idempotent and re-runnable."""
    bench = ctx.settings.bench_repo
    if not (bench / "install.sh").exists():
        raise ToolError(f"install.sh not found in {bench} — clone the repo first (ensure_repos)")

    venv_py = bench / ".venv" / "bin" / "python"
    if venv_py.exists() and not force:
        # Already set up; report without re-running unless forced.
        return {
            "ran": False, "already_setup": True,
            "venv_exists": True, "python_version": _venv_python_version(bench),
            "note": "venv already present; pass force=true to re-run install.sh",
        }

    argv = ["install.sh"]
    if use_uv:
        argv.append("--uv")
    res = await ctx.run_command(argv, timeout=2400.0)
    return {
        "ran": True,
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
        "venv_exists": venv_py.exists(),
        "python_version": _venv_python_version(bench),
        "missing_tools": _scan_missing_tools(res.output),
        "log_tail": res.output[-2000:],
    }


# ---- helpers --------------------------------------------------------------

def _venv_python_version(bench_repo: Path) -> str | None:
    cfg = bench_repo / ".venv" / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text().splitlines():
        if line.strip().startswith("version"):
            # "version = 3.11.15" or "version_info = 3.11.15.final.0"
            return line.split("=", 1)[1].strip()
    return None


def _scan_missing_tools(output: str) -> list[str]:
    """Best-effort: surface tools install.sh reported as missing/not found."""
    missing: list[str] = []
    low = output.lower()
    for tool in ("kubectl", "helm", "helmfile", "jq", "yq", "kustomize", "skopeo", "crane", "git", "uv"):
        if f"{tool}: not found" in low or f"{tool} not found" in low or f"missing {tool}" in low:
            missing.append(tool)
    return sorted(set(missing))
