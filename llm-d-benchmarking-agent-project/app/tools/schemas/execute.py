"""Pydantic input model for the execute_llmdbenchmark tool."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ExecuteInput(BaseModel):
    subcommand: Literal["plan", "standup", "smoketest", "run", "teardown", "results", "experiment"]
    spec: str | None = Field(default=None, description="Spec name from the catalog, e.g. 'cicd/kind'")
    namespace: str | None = None
    harness: str | None = Field(default=None, description="run/experiment only")
    workload: str | None = Field(default=None, description="run/experiment only")
    models: str | None = Field(
        default=None,
        description="A single model id (HF id or short name, e.g. 'facebook/opt-125m', "
                    "'meta-llama/Llama-3.1-8B') to deploy/serve, OVERRIDING the spec's "
                    "scenario-default model (valid on standup/plan/run/experiment). Emitted as "
                    "`-m <id>` (upstream --models on standup/plan/experiment, --model on run; -m "
                    "works on all). WHICH model is YOUR judgment (knowledge/model_override.md; no "
                    "enumerable catalog). CRITICAL: FIRST pass the SAME id to "
                    "check_capacity(overrides={'model': <id>}) so the pre-flight validates the "
                    "IDENTICAL model (HF config lookup, sizing, gated access). Omit to keep the "
                    "spec's default.",
    )
    kubeconfig: str | None = Field(
        default=None,
        description="Path to a NON-DEFAULT kubeconfig FILE to target a remote cluster for THIS "
                    "command instead of the ambient kube context. Emitted as `-k <path>` (upstream "
                    "--kubeconfig / LLMDBENCH_KUBECONFIG); valid on every subcommand; a non-secret "
                    "path, allowlist value-pinned (no `..`). WHEN/WHICH cluster is YOUR judgment "
                    "(knowledge/preconditions.md; no enumerable catalog). Omit for the ambient "
                    "context (the local Kind cluster for the quickstart). To target by API URL + "
                    "bearer TOKEN instead, see flags.cluster_url / flags.cluster_token — the TOKEN "
                    "is a SECRET, backend-only (never argv, never shown).",
    )
    store: dict[str, Any] | None = Field(
        default=None,
        description="ONLY for subcommand='results': drives the CLI's OPTIONAL git-like, "
                    "TEAM-SHARED Results Store (publishes/pulls runs via GCS remotes) — DISTINCT "
                    "from the agent's OWN local history (the result_history tool); reach for it "
                    "only for team sharing. read_knowledge('history') for WHICH and WHEN. Shape "
                    "{command, ...}: init/status/ls and remote 'ls' are read-only/auto-run; "
                    "add/rm/push/pull and remote add/rm are mutating/approval-gated. `command` one "
                    "of init/remote/status/add/rm/ls/push/pull — init: create local .result_store/; "
                    "status: list local runs; remote: manage remotes (remote_action "
                    "add{name,uri=gs://bucket/prefix} / rm{name} / ls); add/rm: stage/unstage "
                    "`paths` (dirs or run-uids); ls: list a remote (alias + optional model/hardware "
                    "filters; no wildcards); push: publish staged runs to a remote (default "
                    "staging); pull: download a run (default prod remote; REQUIRED run_uid). The "
                    "local history store is unchanged.",
    )
    flags: dict[str, Any] | None = Field(
        default=None,
        description="Optional dict of CLI knobs — each documented below with what it emits, "
                    "which subcommands accept it, whether it is read-only or approval-gated, "
                    "and the knowledge guide carrying the WHEN/WHICH judgment (load that guide "
                    "before relying on a flag). Keys: skip, skip_smoketest, dry_run, "
                    "list_endpoints, methods, repo_path, output, endpoint_url, monitoring, "
                    "harness_cpu_nr, harness_mem, cluster_url, cluster_token, step, dataset, "
                    "analyze, stack, "
                    "parallel, gateway_class, wait_timeout, data_access_timeout, "
                    "standalone_deploy_timeout, gateway_deploy_timeout, "
                    "modelservice_deploy_timeout, kustomize_deploy_timeout, pvc_bind_timeout, "
                    "fma_teardown_timeout, generate_config, run_config, debug. Per key: `skip` "
                    "=> -z on a run (collect-only re-analysis of a prior run's results; "
                    "read-only/auto-runs; knowledge/collect_only.md). `skip_smoketest` skips "
                    "the smoketest; `dry_run` previews only and `list_endpoints` lists resolved "
                    "endpoints (both read-only). `methods` => -t deploy method "
                    "(standalone/modelservice/kustomize/fma). `repo_path` => --llmd-repo-path: "
                    "a LOCAL llm-d clone for the kustomize method (standup); else upstream "
                    "clones llm-d.git. The kustomize.* config block is authored via "
                    "write_and_validate_config(artifact_type='scenario'), not here; "
                    "knowledge/deploy_path_playbook.md. `output` => results destination keyword "
                    "'local' (default) or a 'gs://...'/'s3://...' bucket URI (cloud is opt-in); "
                    "knowledge/cloud_results_sink.md. `endpoint_url` => benchmark an existing "
                    "OpenAI-compatible endpoint directly. `monitoring` => True emits "
                    "--monitoring (PodMonitor/ServiceMonitor + EPP verbosity on standup; "
                    "scrapes vLLM /metrics on run/experiment); False emits --no-monitoring on "
                    "STANDUP ONLY (opt-out for clusters lacking the Prometheus-operator CRDs); "
                    "omit for scenario default (default ON — probe prometheus_crds first); "
                    "knowledge/observability.md. `harness_cpu_nr` => backend-only env "
                    "LLMDBENCH_HARNESS_CPU_NR (NOT a CLI flag; default 16) — lower on a "
                    "small/Kind node so the launcher pod schedules; "
                    "knowledge/harness_sizing.md. `harness_mem` => backend-only env "
                    "LLMDBENCH_HARNESS_CPU_MEM (NOT a CLI flag; default 32Gi) — the launcher "
                    "pod's MEMORY request as a Kubernetes quantity ('48Gi', '512Mi'); raise it "
                    "when the launcher OOMs, lower it on a tiny node; "
                    "knowledge/harness_sizing.md. `cluster_url`/`cluster_token` => target a "
                    "remote cluster by API URL + bearer token (alternative to the `kubeconfig` "
                    "file): carried BACKEND-ONLY as "
                    "LLMDBENCH_CLUSTER_URL/LLMDBENCH_CLUSTER_TOKEN env (never argv/event/log; "
                    "scrubbed like HF_TOKEN); the token is a SECRET — never echo it; "
                    "knowledge/preconditions.md. `step` => -s step-list (e.g. '5', '5-9', "
                    "'3-5,9') on standup/smoketest/run/teardown to re-run one failed "
                    "step/range; does NOT change a command's mutating mode; "
                    "knowledge/step_select.md. `dataset` => -x URL/path on run/experiment to "
                    "REPLAY a real dataset instead of the synthetic profile; "
                    "knowledge/dataset_replay.md. `analyze` => --analyze on a run for "
                    "SUPPLEMENTARY matplotlib plots (distributions/session/graphs); your "
                    "SLO/goodput/Pareto math is unchanged; knowledge/analysis.md. `stack` => "
                    "--stack NAME[,NAME...] restricts a multi-stack scenario to a subset "
                    "(standup/smoketest/run/teardown); `parallel` => --parallel <int> caps how "
                    "many stacks deploy in parallel (standup/smoketest/experiment; DISTINCT "
                    "from parallelism/-j harness pods); knowledge/multi_stack.md. "
                    "`gateway_class` => --gateway-class <provider> "
                    "(istio/agentgateway/gke/epponly/data-science-gateway-class) on any "
                    "subcommand, overriding the spec's gateway.className (modelservice deploy "
                    "path only); knowledge/gateway_class.md. Per-phase CLI timeouts are "
                    "positive-int SECONDS, each emitting the matching --*-timeout and MUST stay "
                    "below the runner's per-command deadline: "
                    "`wait_timeout`/`data_access_timeout` on run+experiment; "
                    "`standalone_deploy_timeout` / `gateway_deploy_timeout` / "
                    "`modelservice_deploy_timeout` / `kustomize_deploy_timeout` / "
                    "`pvc_bind_timeout` on standup; `fma_teardown_timeout` on teardown; "
                    "knowledge/phase_timeouts.md. `generate_config` => --generate-config on a "
                    "run (writes a reusable run-config YAML and exits; read-only/auto-runs); "
                    "`run_config` => -c <path> to REPLAY one (run-only; still approval-gated); "
                    "knowledge/runconfig_roundtrip.md. `debug` => -d on run/experiment ONLY "
                    "(harness pods sleep instead of running the load; still approval-gated; on "
                    "teardown -d means --deep so it is NOT emitted there) — explain how to exec "
                    "in but do not drive the shell; knowledge/harness_debug.md. For "
                    "subcommand='experiment': {experiments (path to the experiment YAML), "
                    "workspace, parallelism (int), overrides ('p=v,...'), stop_on_error, "
                    "skip_teardown}.",
    )
    extra: list[str] | None = None
