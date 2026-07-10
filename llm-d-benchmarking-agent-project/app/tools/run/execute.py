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

# A Kubernetes memory quantity: an integer/decimal amount with an optional binary (Ki/Mi/Gi/…) or
# decimal (k/M/G/…) suffix — e.g. ``48Gi``, ``512Mi``, ``0``. Used to validate ``harness_mem`` at
# the tool boundary before it becomes the launcher pod's memory request env (a malformed value
# would otherwise only be caught later by the Kubernetes API server at pod-apply time).
_K8S_MEM_QUANTITY_RE = re.compile(r"^\d+(\.\d+)?(Ei|Pi|Ti|Gi|Mi|Ki|E|P|T|G|M|k|m)?$")

# Subcommands that actually exercise the cluster long enough for live resource stats to be
# meaningful; we poll ``kubectl top`` alongside them (namespace-wide). plan/dry_run/list_endpoints
# are previews and never wrapped.
_POLLED_SUBCOMMANDS = {"run", "experiment", "smoketest"}

# Per-subcommand RUNNER execution timeouts are POLICY DATA: they live as `timeout_s` on each
# llmdbenchmark subcommand in security/allowlist.yaml and are sourced from there by the
# command runner (via the Decision). That is the OUTER, host-side deadline (asyncio.wait_for
# in runner.execute) — one mechanism, not two (Phase 13). It is unchanged here.
#
# Distinct from that: the llmdbenchmark CLI accepts its OWN per-phase timeout flags (Phase 38)
# — a DEEPER, in-process bound the CLI enforces on a specific deploy/wait/data-access/teardown
# phase. The table below is the static flags-key -> (CLI flag, upstream-accepting subcommands)
# mapping that build_argv iterates as PURE MECHANISM. It carries NO judgment: WHEN/WHAT to set,
# and the rule that every value must stay BELOW the runner `timeout_s` ceiling so the two layers
# do not fight, live in knowledge/phase_timeouts.md. Flag spellings + type=int + accepting
# subcommands are verified against llm-d-benchmark/llmdbenchmark/interface/{standup,run,
# experiment,teardown}.py and are value-pinned (positive_int) in security/allowlist.yaml.
# The git-like CLI Results Store (Phase 50). `llmdbenchmark results <store-command>` is an
# OPTIONAL, team-shared store (GCS remotes + push/pull) that is SEPARATE from the agent's own
# local history store (the result_history tool / app/storage/history.py). build_argv emits the
# upstream-EXACT nested-positional shape off the static `store` dict below — PURE MECHANISM, no
# if/elif on a VALUE: the only branch is on the discrete `command` enum token (mirroring the
# _PHASE_TIMEOUT_FLAGS table style). WHEN/WHETHER to use this store vs the local one is judgment
# in knowledge/history.md, never encoded here. The store-commands' shapes are verified against
# llm-d-benchmark/llmdbenchmark/interface/results.py and value-pinned in security/allowlist.yaml
# (init/status/ls/remote-ls read-only; add/rm/push/pull/remote-add/remote-rm mutating).
_RESULTS_STORE_COMMANDS = frozenset(
    {"init", "remote", "status", "add", "rm", "ls", "push", "pull"}
)


