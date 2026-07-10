"""Reproducibility — the provenance-bundle + "Reproduce this run" tool family.

One agent-facing module, two tools (mirroring ``result_history``'s thin mechanism shape):

* ``export_run_bundle`` — capture a :class:`~app.storage.provenance.ProvenanceBundle` for a
  VALIDATED run: locate + schema-validate the report (gate d — refuses an invalid one), read the
  session's generated ``run-config.yaml`` (or note its absence and tell the agent to generate one
  first), capture BOTH read-only repo SHAs (+ dirty flags) via ``ctx.run_readonly`` (degrading to
  ``unavailable`` when a repo is empty — the worktree case), capture the agent/knowledge versions
  + a re-probed env snapshot, write the bundle under ``workspace/bundles/``, and optionally link
  it onto a stored history record. Auto-runs (read-only: git reads + a workspace write).

* ``reproduce_run`` — read a saved bundle and return a structured rerun PROPOSAL. It emits NO
  mutating command (it never calls ``ctx.run_command``). The agent turns the proposal into a
  ``propose_session_plan`` (gate 1: catalog-validated, approval-gated), then a
  ``execute_llmdbenchmark(run, flags.dry_run=True)`` (the CLI ``--dry-run`` gate), and only on a
  clean dry-run the approval-gated ``-c`` replay. Reproduce reuses the existing gates — it adds no
  new mutation path.

Judgment (WHEN to offer a bundle, how to explain a dirty repo to a non-expert, how to sequence a
reproduce, what to say on env drift) lives in ``knowledge/reproducibility.md`` — not here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.storage.history import compute_record_id
from app.storage.provenance import (
    BundleStore,
    InvalidReportError,
    build_bundle,
    capture_repo_state,
    knowledge_hash,
    provenance_view,
)
from app.tools.context import ToolContext
from app.validation.report import (
    find_reports,
    load_report,
    summarize_report,
    validate_report,
)

# Where the CLI's ``--generate-config`` writes the reusable run-config under a session workspace
# (knowledge/runconfig_roundtrip.md). We search the workspace for these so a later replay can
# reference the byte-identical config — never re-serializing our own.
_RUN_CONFIG_GLOBS = ("**/run-config*.yaml", "**/run-config*.yml")


def _find_run_config(workspace: Path) -> Path | None:
    """Newest CLI-generated run-config under the session workspace, if any."""
    candidates: list[Path] = []
    if workspace.is_dir():
        for pat in _RUN_CONFIG_GLOBS:
            candidates.extend(workspace.glob(pat))
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.stat().st_mtime)[-1]


def _resolve_report(source: str) -> Path | None:
    p = Path(source)
    if p.is_file():
        return p
    found = find_reports([p], newest_only=True)
    return found[0] if found else None


async def export_run_bundle(
    ctx: ToolContext,
    *,
    source: str,
    namespace: str | None = None,
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    model: str | None = None,
    slo: dict[str, Any] | None = None,
    label: str | None = None,
    attach_to_history: bool = False,
) -> dict[str, Any]:
    """Capture a provenance bundle for a validated run. Read-only (git reads + a workspace write)."""
    report_path = _resolve_report(source)
    if report_path is None:
        return {"exported": False, "reason": f"no Benchmark Report found under {source!r}"}

    report = load_report(report_path)
    validation = validate_report(report, ctx.settings.benchmark_report_schema_path)
    if not validation.valid:
        # Never certify an unvalidated report (determinism gate d) — mirrors history._store.
        return {
            "exported": False,
            "reason": "report failed schema validation — bundle not created",
            "report_path": str(report_path),
            "errors": validation.errors[:5],
        }

    summary = summarize_report(report)
    report_bytes = report_path.read_bytes()

    # The exact resolved config the CLI wrote (we reference + inline it; we do NOT re-serialize).
    run_config = _find_run_config(ctx.workspace)
    if run_config is not None:
        try:
            cfg_body = run_config.read_text()
        except OSError:
            cfg_body = None
        resolved_config: dict[str, Any] = {
            "found": True, "path": str(run_config), "body": cfg_body,
        }
    else:
        resolved_config = {
            "found": False,
            "note": "No CLI-generated run-config found in this session. To make the run "
                    "byte-reproducible, first run execute_llmdbenchmark(subcommand='run', "
                    "flags={'generate_config': True}) (it writes run-config.yaml under the "
                    "session workspace), then export the bundle again.",
        }

    # Both repo SHAs (+ dirty). Degrades to unavailable (never fabricates) when a repo is empty.
    repos: dict[str, Any] = {}
    for name, repo_path in ctx.settings.repo_paths.items():
        repos[name] = await capture_repo_state(repo_path, ctx.run_readonly)

    # Re-probe the environment read-only for a fresh, hermetic snapshot (what it ran against).
    env_snapshot = await _safe_env_snapshot(ctx, namespace)

    try:
        bundle = build_bundle(
            report_bytes=report_bytes,
            report_summary=summary,
            report_valid=validation.valid,
            report_path=str(report_path),
            repos=repos,
            resolved_config=resolved_config,
            agent_version=ctx.settings.agent_version,
            knowledge_version=knowledge_hash(ctx.settings.knowledge_dir),
            spec=spec,
            harness=harness,
            workload=workload,
            namespace=namespace,
            model=model,
            slo=slo,
            env_snapshot=env_snapshot,
            label=label,
        )
    except InvalidReportError as exc:
        return {"exported": False, "reason": str(exc), "report_path": str(report_path)}

    store = BundleStore(ctx.workspace)
    path = store.write(bundle)
    bundle_json = bundle.to_json()

    attached = False
    if attach_to_history:
        attached = _attach_to_history(ctx, summary, str(report_path), bundle_json)

    return {
        "exported": True,
        "bundle_id": bundle.bundle_id,
        "bundle_path": str(path),
        "regenerate_command": bundle.regenerate_command,
        "dirty": bundle.dirty,
        "repos": repos,
        "run_config_found": resolved_config.get("found", False),
        "model": bundle.model,
        "harness": bundle.harness,
        "report_digest": bundle.report_digest,
        "attached_to_history": attached,
        # The agent surfaces the dirty caveat plainly — see read_knowledge('reproducibility').
    }


async def _safe_env_snapshot(ctx: ToolContext, namespace: str | None) -> dict[str, Any] | None:
    """A best-effort, read-only environment snapshot (what the run targeted). Never raises:
    a probe failure (no cluster, allowlist denial mid-probe) degrades to None rather than
    aborting the capture — the results are still real, the env signal is just absent."""
    try:
        from app.tools.setup.probe import probe_environment

        return await probe_environment(
            ctx, checks=["container_runtime", "kube_context", "cluster_info", "stack"],
            namespace=namespace,
        )
    except Exception:
        return None


def _attach_to_history(
    ctx: ToolContext, summary: dict[str, Any], report_path: str, bundle_json: dict[str, Any]
) -> bool:
    """Link a bundle onto its already-stored history record, if one exists. The store keys a
    record by the same content hash result_history used, so we look it up by that id and, when
    present, rewrite it with bundle_id + a compact provenance dict. Best-effort: a missing record
    or any write error returns False rather than raising."""
    store = ctx.history_store()
    rid = compute_record_id(summary, report_path)
    rec = store.get(rid)
    if rec is None:
        return False
    rec.bundle_id = bundle_json.get("bundle_id")
    rec.provenance = provenance_view(bundle_json)
    try:
        import json

        (store.dir / f"{rid}.json").write_text(json.dumps(rec.to_json(), indent=2, default=str))
    except OSError:
        return False
    return True


async def reproduce_run(ctx: ToolContext, *, bundle_id: str) -> dict[str, Any]:
    """Read a saved bundle and return a structured rerun PROPOSAL. Mutates nothing — it emits no
    command. The agent drives propose_session_plan -> dry-run -> approved -c replay."""
    bundle = BundleStore(ctx.workspace).read(bundle_id)
    if bundle is None:
        return {"reproducible": False, "reason": f"no provenance bundle {bundle_id!r} in this session"}

    resolved = bundle.get("resolved_config") or {}
    run_config_path = resolved.get("path") if resolved.get("found") else None
    repos = bundle.get("repos") or {}
    dirty = bool(bundle.get("dirty"))
    unavailable_repos = [name for name, st in repos.items() if (st or {}).get("unavailable")]

    # The proposal the agent turns into propose_session_plan -> dry-run -> approved -c replay.
    # We DESCRIBE the gated sequence; we never run any step here.
    return {
        "reproducible": True,
        "bundle_id": bundle.get("bundle_id"),
        "proposal": {
            "spec": bundle.get("spec"),
            "harness": bundle.get("harness"),
            "workload": bundle.get("workload"),
            "namespace": bundle.get("namespace"),
            "model": bundle.get("model"),
            "slo": bundle.get("slo"),
            "run_config_path": run_config_path,
        },
        "regenerate_command": bundle.get("regenerate_command"),
        "repos": repos,
        "dirty": dirty,
        "unavailable_repos": unavailable_repos,
        # The exact gated sequence the agent must follow — never a direct subprocess from here.
        "next_steps": [
            "propose_session_plan with the captured spec/harness/workload/namespace/slo "
            "(catalog-validated, approval-gated — determinism gate 1).",
            "After approval: execute_llmdbenchmark(subcommand='run', flags={'run_config': "
            f"{run_config_path!r}, 'dry_run': True}}) to PREVIEW the replay (the CLI --dry-run "
            "gate; read-only, no mutation)." if run_config_path else
            "No captured run-config: first re-create it with "
            "execute_llmdbenchmark(subcommand='run', flags={'generate_config': True}), then "
            "preview with flags.run_config + dry_run=True.",
            "Only on a clean dry-run: execute_llmdbenchmark(subcommand='run', flags={'run_config': "
            "<path>}) — the approval-gated -c replay (run-only; needs a live stack serving the "
            "captured model).",
        ],
        # Judgment on dirty/unavailable SHAs + env drift: read_knowledge('reproducibility').
        "caveat": _reproduce_caveat(dirty, unavailable_repos),
    }


def _reproduce_caveat(dirty: bool, unavailable_repos: list[str]) -> str | None:
    parts: list[str] = []
    if dirty:
        parts.append(
            "One or more repos were DIRTY (uncommitted changes) when this run was captured, so "
            "an exact re-run requires the same working tree — explain this plainly to the user."
        )
    if unavailable_repos:
        parts.append(
            f"Repo SHA was unavailable for: {', '.join(unavailable_repos)} (empty/absent at "
            "capture). The results are real but were NOT captured as exactly reproducible — say so."
        )
    return " ".join(parts) or None
