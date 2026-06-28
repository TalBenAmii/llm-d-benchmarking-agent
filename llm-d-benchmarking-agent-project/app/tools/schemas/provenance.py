"""Pydantic input models for the provenance / observability / history / workload-introspection
/ next-step tools."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProvisionHfSecretInput(BaseModel):
    namespace: str = Field(
        ...,
        description="The target Kubernetes namespace (an RFC1123 label, e.g. the plan's "
                    "namespace) to create/update the HuggingFace token Secret in. This is the "
                    "APPROVAL-GATED MUTATING step that materializes the cluster HF Secret a "
                    "GATED-model standup needs (so a `standup` doesn't fail minutes in with an "
                    "opaque image-pull/weights error). The token itself is BACKEND-ONLY (read "
                    "from the backend HF_TOKEN env, never shown, never an input here). WHEN to "
                    "call this is knowledge/capacity.md, NOT your guess: ONLY after a "
                    "check_capacity GATED+UNAUTHORIZED verdict whose reason says NO token is "
                    "configured cluster-side — never for a public model, and never when a token "
                    "merely LACKS access (that needs a HuggingFace access request, not a secret).",
    )
    name: str | None = Field(
        default=None,
        description="The Secret name (an RFC1123 object name). Omit to use the upstream "
                    "default 'llm-d-hf-token' (HF_TOKEN_NAME) that the llm-d standup expects; "
                    "only override it if the deployment was configured with a different name.",
    )


class ObserveRunMetricsInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace to read pod usage from "
                                            "(ignored for scope='nodes').")
    scope: Literal["pods", "nodes"] = Field(
        default="pods",
        description="'pods' = live CPU/memory of pods in the namespace; 'nodes' = node usage.",
    )
    run_id: str | None = Field(
        default=None,
        description="Narrow pod usage to ONE orchestrated run by its run-id label "
                    "(scope='pods' only). Omit to see all pods in the namespace.",
    )
    containers: bool = Field(
        default=False,
        description="Break pod usage down per-container (scope='pods' only).",
    )


class ResultHistoryInput(BaseModel):
    action: Literal["store", "list", "get", "trend", "delete"] = Field(
        ...,
        description="store = persist a validated Benchmark Report's summary for the long "
                    "term; list = show stored results (newest first); get = one record's full "
                    "summary; trend = the time-series of ONE metric across stored results; "
                    "delete = forget one record. All actions auto-run (none touches the "
                    "cluster or the repos).",
    )
    source: str | None = Field(
        default=None,
        description="action=store: a Benchmark Report file OR a run directory (its newest "
                    "report is used). The report is schema-validated before it is stored.",
    )
    label: str | None = Field(
        default=None,
        description="action=store: a short human label for this result, e.g. "
                    "'8B baseline, concurrency=16'.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="action=store: free-form tags to group related results "
                    "(e.g. ['8B','baseline']); filter by one later with filter_tag.",
    )
    spec: str | None = Field(default=None, description="action=store: spec used (provenance).")
    harness: str | None = Field(default=None, description="action=store: harness used (provenance).")
    workload: str | None = Field(default=None, description="action=store: workload used (provenance).")
    namespace: str | None = Field(default=None, description="action=store: namespace used (provenance).")
    session_id: str | None = Field(default=None, description="action=store: originating chat id (provenance).")
    record_id: str | None = Field(
        default=None, description="action=get/delete: the stored record's id (from a prior list).",
    )
    metric: str | None = Field(
        default=None,
        description="action=trend: which metric to trend. One of ttft / tpot / itl / "
                    "request_latency / output_token_rate / total_token_rate / request_rate / "
                    "success_rate_pct.",
    )
    filter_tag: str | None = Field(
        default=None, description="action=list/trend: only include results carrying this tag.",
    )
    filter_model: str | None = Field(
        default=None, description="action=list/trend: only include results for this model name.",
    )
    start_date: str | None = Field(
        default=None,
        description="action=list/trend: only include results STORED on or after this date "
                    "(inclusive). Accepts an ISO-8601 date ('2026-05-01') or datetime "
                    "('2026-05-01T00:00:00'); a bare date is treated as 00:00:00 UTC that day. "
                    "Filters on each record's stored_at (when it was persisted to history), the "
                    "only timestamp every record carries. Omit for no lower bound.",
    )
    end_date: str | None = Field(
        default=None,
        description="action=list/trend: only include results STORED on or before this date. A "
                    "bare date ('2026-06-15') is treated as the END of that day (23:59:59.999 "
                    "UTC) so the day is inclusive; a full datetime is used as-is. Filters on "
                    "stored_at. Omit for no upper bound.",
    )


class ExportRunBundleInput(BaseModel):
    """Capture a reproducibility PROVENANCE BUNDLE for a validated run (read-only: git reads +
    a workspace write). See read_knowledge('reproducibility')."""

    source: str = Field(
        ...,
        description="A Benchmark Report file OR a run directory (its newest report is used). The "
                    "report is schema-validated FIRST — an unvalidated report is refused (a bundle "
                    "only ever certifies a schema-valid run).",
        min_length=1,
    )
    namespace: str | None = Field(
        default=None,
        description="The namespace the run targeted (from the approved SessionPlan). Used to "
                    "build the copy-paste regenerate command (llmdbenchmark run -c <cfg> -p <ns>).",
    )
    spec: str | None = Field(default=None, description="spec used (provenance / re-derive a rerun plan).")
    harness: str | None = Field(default=None, description="harness used (provenance); falls back to the report's own.")
    workload: str | None = Field(default=None, description="workload used (provenance).")
    model: str | None = Field(default=None, description="model served (provenance); falls back to the report's own.")
    slo: dict[str, Any] | None = Field(
        default=None,
        description="The approved SessionPlan's SLO block, if any, so a reproduce can re-derive "
                    "the SLO verdicts.",
    )
    label: str | None = Field(default=None, description="A short human label for this bundle, e.g. '8B baseline'.")
    attach_to_history: bool = Field(
        default=False,
        description="Also attach this bundle's id + provenance to the matching stored history "
                    "record (if one exists). The result is stored separately via result_history; "
                    "this just links them.",
    )


class ReproduceRunInput(BaseModel):
    """Read a saved provenance bundle and return a structured rerun PROPOSAL — it mutates
    NOTHING. The agent then drives propose_session_plan -> dry-run -> approved -c replay. See
    read_knowledge('reproducibility')."""

    bundle_id: str = Field(
        ...,
        description="The id of a previously exported provenance bundle (from export_run_bundle or "
                    "a history record). reproduce_run returns the captured spec/harness/workload/"
                    "namespace/slo + run-config path + the dry-run-FIRST sequence; it emits NO "
                    "mutating command. Replay still goes through SessionPlan approval + the CLI "
                    "--dry-run gate, never around them.",
        min_length=1,
    )


class InspectWorkloadProfileInput(BaseModel):
    workload: str = Field(
        ...,
        description="The workload profile name as the agent uses it elsewhere, e.g. "
                    "'chatbot_synthetic.yaml', 'sanity_random.yaml', "
                    "'guide_pd-disaggregation_1.yaml' (the '.yaml'/'.yaml.in' suffix is "
                    "optional). Must be one of the profiles that actually exist on disk for "
                    "the harness — list_catalog enumerates them.",
    )
    harness: str | None = Field(
        default=None,
        description="Optional harness whose profiles dir to look in (inference-perf, guidellm, "
                    "vllm-benchmark, aiperf, inferencemax, nop). If omitted, every harness dir "
                    "is searched (inference-perf first).",
    )


class EstimateRunDurationInput(BaseModel):
    workload: str = Field(
        ...,
        description="The workload profile name to estimate a wall-clock duration for (same "
                    "naming as inspect_workload_profile / list_catalog), e.g. "
                    "'chatbot_synthetic.yaml'.",
    )
    harness: str | None = Field(
        default=None,
        description="Optional harness whose profiles dir to look in. If omitted, every harness "
                    "dir is searched (inference-perf first).",
    )


class NextStepSuggestion(BaseModel):
    """One clickable next-step button: a short `label` the user sees on the pill, and the
    `prompt` that is sent AS-IF the user typed it when they click it."""

    label: str = Field(
        ...,
        description="The short button text the user sees (≈2-5 words, e.g. 'Save as baseline', "
                    "'Run a higher-load sweep', 'Tear down the stack'). Keep it tight — it must "
                    "fit on a small pill.",
        min_length=1,
        max_length=48,
    )
    prompt: str = Field(
        ...,
        description="The message sent on the user's behalf when they click this button — phrase "
                    "it in the FIRST PERSON as the user's own request (e.g. 'Save this run as my "
                    "baseline so we can trend future runs against it'). On click it is submitted "
                    "exactly as if the user typed it, so make it a complete, unambiguous instruction.",
        min_length=1,
    )


class SuggestNextStepsInput(BaseModel):
    suggestions: list[NextStepSuggestion] = Field(
        ...,
        description="2-4 next-step options to render as clickable buttons under your reply. "
                    "Use this INSTEAD of asking 'want me to…?' in prose: each option is one "
                    "concrete follow-up the user can take with a single click. Order them best "
                    "first. Make them genuinely distinct and actionable in the CURRENT context "
                    "(e.g. after a run: save baseline / compare / sweep / tear down).",
        min_length=1,
        max_length=4,
    )


class EnableAdvancedToolsInput(BaseModel):
    """No arguments — calling the tool IS the request to reveal the advanced tool set."""
