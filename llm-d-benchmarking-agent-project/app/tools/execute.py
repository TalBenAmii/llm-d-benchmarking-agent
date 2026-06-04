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


def build_argv(
    subcommand: str,
    *,
    spec: str | None = None,
    namespace: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    models: str | None = None,
    kubeconfig: str | None = None,
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

    ``kubeconfig`` (Phase 29) emits ``-k <path>`` after the subcommand to target a NON-DEFAULT
    kubeconfig FILE (upstream ``--kubeconfig``, sourced from ``LLMDBENCH_KUBECONFIG``) — i.e. a
    remote cluster instead of the ambient context. It is valid on every subcommand. PURE
    MECHANISM: we emit whatever path the agent chose; WHEN/WHICH cluster to target is judgment
    (knowledge/preconditions.md), never an if/elif on the value. It is a plain (non-secret) file
    path; the cluster URL/token route stays BACKEND-ONLY (see ``execute_llmdbenchmark``) and is
    NEVER an argv token. Omitted ⇒ the ambient kube context stands.

    ``flags["monitoring"]`` is SUBCOMMAND-AWARE (Phase 27): ``True`` emits ``--monitoring`` for
    standup/run/experiment/plan; ``False`` emits ``--no-monitoring`` only for ``standup`` (the
    sole subcommand whose upstream argparse — BooleanOptionalAction — accepts it; run/experiment/
    plan are store_true, so an opt-out there just omits the flag); ``None``/absent emits nothing
    (scenario defaults). Whether to set it is the agent's judgment (knowledge/observability.md),
    not Python's.

    ``flags["step"]`` (Phase 31) emits ``-s <spec>`` to RE-RUN a single step or step range —
    e.g. ``'5'``, ``'5-9'``, ``'3,7'``, ``'3-5,9'`` (the upstream step-list grammar: numbers,
    ``N-M`` ranges, and comma-separated combos). Valid upstream on standup/smoketest/run/teardown
    only; ``None``/absent emits nothing (the whole phase runs). WHICH step to re-run after a
    mid-phase failure is the agent's judgment (knowledge/step_select.md), not Python's.

    ``flags["dataset"]`` (Phase 41) emits ``-x <url>`` to REPLAY a real dataset instead of the
    synthetic workload profile. It is SUBCOMMAND-AWARE: upstream ``-x``/``--dataset`` exists ONLY
    on ``run`` and ``experiment`` (standup/plan/smoketest/teardown reject it), so we emit it for
    those two ONLY; omitted/None emits nothing (the synthetic profile still drives the load). This
    is pure MECHANISM — WHETHER to replay a dataset, and WHICH one, is the agent's judgment in
    knowledge/dataset_replay.md, never an if/elif on the value here. We set NO env var: the CLI
    itself derives LLMDBENCH_RUN_DATASET_DIR/_FILE from the URL during profile rendering.

    The per-phase CLI timeout keys (Phase 38) — ``flags["wait_timeout"]``,
    ``flags["data_access_timeout"]``, ``flags["standalone_deploy_timeout"]``,
    ``flags["gateway_deploy_timeout"]``, ``flags["modelservice_deploy_timeout"]``,
    ``flags["kustomize_deploy_timeout"]``, ``flags["pvc_bind_timeout"]``,
    ``flags["fma_teardown_timeout"]`` — each emit the matching ``--*-timeout <seconds>`` CLI
    flag, but ONLY on the subcommand(s) upstream accepts it on (the ``_PHASE_TIMEOUT_FLAGS``
    table: standup owns the deploy/bind timeouts; run+experiment own wait/data-access; teardown
    owns fma). These are the CLI's OWN per-phase bound — a DEEPER timeout the CLI enforces
    internally — and must stay BELOW the runner's per-command ``timeout_s`` ceiling so the two
    layers do not fight. Emission is pure MECHANISM (no if/elif on the value); WHEN/WHAT to set
    is the agent's judgment in knowledge/phase_timeouts.md. Omitted ⇒ nothing emitted (the CLI's
    own defaults / env stand) and the runner deadline still bounds the whole process.

    ``flags["analyze"]`` (Phase 40) emits a bare ``--analyze`` ONLY on ``run``. Upstream defines
    ``--analyze`` (store_true, env ``LLMDBENCH_RUN_EXPERIMENT_ANALYZE_LOCALLY=1``) SOLELY on the
    ``run`` subparser (llmdbenchmark/interface/run.py) — the shared parser and experiment/standup/
    plan do NOT carry it — so we guard on ``subcommand == "run"`` and emit nothing elsewhere. When
    set, the CLI runs its optional workstation matplotlib analysis on the collected results,
    producing three EXTRA plot families UNDER ``analysis/`` — per-request distributions
    (``analysis/distributions/``), session-lifecycle bar charts (``analysis/session/``), and
    Prometheus time-series (``analysis/graphs/``) — IN ADDITION to the harness's own PNGs. These
    are SUPPLEMENTARY visualizations; they do NOT change the run's mutating mode and do NOT touch
    the agent's own SLO/goodput/Pareto math. This is pure MECHANISM — WHEN to ask for it is the
    agent's judgment (knowledge/analysis.md), never an if/elif on the value. Omitted/None/False
    emits nothing.

    ``flags["generate_config"]`` / ``flags["run_config"]`` (Phase 42) drive the CLI's OWN
    run-config round-trip, in addition to the agent's in-workspace write_and_validate_config.
    Both are upstream ``run``-ONLY, so we emit them only for ``subcommand == "run"``:
    ``generate_config`` => ``--generate-config`` (GENERATE a reusable run-config YAML from the
    current settings under ``--workspace`` — anchored to ctx.workspace by execute_llmdbenchmark —
    and EXIT; it deploys nothing, so the allowlist auto-runs it like ``--dry-run``);
    ``run_config`` => ``-c <path>`` (REPLAY a previously generated run-config — run-only mode —
    where ``<path>`` is the workspace-relative file ``--generate-config`` wrote). PURE MECHANISM:
    WHEN to generate vs reuse vs author in-workspace is judgment in
    knowledge/runconfig_roundtrip.md, never an if/elif on the value. No env var is set (the CLI
    consumes ``--generate-config``/``run_config`` directly). Omitted ⇒ nothing emitted.

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
    upstream clone stands).

    ``flags["stack"]`` (Phase 33) emits ``--stack <names>`` to restrict a MULTI-STACK scenario
    (N model pools behind one gateway, e.g. ``guides/multi-model-wva`` whose stacks are
    ``qwen3-06b``/``llama-31-8b``) to a SUBSET — a single stack name, or a comma-separated list
    (``NAME[,NAME...]``). It is SUBCOMMAND-AWARE: upstream ``--stack`` exists ONLY on
    standup/smoketest/run/teardown (plan/experiment reject it), so we emit it for those four
    ONLY; omitted/None emits nothing (every stack of the scenario is operated on). Pure
    MECHANISM — WHICH stack(s) to target is the agent's judgment in knowledge/multi_stack.md,
    never an if/elif on the value here.

    ``flags["parallel"]`` (Phase 33) emits ``--parallel <int>`` to CAP how many stacks are
    deployed/smoketested in parallel (the upstream per-pool max-parallel-stacks knob, an int
    defaulting to 4). It is SUBCOMMAND-AWARE: upstream ``--parallel`` exists ONLY on
    standup/smoketest/experiment (run uses the SEPARATE ``--parallelism``/``-j`` harness-pod
    count, teardown/plan have neither), so we emit it for those three ONLY; ``None``/absent emits
    nothing (the default 4 stands). We guard with ``is not None`` (like the existing
    ``parallelism``→``-j`` line) so an explicit ``0`` is honored. This is DISTINCT from
    ``flags["parallelism"]``→``-j`` above (number of parallel harness PODS, not stacks) — do NOT
    conflate them. Pure MECHANISM — HOW MANY stacks to deploy at once (i.e. whether to cap below
    4 on a small/Kind node) is the agent's judgment in knowledge/multi_stack.md, never an
    if/elif on the value here.

    ``flags["gateway_class"]`` (Phase 32) emits ``--gateway-class <provider>`` to choose the
    gateway PROVIDER, OVERRIDING the scenario's ``gateway.className`` for this command. It is
    emitted UNCONDITIONALLY across subcommands — upstream registers ``--gateway-class`` on ALL
    SIX (plan/standup/smoketest/run/teardown/experiment, verified in
    llmdbenchmark/interface/*.py), each defaulting to ``LLMDBENCH_GATEWAY_CLASS`` — so there is
    no subcommand guard and, deliberately, no judgment branch here. This is PURE MECHANISM: we
    emit whatever provider the agent chose; WHICH provider (one of istio / agentgateway / gke /
    epponly / data-science-gateway-class) lives entirely in knowledge/gateway_class.md, never an
    if/elif on the value. Upstream applies it ONLY on the modelservice deploy path (it is ignored
    by kustomize/standalone/fma per the standup help). Omitted/None ⇒ nothing emitted and the
    spec's scenario ``gateway.className`` stands."""
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
    # Cluster access (Phase 29): target a NON-DEFAULT kubeconfig FILE for this command, OVERRIDING
    # the ambient kube context. PURE MECHANISM — we emit whatever path the agent chose; WHEN/WHICH
    # cluster to target is judgment (knowledge/preconditions.md), never an if/elif on the value.
    # `-k` is the short form of --kubeconfig and is valid on every subcommand (standup/run/
    # smoketest/teardown/plan/experiment/results). A plain (non-secret) file path; the cluster
    # URL/token route stays BACKEND-ONLY (see execute_llmdbenchmark) and is NEVER an argv token.
    # Omitted ⇒ the ambient context stands.
    if kubeconfig:
        argv += ["-k", str(kubeconfig)]
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
    # Multi-stack SUBSET (Phase 33): emit --stack <names> to restrict a multi-stack scenario
    # (N model pools behind one gateway, e.g. guides/multi-model-wva) to one stack or a
    # comma-separated subset. SUBCOMMAND-AWARE: upstream --stack is accepted ONLY on
    # standup/smoketest/run/teardown (plan/experiment reject it), so we guard on the subcommand;
    # an absent/None stack emits nothing (every stack is operated on). PURE MECHANISM — WHICH
    # stack(s) to target is the agent's judgment (knowledge/multi_stack.md), never an if/elif on
    # the value.
    if flags.get("stack") and subcommand in ("standup", "smoketest", "run", "teardown"):
        argv += ["--stack", str(flags["stack"])]
    # Per-pool parallelism CAP (Phase 33): emit --parallel <int> to cap how many stacks deploy/
    # smoketest in parallel (upstream default 4). SUBCOMMAND-AWARE: upstream --parallel is on
    # standup/smoketest/experiment ONLY (run uses the SEPARATE --parallelism/-j harness-pod count;
    # teardown/plan have neither), so we guard on the subcommand. `is not None` (mirroring the
    # parallelism guard above) honors an explicit 0; absent/None emits nothing (default 4 stands).
    # PURE MECHANISM — HOW MANY stacks at once is the agent's judgment (knowledge/multi_stack.md),
    # never an if/elif on the value. DISTINCT from flags["parallelism"]->-j (parallel harness PODS).
    if flags.get("parallel") is not None and subcommand in ("standup", "smoketest", "experiment"):
        argv += ["--parallel", str(flags["parallel"])]
    # Gateway PROVIDER selection (Phase 32): emit --gateway-class <provider> to OVERRIDE the
    # scenario's gateway.className. Emitted UNCONDITIONALLY across subcommands — upstream
    # registers --gateway-class on ALL SIX (plan/standup/smoketest/run/teardown/experiment), so
    # there is no subcommand guard and no judgment branch in Python. PURE MECHANISM — WHICH
    # provider (istio/agentgateway/gke/epponly/data-science-gateway-class) is the agent's judgment
    # in knowledge/gateway_class.md, never an if/elif on the value. Upstream applies it only on
    # the modelservice deploy path (ignored by kustomize/standalone/fma). Omitted ⇒ the spec's
    # gateway.className stands. The provider value is allowlist-pinned to the gateway_class enum.
    if flags.get("gateway_class"):
        argv += ["--gateway-class", str(flags["gateway_class"])]
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
    # Step selection / re-run (Phase 31): emit -s <spec> so the agent can re-run a single
    # failed step or step range instead of redoing the whole phase. PURE MECHANISM — we emit
    # whatever step-spec the agent chose; WHICH step to re-run (and per-phase step numbering)
    # is judgment in knowledge/step_select.md, never an if/elif on the value. -s is the short
    # form valid upstream on standup/smoketest/run/teardown; the step-list grammar (N / N-M
    # ranges / N,M lists / combos like 3-5,9) is parsed by the CLI's StepExecutor. An unknown
    # -s on a subcommand that doesn't accept it is screened by the allowlist (only the four
    # accepting subcommands permit it). Re-running mutating steps stays approval-gated — -s
    # does not change a command's mode.
    if flags.get("step"):
        argv += ["-s", str(flags["step"])]
    # Dataset replay (Phase 41): emit -x <url> so the harness REPLAYS a real dataset instead of
    # the synthetic workload profile. Upstream -x/--dataset is accepted ONLY on run/experiment, so
    # we guard on the subcommand; an absent/None dataset emits nothing (synthetic profile stands).
    # PURE MECHANISM — WHICH dataset / dataset-vs-synthetic is the agent's judgment
    # (knowledge/dataset_replay.md), never an if/elif on the value.
    if flags.get("dataset") and subcommand in ("run", "experiment"):
        argv += ["-x", str(flags["dataset"])]
    # Per-phase CLI timeouts (Phase 38): emit the CLI's OWN per-phase timeout flags (seconds)
    # so a slow deploy / data-access / teardown phase can be given a longer DEEPER bound than
    # the host's blunt per-command runner deadline. This is pure MECHANISM driven by the
    # _PHASE_TIMEOUT_FLAGS table below (flags-key -> CLI flag + the upstream-accepting
    # subcommands) — there is NO if/elif on the value. The table mirrors the upstream argparse:
    # standup owns the five deploy/bind timeouts; run+experiment own --wait-timeout /
    # --data-access-timeout; teardown owns --fma-teardown-timeout. A timeout key set on a
    # subcommand that does not accept it emits NOTHING (guarded by the subcommand tuple, like
    # dataset above), so an out-of-place key is silently dropped instead of producing a flag the
    # CLI would reject. WHEN/WHAT to set — and the CRITICAL reconcile rule that each value must
    # stay BELOW the runner's `timeout_s` ceiling for that subcommand (3600 standup/run, 900
    # teardown, 14400 experiment) so the two timeout layers do not fight — is the agent's
    # judgment in knowledge/phase_timeouts.md, never encoded here.
    for key, (cli_flag, accepts) in _PHASE_TIMEOUT_FLAGS.items():
        value = flags.get(key)
        if value is not None and subcommand in accepts:
            argv += [cli_flag, str(value)]
    # Run-config round-trip (Phase 42): use the CLI's OWN --generate-config / -c reuse
    # mechanism. Both are upstream `run`-ONLY (interface/run.py: --generate-config is store_true,
    # "Generate a run config YAML from current settings and exit"; -c/--config dest=run_config,
    # "Path to run config YAML file (enables run-only mode)"), so we guard on subcommand == "run".
    #   * flags["generate_config"] => append --generate-config: the CLI writes a reusable run-config
    #     YAML from the current settings UNDER --workspace (which execute_llmdbenchmark anchors to
    #     ctx.workspace for a non-preview run) and EXITS — it deploys nothing, so the allowlist
    #     marks it a read_only_trigger and it auto-runs (like --list-endpoints/--dry-run).
    #   * flags["run_config"] => append -c <path>: REPLAY a previously generated run-config
    #     (run-only mode). The path is the file --generate-config emitted under the session
    #     workspace, so a generated config lands under and replays from the same session dir.
    # This is pure MECHANISM. WHEN to generate vs reuse via -c vs author in-workspace with
    # write_and_validate_config is the agent's judgment in knowledge/runconfig_roundtrip.md, never
    # an if/elif on the value. We emit the short -c so its value can be a workspace-relative path,
    # and set NO env var (the CLI consumes --generate-config/run_config directly).
    if flags.get("generate_config") and subcommand == "run":
        argv.append("--generate-config")
    if flags.get("run_config") and subcommand == "run":
        argv += ["-c", str(flags["run_config"])]
    if flags.get("list_endpoints"):
        argv.append("--list-endpoints")
    # Collect-only / skip-execution mode (Phase 36): emit ``-z`` to SKIP the harness/load
    # execution and only collect + analyze data from the EXISTING results of a prior run in
    # the same workspace (upstream help: "Skip execution and only collect data from existing
    # results"). This is pure MECHANISM — WHETHER to set it is the agent's judgment
    # (knowledge/collect_only.md): use it to re-collect/re-analyze a run that already loaded,
    # WITHOUT re-running the benchmark. Upstream defines ``-z``/``--skip`` on the ``run``
    # subcommand ALONE (run.py), so the agent only sets it for a ``run``; we emit the short
    # ``-z`` (the -m precedent). Emission is unconditional mechanism — no if/elif on the value.
    if flags.get("skip"):
        argv.append("-z")
    # Local analysis plot families (Phase 40): emit a bare ``--analyze`` so the CLI ALSO runs its
    # optional workstation matplotlib analysis on the collected results, writing three EXTRA plot
    # families under analysis/ (per-request distributions, session-lifecycle, Prometheus
    # time-series) IN ADDITION to the harness PNGs. Upstream defines ``--analyze`` (store_true) on
    # the ``run`` subparser ALONE, so we guard on the subcommand; experiment/standup/plan reject
    # it. It does NOT change a run's mutating mode (a real run still loads + needs approval), and
    # the agent's own SLO/goodput/Pareto math is untouched. PURE MECHANISM — WHEN to ask for it is
    # the agent's judgment (knowledge/analysis.md), never an if/elif on the value.
    if flags.get("analyze") and subcommand == "run":
        argv.append("--analyze")
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
    kubeconfig: str | None = None,
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
        workload=workload, models=models, kubeconfig=kubeconfig, flags=flags, extra=extra,
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

    # Remote-cluster access by API-server URL + bearer TOKEN (Phase 29). These ride the SAME
    # backend-only `env=child_env` overlay as LLMDBENCH_HARNESS_CPU_NR — they are ENV VARS, NOT
    # CLI flags, so they bypass the allowlist and NEVER enter argv. The token is a SECRET: it is
    # therefore deliberately NOT an allowlisted flag (it could never be expressed as an argv
    # token) and it never appears in a `command` event — `_emit_command` emits only argv/text/
    # mode, so the browser/log/persisted trail never sees it (mirrors the HF_TOKEN non-leak
    # rationale in scripts/provision_hf_secret.py + settings.extra_subprocess_env). The benchmark
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