def _build_results_store_argv(store: dict[str, Any]) -> list[str]:
    """Translate a structured ``store`` request into the upstream-exact ``results``
    sub-positionals. PURE MECHANISM — the single branch is on the discrete ``command`` enum
    token (a fixed set), never on a value. Optional slots emit nothing when absent; a missing
    REQUIRED sub-field raises a clean ToolError the agent can self-correct from, rather than a
    raw KeyError. The allowlist + the CLI's own argparse still reject a malformed shape."""
    command = str(store.get("command", ""))
    if command not in _RESULTS_STORE_COMMANDS:
        raise ToolError(
            f"unsupported results-store command {command!r}; "
            f"allowed: {sorted(_RESULTS_STORE_COMMANDS)}"
        )

    def _req(field: str) -> str:
        """A required sub-positional — clean, self-correctable ToolError over a raw KeyError."""
        val = store.get(field)
        if val is None or str(val) == "":
            raise ToolError(f"results {command!r} requires a {field!r} value")
        return str(val)

    out: list[str] = [command]
    if command in ("add", "rm"):
        # `results add|rm <paths...>` — one or more local dir paths / run-uids to (un)stage.
        # Guard the shape: a non-iterable `paths` (e.g. a scalar) would raise TypeError BEFORE the
        # allowlist could reject it; a bare string would silently iterate per-character. Both must
        # be a clean, self-correctable ToolError instead.
        paths = store.get("paths") or []
        if not isinstance(paths, (list, tuple)):
            raise ToolError(f"results {command!r} `paths` must be a list of path/run-uid strings")
        out += [str(p) for p in paths]
    elif command == "remote":
        # `results remote {add NAME URI | rm NAME | ls}`.
        action = str(store.get("remote_action", ""))
        out.append(action)
        if action == "add":
            out += [_req("name"), _req("uri")]
        elif action == "rm":
            out.append(_req("name"))
    elif command == "ls":
        # `results ls <remote> [-m model] [-w hardware]` — list a remote, optional filters.
        out.append(_req("remote"))
        if store.get("model"):
            out += ["-m", str(store["model"])]
        if store.get("hardware"):
            out += ["-w", str(store["hardware"])]
    elif command == "push":
        # `results push [remote] [path] [-g group]` — publish staged (or an ad-hoc dir) to GCS.
        # `remote` (nargs='?', default 'staging') and `path` (nargs='?') are TWO ORDERED optional
        # positionals. A path-only push (no remote — the schema/knowledge document `remote` as
        # optional, defaulting to 'staging') must STILL emit the remote slot first, else upstream
        # argparse binds the path to the `remote` positional (the run dir becomes the remote name)
        # and the WRONG store op runs. So when a `path` is given without a `remote`, emit the
        # upstream default `staging` to hold the first slot; a bare push (neither) emits nothing.
        remote = store.get("remote")
        path = store.get("path")
        if remote:
            out.append(str(remote))
        elif path:
            out.append("staging")  # upstream push default — keeps `path` in the second positional
        if path:
            out.append(str(path))
        if store.get("group"):
            out += ["-g", str(store["group"])]
    elif command == "pull":
        # `results pull [remote...] --run-uid <uid>` — download a run from a remote.
        if store.get("remote"):
            out.append(str(store["remote"]))
        out += ["--run-uid", _req("run_uid")]
    return out


_PHASE_TIMEOUT_FLAGS: dict[str, tuple[str, tuple[str, ...]]] = {
    # standup phases (modelservice / standalone / kustomize deploy + PVC bind)
    "standalone_deploy_timeout": ("--standalone-deploy-timeout", ("standup",)),
    "gateway_deploy_timeout": ("--gateway-deploy-timeout", ("standup",)),
    "modelservice_deploy_timeout": ("--modelservice-deploy-timeout", ("standup",)),
    "kustomize_deploy_timeout": ("--kustomize-deploy-timeout", ("standup",)),
    "pvc_bind_timeout": ("--pvc-bind-timeout", ("standup",)),
    # harness wait / data-access (run + experiment)
    "wait_timeout": ("--wait-timeout", ("run", "experiment")),
    "data_access_timeout": ("--data-access-timeout", ("run", "experiment")),
    # teardown (FMA launcher/requester drain)
    "fma_teardown_timeout": ("--fma-teardown-timeout", ("teardown",)),
}


