"""Pydantic input models for the config-artifact authoring tools."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class WriteConfigInput(BaseModel):
    artifact_type: Literal["workload", "run_config", "scenario"] = Field(
        ...,
        description="What kind of config artifact to author. 'workload'/'run_config' write a "
                    "stock-shaped YAML as-is (MVP, rarely needed). 'scenario' AUTHORS finer "
                    "per-knob vLLM/scheduling/storage edits beyond the parallelism/memory knobs "
                    "that check_capacity + generate_doe_experiment already cover — see `content`.",
    )
    target_filename: str = Field(..., description="Bare *.yaml filename (no path separators)")
    content: dict[str, Any] = Field(
        ...,
        description="The config body. For 'workload'/'run_config' it is written verbatim. "
                    "For 'scenario' it is the set of per-knob OVERRIDES merged onto a minimal "
                    "`scenario: [ {name, ...} ]` skeleton: a REQUIRED 'name' (the scenario item "
                    "name) plus >=1 override keyed by the DOTTED upstream scenario field path. "
                    "Supported knob paths include vllmCommon.flags.* (e.g. enforceEager, "
                    "noPrefixCaching), vllmCommon.kvTransfer.* (enabled/connector/role), "
                    "vllmCommon.kvEvents.* (enabled/publisher/port/topicPrefix), "
                    "vllmCommon.priorityClassName, vllmCommon.ephemeralStorage, "
                    "vllmCommon.networkResource, affinity.* (enabled/nodeSelector/podAffinity/"
                    "podAntiAffinity), schedulerName, routing.servicePort, and per-section "
                    "decode.*/prefill.* (schedulerName, priorityClassName, ...). To author a "
                    "KUSTOMIZE-method deploy (Phase 46), set the kustomize.* family instead: "
                    "kustomize.enabled (true ⇒ deploy the upstream llm-d guide directly — this "
                    "OVERRIDES the rest of the scenario), kustomize.guideName (required; the "
                    "guides/<name> dir), kustomize.repoPath (a local llm-d clone; else upstream "
                    "clones it), kustomize.repoRef, kustomize.acceleratorBackend, "
                    "kustomize.monitoring, kustomize.overlayPath, kustomize.extraHelmValues, "
                    "kustomize.extraHelmSets, kustomize.guideVariableOverrides, and "
                    "kustomize.patches (a LIST of {patch: <inline YAML>} strategic-merge patches "
                    "against the guide's modelserver base). To CONFIGURE OpenTelemetry "
                    "distributed tracing on the deployed modelservice pods (Phase 54), set the "
                    "tracing.* family: tracing.enabled (true ⇒ turn it on), tracing.otlpEndpoint "
                    "(the OTLP gRPC endpoint of the USER'S OTel collector, e.g. "
                    "http://otel-collector:4317), tracing.sampling.sampler (e.g. "
                    "parentbased_traceidratio) + tracing.sampling.samplerArg ('1.0'=100%, "
                    "'0.1'=10%), tracing.serviceNames.{vllmDecode,vllmPrefill,routingProxy}, and "
                    "tracing.vllm.collectDetailedTraces. NOTE: the benchmark only CONFIGURES "
                    "tracing — it never deploys a collector/Jaeger and never collects/shows "
                    "traces in the report (the user views them in their own backend). The knobs "
                    "are SHAPE-validated against the repo's own scenario examples (read live). "
                    "WHICH knobs to set is JUDGMENT — call read_knowledge('vllm_overrides') for "
                    "vLLM tuning, read_knowledge('observability') for the tracing.* block + its "
                    "config-only limitation, or read_knowledge('deploy_path_playbook') for WHICH "
                    "guide/overlay/patches/repo the kustomize block should carry; the repos stay "
                    "read-only (authored into the session workspace). Preview the authored file "
                    "via execute_llmdbenchmark(subcommand='plan'/'run', flags={'dry_run': True}).",
    )


class ConvertGuideInput(BaseModel):
    name: str = Field(
        ...,
        description="The guide/scenario name token (letters/digits/_/-/. only). It becomes "
                    "ai.<name>.sh + ai.<name>.yaml in the SESSION WORKSPACE — the upstream "
                    "'ai.' prefix marks an agent-generated scenario. The read-only repos are "
                    "NEVER written; output goes to the session workspace only.",
    )
    env: dict[str, str] = Field(
        ...,
        description="REQUIRED. The already-resolved LLMDBENCH_* -> value map you derived from "
                    "the guide. Each key MUST start with 'LLMDBENCH_' (e.g. "
                    "{'LLMDBENCH_DEPLOY_MODEL_LIST': 'Qwen/Qwen3-32B', "
                    "'LLMDBENCH_VLLM_MODELSERVICE_DECODE_REPLICAS': '2'}); >=1 entry. The "
                    "mapping JUDGMENT — WHICH Helm/kustomize path maps to WHICH LLMDBENCH_* var, "
                    "the standard practices (DECODE_MODEL_COMMAND=custom, REPLACE_ENV_* "
                    "placeholders, the preprocess command), and which defaults to override — is "
                    "read_knowledge('convert_guide'), NOT this tool. The tool only EMITS the "
                    "sorted, shell-quoted exports into the workspace .sh.",
    )
    sources: dict[str, str] | None = Field(
        default=None,
        description="Optional per-LLMDBENCH_* var -> a short source-trace string (e.g. "
                    "'ms/values.yaml lines 23-24'), emitted as a '# SOURCE:' comment above "
                    "each export for upstream's traceability requirement. Keys not present in "
                    "`env` are ignored.",
    )
    scenario: dict[str, Any] | None = Field(
        default=None,
        description="Optional per-knob dotted-path overrides for the VALIDATABLE companion YAML "
                    "twin (same shape as write_and_validate_config content for "
                    "artifact_type='scenario': a 'name' is forced to <name>, plus >=1 DOTTED "
                    "upstream scenario field path, e.g. {'model.shortName': 'qwen3-32b', "
                    "'decode.parallelism.tensor': 2}). The twin is what the determinism gate "
                    "(plan/--dry-run) actually validates — a bare .sh is NOT gate-able. Omit it "
                    "to derive a minimal twin carrying just the scenario name.",
    )
    harness: str | None = Field(
        default=None,
        description="Optional harness recorded into the .sh as LLMDBENCH_HARNESS_NAME. "
                    "Defaults to 'inference-perf' (the upstream convert-guide default).",
    )
    profile: str | None = Field(
        default=None,
        description="Optional workload profile recorded into the .sh as "
                    "LLMDBENCH_HARNESS_EXPERIMENT_PROFILE. Defaults to 'sanity_random.yaml' "
                    "(the upstream convert-guide default).",
    )
    source_ref: str | None = Field(
        default=None,
        description="Optional guide URL/path, recorded only as a provenance header comment in "
                    "the .sh (e.g. 'https://github.com/llm-d/llm-d/tree/main/guides/"
                    "inference-scheduling'). Not fetched by this tool — you read the guide "
                    "yourself via read_repo_doc / run_command git clone / your own file reads.",
    )
