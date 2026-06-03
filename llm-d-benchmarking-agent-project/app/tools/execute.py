"""The single gated entry point for running the ``llmdbenchmark`` CLI.

Every standup / smoketest / run / teardown / plan goes through here. The handler builds
an argv list from structured arguments (never a shell string), validates it against the
allowlist for a clean early error, then runs it via the approval-gated runner.
"""
from __future__ import annotations

import re
from typing import Any

from app.observability.resource_poller import resource_stats_poller
from app.tools.context import ToolContext, ToolError

_SUBCOMMANDS = {"plan", "standup", "smoketest", "run", "teardown", "results", "experiment"}

# Subcommands that actually exercise the cluster long enough for live resource stats to be
# meaningful; we poll ``kubectl top`` alongside them (namespace-wide). plan/dry_run/list_endpoints
# are previews and never wrapped.
_POLLED_SUBCOMMANDS = {"run", "experiment", "smoketest"}

# Per-subcommand execution timeouts are now POLICY DATA: they live as `timeout_s` on each
# llmdbenchmark subcommand in security/allowlist.yaml and are sourced from there by the
# command runner (via the Decision). There is intentionally no Python timeout table here —
# one mechanism, not two (Phase 13).


def build_argv(
    subcommand: str,
    *,
    spec: str | None = None,
    namespace: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    models: str | None = None,
    flags: dict[str, Any] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Assemble the logical argv. Global flags (``--spec``, ``--workspace``) precede the
    subcommand; everything else follows it.

    ``models`` (Phase 28) emits ``-m <id>`` after the subcommand to OVERRIDE the spec's
    scenario-default model for THIS standup (or plan/run/experiment). Upstream spells this
    ``--models`` on standup/plan/experiment but ``--model`` on run; ``-m`` is the single short
    form valid on all of them, so we always emit ``-m``. Omitted ⇒ the spec's default model
    stands. WHICH model is the agent's judgment (knowledge/model_override.md), not Python's —
    this is pure mechanism. The SAME id must be passed to check_capacity(overrides={'model': …})
    so the pre-flight validates the identical model (HF config lookup + gated-access).

    ``flags["monitoring"]`` is SUBCOMMAND-AWARE (Phase 27): ``True`` emits ``--monitoring`` for
    standup/run/experiment/plan; ``False`` emits ``--no-monitoring`` only for ``standup`` (the
    sole subcommand whose upstream argparse — BooleanOptionalAction — accepts it; run/experiment/
    plan are store_true, so an opt-out there just omits the flag); ``None``/absent emits nothing
    (scenario defaults). Whether to set it is the agent's judgment (knowledge/observability.md),
    not Python's.

    ``flags["repo_path"]`` (Phase 46) emits ``--llmd-repo-path <path>`` — a real ``standup``
    argparse flag — pointing the KUSTOMIZE deploy method (``-t kustomize``) at a LOCAL llm-d
    clone instead of letting upstream clone ``https://github.com/llm-d/llm-d.git`` into
    ``workspace/llm-d``. It is the CLI fallback for the scenario block's ``kustomize.repoPath``
    (see llm-d-benchmark/docs/kustomize.md). Pure MECHANISM — we emit whatever path the agent
    supplied; WHICH guide/overlay/patches/repo to deploy is the agent's judgment in
    knowledge/deploy_path_playbook.md, never an if/elif on the value here. The kustomize.* config
    BLOCK itself (guideName/repoPath/repoRef/patches/overlayPath/extraHelmValues/
    guideVariableOverrides) is NOT built here — it is AUTHORED as a scenario via
    write_and_validate_config(artifact_type='scenario') using DOTTED ``kustomize.*`` keys, then
    GATED through plan/--dry-run. Omitted ⇒ nothing emitted (the block's repoPath / the default
    upstream clone stands)."""
    flags = flags or {}
    argv: list[str] = ["llmdbenchmark"]
    if spec:
        argv += ["--spec", spec]
    if flags.get("workspace"):
        argv += ["--workspace", str(flags["workspace"])]
    argv.append(subcommand)
    if namespace:
        argv += ["-p", namespace]
    if harness:
        argv += ["-l", harness]
    if workload:
        argv += ["-w", workload]
    # Model override (Phase 28): select a model per standup, OVERRIDING the spec's scenario
    # default. PURE MECHANISM — we emit whatever id the agent chose; WHICH model is judgment
    # (knowledge/model_override.md), never an if/elif on the value. Always the short ``-m``
    # (the one form valid across standup/plan/run/experiment, where upstream uses --models on
    # standup/plan/experiment but --model on run). Omitted ⇒ the spec's default model stands.
    if models:
        argv += ["-m", str(models)]
    if flags.get("methods"):
        argv += ["-t", str(flags["methods"])]
    # Kustomize local-clone path (Phase 46): point the `-t kustomize` deploy method at a LOCAL
    # llm-d clone via the real standup `--llmd-repo-path` flag (the CLI fallback for the
    # scenario block's kustomize.repoPath). PURE MECHANISM — WHICH repo/guide/overlay is the
    # agent's judgment (knowledge/deploy_path_playbook.md), never an if/elif on the value.
    # Omitted ⇒ nothing emitted (upstream clones llm-d into workspace/ unless repoPath is set).
    if flags.get("repo_path"):
        argv += ["--llmd-repo-path", str(flags["repo_path"])]
    if flags.get("output"):
        argv += ["-r", str(flags["output"])]
    if flags.get("endpoint_url"):
        argv += ["-U", str(flags["endpoint_url"])]
    # experiment (DoE sweep) extras — emitted only when present, so other subcommands are unaffected.
    if flags.get("experiments"):
        argv += ["-e", str(flags["experiments"])]
    if flags.get("overrides"):
        argv += ["-o", str(flags["overrides"])]
    if flags.get("parallelism") is not None:
        argv += ["-j", str(flags["parallelism"])]
    if flags.get("stop_on_error"):
        argv.append("--stop-on-error")
    if flags.get("skip_teardown"):
        argv.append("--skip-teardown")
    if flags.get("skip_smoketest"):
        argv.append("--skip-smoketest")
    # Monitoring (Phase 27): activate the metrics PRODUCER so results.observability gets
    # populated (KV-cache hit rate / queue depth / GPU util / EPP-log snapshots). This is pure
    # MECHANISM — the on/off + CRD opt-out JUDGMENT is the agent's, set into flags["monitoring"]
    # from knowledge/observability.md (default ON; opt out on CRD-less clusters). It is also
    # SUBCOMMAND-AWARE, mirroring the upstream argparse: standup uses BooleanOptionalAction so it
    # accepts BOTH --monitoring and --no-monitoring; run/experiment/plan use store_true, so only
    # --monitoring exists there — an explicit opt-out simply omits the flag (no scraping). We only
    # ever emit a flag the agent explicitly set; an unset (None) monitoring touches nothing.
    monitoring = flags.get("monitoring")
    if monitoring is True:
        argv.append("--monitoring")
    elif monitoring is False and subcommand == "standup":
        argv.append("--no-monitoring")
    if flags.get("list_endpoints"):
        argv.append("--list-endpoints")
    if flags.get("dry_run"):
        argv.append("--dry-run")
    argv += list(extra or [])
    return argv


async def execute_llmdbenchmark(
    ctx: ToolContext,
    *,
    subcommand: str,
    spec: str | None = None,
    namespace: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    models: str | None = None,
    flags: dict[str, Any] | None = None,
    extra: list[str] | None = None,
) -> dict[str, Any]:
    if subcommand not in _SUBCOMMANDS:
        raise ToolError(f"unsupported subcommand {subcommand!r}; allowed: {sorted(_SUBCOMMANDS)}")

    flags = dict(flags or {})
    # `llmdbenchmark`'s -r/--output is a DESTINATION KEYWORD — `local`, `gs://…`, or
    # `s3://…` — NOT a filesystem path. Passing an absolute path makes the run fail with
    # "Unknown output destination: <path>". So default a `run` to local output and anchor
    # its --workspace to the session dir: the CLI then writes the report UNDER this session
    # (locate_and_parse_report searches the workspace recursively) and it persists with the
    # session. Mirrors the experiment anchoring below.
    if subcommand == "run" and not flags.get("list_endpoints") and not flags.get("dry_run"):
        flags.setdefault("output", "local")
        flags.setdefault("workspace", str(ctx.workspace))
    # A DoE `experiment` writes per-treatment reports under its workspace; anchor it to the
    # session dir (unless previewing) so compare_reports(experiment_dir=...) can find them.
    if subcommand == "experiment" and not flags.get("workspace") and not flags.get("dry_run"):
        flags["workspace"] = str(ctx.workspace / "experiment")

    argv = build_argv(
        subcommand, spec=spec, namespace=namespace, harness=harness,
        workload=workload, models=models, flags=flags, extra=extra,
    )

    # Right-size the harness launcher's CPU request for small/Kind nodes. This is an ENV VAR
    # (LLMDBENCH_HARNESS_CPU_NR), NOT a CLI flag and NOT an executable, so it bypasses the
    # allowlist entirely and is carried backend-only through the child env — it never reaches
    # the browser (no `command` event emits env). PURE MECHANISM: we forward whatever value the
    # agent chose; WHETHER to lower it from the default (16) and to WHAT — given the probed node
    # CPU and the harness (inference-perf's multi-process launcher needs more headroom than
    # vllm-benchmark's single-process one) — is judgment sourced from knowledge/harness_sizing.md,
    # never an if/elif here. Omitted when the agent didn't supply it (default 16 stands).
    child_env: dict[str, str] | None = None
    if flags.get("harness_cpu_nr") is not None:
        child_env = {"LLMDBENCH_HARNESS_CPU_NR": str(flags["harness_cpu_nr"])}

    # Validate up front for a clean, specific error message before any approval prompt.
    decision = ctx.allowlist.validate(argv, catalog=ctx.catalog_for_allowlist())
    if not decision.allowed:
        raise ToolError(f"command refused by allowlist: {decision.reason}\n  argv: {' '.join(argv)}")

    # No timeout override: ctx.run_command sources the per-command deadline from the
    # allowlist's `timeout_s` for this subcommand (data), falling back to the runner's
    # global default when the policy declares none. For the cluster-exercising subcommands,
    # stream live resource stats alongside the run (backend-only, zero LLM cost; no-op without
    # a UI emitter or in simulate mode).
    if subcommand in _POLLED_SUBCOMMANDS and namespace:
        async with resource_stats_poller(ctx, namespace=namespace):
            res = await ctx.run_command(argv, env=child_env)
    else:
        res = await ctx.run_command(argv, env=child_env)
    results_dir = _result_location(
        subcommand, flags, _parse_results_dir(res.output), str(ctx.workspace / "results")
    )
    return {
        "argv": argv,
        "mode": decision.mode,
        "exit_code": res.exit_code,
        "duration_s": res.duration_s,
        "timed_out": res.timed_out,
        "results_dir": results_dir,
        "stdout_tail": res.output[-2500:],
    }


_RESULTS_RE = re.compile(r"(/[\w./-]*results[\w./-]*)")


def _parse_results_dir(output: str) -> str | None:
    """Best-effort: pull a results directory path out of CLI output."""
    matches = _RESULTS_RE.findall(output or "")
    return matches[-1] if matches else None


def _result_location(
    subcommand: str, flags: dict[str, Any], parsed: str | None, run_output_dir: str
) -> str | None:
    """Where the agent can find the report(s) afterwards (this is fed straight into
    ``compare_reports``).

    A ``run`` writes a single report under its ``-r/--output`` dir. An ``experiment``
    writes one report *per treatment*: its ``-r/--output`` is the per-treatment
    destination, so the dir that contains them ALL is the ``--workspace`` we anchored in
    ``execute_llmdbenchmark``. Returning that workspace lets
    ``compare_reports(experiment_dir=...)`` recursively discover every treatment's report;
    a stdout-scraped path (if any) would point at a single treatment's subdir, so it is
    only a fallback here.
    """
    if subcommand == "experiment":
        return flags.get("workspace") or parsed
    return parsed or (run_output_dir if flags.get("output") else None)
