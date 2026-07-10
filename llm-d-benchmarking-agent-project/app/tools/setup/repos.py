"""Mutating tools for preparing the environment: clone the read-only repos if missing,
and run the benchmark repo's install.sh to build its venv.

Both go through the approval gate (via ``ctx.run_command``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import BENCH_REPO_NAME, GUIDE_REPO_NAME, SKILLS_REPO_NAME
from app.tools.context import ToolContext, ToolError

_KNOWN_REPOS = (BENCH_REPO_NAME, GUIDE_REPO_NAME, SKILLS_REPO_NAME)

# GitHub org each repo clones from. Most live under `llm-d`; the skills library lives under
# `llm-d-incubation`. This map only BUILDS the URL — the clone target is still pinned by the
# allowlist (llmd_clone_url regex), so this grants no capability on its own.
_REPO_ORG = {SKILLS_REPO_NAME: "llm-d-incubation"}


async def ensure_repos(ctx: ToolContext, *, repos: list[str] | None = None, ref: str | None = None) -> dict[str, Any]:
    """Clone any missing repos into the canonical sibling location. Idempotent;
    never deletes or overwrites an existing directory."""
    repos = repos or list(_KNOWN_REPOS)
    repos_dir = ctx.settings.resolved_repos_dir
    results: list[dict[str, Any]] = []

    # ALWAYS refresh the catalog before returning — even if a later repo's clone is rejected at
    # the approval gate (ApprovalRejected) or denied (ToolError), which propagate straight out of
    # ``ctx.run_command`` mid-loop. An EARLIER repo in the same call may
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

            url = f"https://github.com/{_REPO_ORG.get(name, 'llm-d')}/{name}"
            argv = ["git", "clone", url]
            # ``ref`` pins the benchmark/guide repos (e.g. a release tag); the skills library is
            # independently versioned and has no such tag, so never apply ``ref`` to it (else the
            # clone fails on a non-existent branch and the skill grounding silently vanishes).
            if ref and name != SKILLS_REPO_NAME:
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


# ── provision_hf_secret (merged from app/tools/hf_secret.py) ──────────────────
# provision_hf_secret — approval-gated provisioning of the cluster HF token Secret.
#
# A gated-model standup needs the cluster to hold a HuggingFace token Secret (the upstream
# ``llm-d-hf-token``) so the model server can pull the gated weights. This tool materializes
# that Secret BEFORE standup. It is the natural follow-on to the Phase 62 gated-access
# pre-flight: ``check_capacity`` tells you a model is GATED+UNAUTHORIZED because no token is
# configured cluster-side; this is the mutating step that fixes it.
#
# This handler is pure MECHANISM: it builds the argv for the vetted ``provision_hf_secret.py``
# script and calls ``ctx.run_command``, which routes the (mutating, per the allowlist) command
# through the approval gate. There is NO decision logic here — WHEN a gated model is in scope
# and WHEN to provision lives in ``knowledge/capacity.md``.
#
# The token NEVER crosses this layer. The handler's argv carries only ``--namespace`` and
# (optionally) ``--name``; the script reads ``HF_TOKEN`` from the scrubbed child env that the
# runner injects (``settings.extra_subprocess_env``). So the token appears in no tool input,
# no argv, no ``command`` event, and no log.

# Upstream HF_TOKEN_NAME default (llm-d/helpers/hf-token.md). Kept in lockstep with the
# script's own default; the script applies it when --name is omitted from the argv.
_DEFAULT_SECRET_NAME = "llm-d-hf-token"


async def provision_hf_secret(
    ctx: ToolContext,
    *,
    namespace: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Create/update the cluster HuggingFace token Secret in ``namespace``.

    Mutating → routed through the approval gate by ``ctx.run_command`` (the allowlist marks
    ``provision_hf_secret.py`` ``mode: mutating``). The token is read backend-side by the
    script from the scrubbed child env and is never part of the argv built here.
    """
    secret_name = name or _DEFAULT_SECRET_NAME
    argv = ["provision_hf_secret.py", "--namespace", namespace, "--name", secret_name]
    res = await ctx.run_command(argv)
    return {
        "namespace": namespace,
        "name": secret_name,
        "provisioned": res.exit_code == 0,
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
        # kubectl's own confirmation (e.g. "secret/llm-d-hf-token created"); never the token.
        "stdout_tail": res.output[-2000:],
        "note": (
            "HuggingFace token Secret provisioned. Re-run check_capacity to confirm the gated "
            "model is now authorized before standing up."
            if res.exit_code == 0
            else "Secret provisioning FAILED — read the stdout_tail. If HF_TOKEN is not "
            "configured in the backend, it must be set there (it stays backend-only)."
        ),
    }