# Single-flag, subcommand-guarded emissions, collapsed into ONE data-driven loop (sibling of
# _PHASE_TIMEOUT_FLAGS). Each row is flags-key -> (cli_flag, accepting_subcommands, takes_value,
# allow_falsy) where:
#   * accepting_subcommands  — the subcommands upstream accepts the flag on; an EMPTY tuple means
#                              "every subcommand" (no guard — gateway_class/step ride all of them).
#   * takes_value            — True emits ``cli_flag <value>``; False emits a BARE ``cli_flag``.
#   * allow_falsy            — True uses an ``is not None`` guard so an explicit 0 still emits
#                              (parallel's per-pool cap); False uses the plain truthy guard.
# This table is DERIVED 1:1 from the former hand-written branches — same flags-key, cli_flag,
# accepting-subcommand tuple, valued/bare, and guard for each — and carries NO judgment (WHICH
# value to set lives in the per-flag knowledge/*.md). The per-flag rationale that used to sit on
# each branch is summarized below; the subcommand guards are EXACTLY as before (notably ``-d``
# stays run/experiment-only, never teardown where -d means the destructive --deep). ``monitoring``
# is NOT in this table — its True/False asymmetry (BooleanOptionalAction) needs a real branch.
_SUBCOMMAND_FLAGS: dict[str, tuple[str, tuple[str, ...], bool, bool]] = {
    # Multi-stack SUBSET (Phase 33): --stack <names> restricts a multi-stack scenario to a subset.
    # standup/smoketest/run/teardown only (plan/experiment reject it). knowledge/multi_stack.md.
    "stack": ("--stack", ("standup", "smoketest", "run", "teardown"), True, False),
    # Per-pool parallelism CAP (Phase 33): --parallel <int> caps how many stacks deploy/smoketest
    # at once (default 4). standup/smoketest/experiment only (run uses -j harness PODS). The
    # is-not-None guard (allow_falsy) honors an explicit 0. knowledge/multi_stack.md.
    "parallel": ("--parallel", ("standup", "smoketest", "experiment"), True, True),
    # Gateway PROVIDER selection (Phase 32): --gateway-class <provider> OVERRIDES the scenario's
    # gateway.className. Registered on ALL SIX subcommands upstream, so NO guard (empty tuple).
    # knowledge/gateway_class.md; value is allowlist-pinned to the gateway_class enum.
    "gateway_class": ("--gateway-class", (), True, False),
    # Step selection / re-run (Phase 31): -s <spec> re-runs one step / step range. -s is valid on
    # standup/smoketest/run/teardown upstream, but the allowlist screens an -s on a non-accepting
    # subcommand, so this kept NO Python subcommand guard before — preserved (empty tuple).
    # knowledge/step_select.md.
    "step": ("-s", (), True, False),
    # Dataset replay (Phase 41): -x <url> replays a real dataset instead of the synthetic profile.
    # run/experiment only. knowledge/dataset_replay.md.
    "dataset": ("-x", ("run", "experiment"), True, False),
    # Run-config round-trip (Phase 42): --generate-config GENERATES a reusable run-config YAML and
    # exits (run-only, read_only_trigger); -c <path> REPLAYS one (run-only). knowledge/
    # runconfig_roundtrip.md.
    "generate_config": ("--generate-config", ("run",), False, False),
    "run_config": ("-c", ("run",), True, False),
    # Harness debug mode (Phase 37): bare -d starts harness pods with `sleep infinity` instead of
    # the load (upstream --debug). GUARDED to run/experiment ONLY — on teardown -d means the
    # DESTRUCTIVE --deep, so an unguarded -d would turn a debug request into a deep teardown.
    # Stays mutating/approval-gated. knowledge/harness_debug.md.
    "debug": ("-d", ("run", "experiment"), False, False),
    # Local analysis plot families (Phase 40): bare --analyze ALSO runs the optional workstation
    # matplotlib analysis (extra plot families under analysis/). run-only. knowledge/analysis.md.
    "analyze": ("--analyze", ("run",), False, False),
}


def _argv_positionals(
    argv: list[str],
    *,
    namespace: str | None,
    harness: str | None,
    workload: str | None,
    models: str | None,
    kubeconfig: str | None,
    flags: dict[str, Any],
) -> None:
    """Core post-subcommand positionals/overrides shared by the non-``results`` subcommands.

    ``models`` (Phase 28) ⇒ ``-m <id>`` OVERRIDES the spec's scenario-default model. Upstream
    spells it ``--models`` on standup/plan/experiment but ``--model`` on run; ``-m`` is the one
    short form valid on all, so we always emit ``-m``. The SAME id must reach
    check_capacity(overrides={'model': …}). WHICH model is judgment (knowledge/model_override.md).

    ``kubeconfig`` (Phase 29) ⇒ ``-k <path>`` targets a NON-DEFAULT kubeconfig FILE (upstream
    ``--kubeconfig`` / ``LLMDBENCH_KUBECONFIG``), valid on every subcommand. A plain non-secret
    path; the cluster URL/token route stays BACKEND-ONLY (see ``execute_llmdbenchmark``) and is
    NEVER an argv token. WHEN/WHICH cluster is judgment (knowledge/preconditions.md).

    ``flags["repo_path"]`` (Phase 46) ⇒ ``--llmd-repo-path <path>`` points the KUSTOMIZE deploy
    method (``-t kustomize``) at a LOCAL llm-d clone instead of the upstream clone — the CLI
    fallback for the scenario block's ``kustomize.repoPath``. The kustomize.* BLOCK itself is
    authored as a scenario (DOTTED keys) + gated via plan/--dry-run, NOT here.
    WHICH guide/overlay/repo is judgment (knowledge/deploy_path_playbook.md).
    """
    if namespace:
        argv += ["-p", namespace]
    if harness:
        argv += ["-l", harness]
    if workload:
        argv += ["-w", workload]
    if models:
        argv += ["-m", str(models)]
    if kubeconfig:
        argv += ["-k", str(kubeconfig)]
    if flags.get("methods"):
        argv += ["-t", str(flags["methods"])]
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


def _argv_subcommand_flags(argv: list[str], subcommand: str, flags: dict[str, Any]) -> None:
    """The single-flag, subcommand-guarded emissions, driven by the ``_SUBCOMMAND_FLAGS`` table
    (stack / parallel / gateway_class / step / dataset / generate_config / run_config / debug /
    analyze). Each row carries its CLI flag, accepting-subcommand guard, valued/bare shape, and
    explicit-0 handling; per-flag rationale sits on the rows. PURE MECHANISM — no if/elif on a
    value (WHICH value to set lives in the per-flag knowledge/*.md). ``monitoring`` is NOT here —
    its True/False (BooleanOptionalAction) asymmetry needs a real branch (see _argv_toggles)."""
    for key, (cli_flag, accepts, takes_value, allow_falsy) in _SUBCOMMAND_FLAGS.items():
        value = flags.get(key)
        present = value is not None if allow_falsy else bool(value)
        if not present or (accepts and subcommand not in accepts):
            continue
        if takes_value:
            argv += [cli_flag, str(value)]
        else:
            argv.append(cli_flag)


def _argv_toggles(argv: list[str], subcommand: str, flags: dict[str, Any]) -> None:
    """Boolean/asymmetric toggles + the per-phase CLI timeouts + the run-flags tail.

    ``flags["monitoring"]`` (Phase 27) is SUBCOMMAND-AWARE: ``True`` ⇒ ``--monitoring`` (standup/
    run/experiment/plan, all of which accept it); ``False`` ⇒ ``--no-monitoring`` only for
    ``standup`` (the sole subcommand whose argparse — BooleanOptionalAction — accepts the opt-out;
    run/experiment/plan are store_true, so an opt-out there just omits the flag); None/absent emits
    nothing. WHETHER to set it is judgment (knowledge/observability.md).

    The per-phase CLI timeout keys (Phase 38) emit ``--*-timeout <seconds>`` per the
    ``_PHASE_TIMEOUT_FLAGS`` table — a DEEPER bound the CLI enforces internally, which must stay
    BELOW the runner's per-command ``timeout_s`` ceiling so the two layers don't fight. A key set
    on a non-accepting subcommand emits NOTHING. WHEN/WHAT to set is judgment
    (knowledge/phase_timeouts.md).

    ``flags["skip"]`` (Phase 36) ⇒ ``-z`` (run-only upstream): SKIP execution and only collect +
    analyze data from an EXISTING run in the same workspace (knowledge/collect_only.md).
    """
    if flags.get("stop_on_error"):
        argv.append("--stop-on-error")
    if flags.get("skip_teardown"):
        argv.append("--skip-teardown")
    if flags.get("skip_smoketest"):
        argv.append("--skip-smoketest")
    monitoring = flags.get("monitoring")
    if monitoring is True:
        argv.append("--monitoring")
    elif monitoring is False and subcommand == "standup":
        argv.append("--no-monitoring")
    for key, (cli_flag, accepts) in _PHASE_TIMEOUT_FLAGS.items():
        value = flags.get(key)
        if value is not None and subcommand in accepts:
            argv += [cli_flag, str(value)]
    if flags.get("list_endpoints"):
        argv.append("--list-endpoints")
    if flags.get("skip"):
        argv.append("-z")
    if flags.get("dry_run"):
        argv.append("--dry-run")


def build_argv(
    subcommand: str,
    *,
    spec: str | None = None,
    namespace: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    models: str | None = None,
    kubeconfig: str | None = None,
    store: dict[str, Any] | None = None,
    flags: dict[str, Any] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Assemble the logical argv. Global flags (``--spec``, ``--workspace``) precede the
    subcommand; everything else follows it. PURE MECHANISM throughout — no if/elif on a VALUE;
    WHICH value to set for any knob is the agent's judgment in the per-flag ``knowledge/*.md``.

    Emission order (each delegated to a focused helper; per-flag rationale lives on the helper /
    the ``_SUBCOMMAND_FLAGS`` + ``_PHASE_TIMEOUT_FLAGS`` table rows):
      1. ``llmdbenchmark`` + global ``--spec`` / ``--workspace``, then the subcommand token.
      2. ``results`` early-return (Phase 50): the OPTIONAL git-like Results Store takes its OWN
         nested store-command and NONE of the positionals below — emit it via
         ``_build_results_store_argv`` and return, so namespace/harness/model/run-flags never leak
         onto a store invocation. ``--spec`` still precedes it (upstream errors without it). This
         store is SEPARATE from the agent's local history store (result_history); WHEN to use it
         is judgment (knowledge/history.md).
      3. ``_argv_positionals`` — namespace/harness/workload + the -m/-k/-t/--llmd-repo-path/-r/-U
         overrides and the DoE -e/-o/-j extras.
      4. ``_argv_subcommand_flags`` — the table-driven single flags (stack/parallel/gateway_class/
         step/dataset/generate_config/run_config/debug/analyze).
      5. ``_argv_toggles`` — stop/skip booleans, the subcommand-aware --monitoring/--no-monitoring,
         the per-phase --*-timeout flags, and --list-endpoints/-z/--dry-run.
      6. ``extra`` passthrough, appended last.
    """
    flags = flags or {}
    argv: list[str] = ["llmdbenchmark"]
    if spec:
        argv += ["--spec", spec]
    if flags.get("workspace"):
        argv += ["--workspace", str(flags["workspace"])]
    argv.append(subcommand)
    if subcommand == "results" and store:
        argv += _build_results_store_argv(store)
        argv += list(extra or [])
        return argv
    _argv_positionals(
        argv, namespace=namespace, harness=harness, workload=workload,
        models=models, kubeconfig=kubeconfig, flags=flags,
    )
    _argv_subcommand_flags(argv, subcommand, flags)
    _argv_toggles(argv, subcommand, flags)
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
    kubeconfig: str | None = None,
    store: dict[str, Any] | None = None,
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
    # This anchoring also covers the Phase-42 round-trip: a `run --generate-config` and a
    # `run -c <path>` replay both fall through here (neither sets list_endpoints/dry_run), so the
    # generated run-config is WRITTEN under the session --workspace and a later -c REPLAYS a
    # workspace-relative config from the same session dir.
    if subcommand == "run" and not flags.get("list_endpoints") and not flags.get("dry_run"):
        flags.setdefault("output", "local")
        flags.setdefault("workspace", str(ctx.workspace))
    # A DoE `experiment` writes per-treatment reports under its workspace; anchor it to the
    # session dir (unless previewing) so compare_reports(experiment_dir=...) can find them.
    if subcommand == "experiment" and not flags.get("workspace") and not flags.get("dry_run"):
        flags["workspace"] = str(ctx.workspace / "experiment")

    argv = build_argv(
        subcommand, spec=spec, namespace=namespace, harness=harness,
        workload=workload, models=models, kubeconfig=kubeconfig, store=store,
        flags=flags, extra=extra,
    )

    # Right-size the harness launcher's CPU request for small/Kind nodes. This is an ENV VAR
    # (LLMDBENCH_HARNESS_CPU_NR), NOT a CLI flag and NOT an executable, so it bypasses the
    # allowlist entirely and is carried backend-only through the child env — it never reaches
    # the browser (no `command` event emits env). PURE MECHANISM: we forward whatever value the
    # agent chose; WHETHER to lower it from the default (16) and to WHAT — given the probed node
    # CPU and the harness (inference-perf's multi-process launcher needs more headroom than
    # vllm-benchmark's single-process one) — is judgment sourced from knowledge/harness_sizing.md,
    # never an if/elif here. Omitted when the agent didn't supply it (default 16 stands).
    child_env: dict[str, str] = {}
    if flags.get("harness_cpu_nr") is not None:
        child_env["LLMDBENCH_HARNESS_CPU_NR"] = str(flags["harness_cpu_nr"])
    # Sibling of harness_cpu_nr: the launcher pod's MEMORY request (LLMDBENCH_HARNESS_CPU_MEM,
    # default 32Gi) — also a backend-only ENV VAR, NOT a CLI flag, carried the same scrubbed way.
    # Raise it when the launcher OOMs, lower it on a tiny node. Validate the K8s quantity format at
    # this boundary (determinism gate) so a typo is a clean, self-correctable error rather than a
    # late pod-apply failure. WHETHER/what to set is judgment in knowledge/harness_sizing.md.
    if flags.get("harness_mem") is not None:
        mem = str(flags["harness_mem"])
        if not _K8S_MEM_QUANTITY_RE.match(mem):
            raise ToolError(
                f"harness_mem must be a Kubernetes memory quantity like '48Gi' or '512Mi' "
                f"(got {mem!r})"
            )
        child_env["LLMDBENCH_HARNESS_CPU_MEM"] = mem

    # Remote-cluster access by API-server URL + bearer TOKEN (Phase 29). These ride the SAME
    # backend-only `env=child_env` overlay as LLMDBENCH_HARNESS_CPU_NR — they are ENV VARS, NOT
    # CLI flags, so they bypass the allowlist and NEVER enter argv. The token is a SECRET: it is
    # therefore deliberately NOT an allowlisted flag (it could never be expressed as an argv
    # token) and it never appears in a `command` event — `_emit_command` emits only argv/text/
    # mode, so the browser/log/persisted trail never sees it (mirrors the HF_TOKEN non-leak
    # rationale in scripts/bridges/provision_hf_secret.py + settings.extra_subprocess_env). The benchmark
    # CLI consumes cluster_url/cluster_token via its ExecutionContext (utilities/cluster.kube_connect
    # honours host + bearer token); we forward them as LLMDBENCH_CLUSTER_URL/_TOKEN. PURE MECHANISM
    # — WHEN/WHETHER to target a remote cluster is judgment in knowledge/preconditions.md.
    if flags.get("cluster_url"):
        child_env["LLMDBENCH_CLUSTER_URL"] = str(flags["cluster_url"])
    if flags.get("cluster_token"):
        child_env["LLMDBENCH_CLUSTER_TOKEN"] = str(flags["cluster_token"])
    # A non-default kubeconfig FILE is ALSO honoured upstream via LLMDBENCH_KUBECONFIG; we already
    # emit it as the `-k` argv flag (build_argv), which is the canonical, non-secret path. No env
    # duplication is needed — the flag is the single source for the file-path case.
    child_env_or_none: dict[str, str] | None = child_env or None

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
            res = await ctx.run_command(argv, env=child_env_or_none)
    else:
        res = await ctx.run_command(argv, env=child_env_or_none)
    results_dir = _result_location(
        subcommand, flags, _parse_results_dir(res.output), str(ctx.workspace / "results")
    )
    out: dict[str, Any] = {
        "argv": argv,
        "mode": decision.mode,
        "exit_code": res.exit_code,
        "duration_s": res.duration_s,
        "timed_out": res.timed_out,
        "results_dir": results_dir,
    }
    # The raw CLI INFO-log tail is the canonical report's poor cousin. When a run exited cleanly and
    # pointed at a results dir, that report — not the log — is the source of truth (rule #4: parse
    # results from the Benchmark Report v0.2 schema, never scrape logs), so the tail is redundant and
    # re-replayed on every step until compaction stubs it. Drop it in exactly that case (and if the
    # report is empty/malformed the tail is still re-fetchable via run_shell). KEEP it when the run
    # failed/timed out (triage needs it) or produced no results_dir (plan/dry-run/smoketest previews,
    # where the stdout IS the signal the model reads).
    if res.exit_code != 0 or res.timed_out or not results_dir:
        out["stdout_tail"] = res.output[-2500:]
    return out


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
