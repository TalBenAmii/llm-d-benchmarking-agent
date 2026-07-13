"""A frozen snapshot of the llm-d-benchmark catalog (specs / harnesses / workloads).

WHY THIS EXISTS
---------------
Flow validation must run **hermetically** in CI, where ``llm-d-benchmark`` and ``llm-d``
are empty gitlinks. But the things we want to validate consult the *live on-disk catalog*:

  * the command policy's ``ref_catalog`` checks (``--spec``/``-l``/``-w`` must name a
    real spec/harness/workload), and
  * the ``SessionPlan`` validator (``validate_plan``).

With no repo present those lists are empty and *every* spec/harness/workload would fail to
validate — so the harness could not exercise the real policy. Seeding a ``ToolContext``
with this snapshot lets the harness validate the agent's commands against the **real**
policy exactly as it would in production, with zero repos on disk.

This is a *snapshot*, not a source of truth. ``test_flows.py::test_snapshot_matches_live``
asserts that every name the flows reference still exists in the **live** catalog whenever
the repo IS present (local dev / a CI job that checks out the submodules), so drift between
this snapshot and the upstream repo is caught loudly.

Generated from ``app.tools.setup.catalog.build_catalog`` against llm-d-benchmark @ 2026-06-25.
Regenerate with:  ``make snapshot-catalog``  (see the Makefile).
"""
from __future__ import annotations

from typing import Any

# ---- the snapshot (shape mirrors build_catalog's return) ---------------------

SPECS: list[str] = [
    "cicd/cks", "cicd/gke", "cicd/kind", "cicd/ocp", "cicd/ocp-nofma-wva",
    "examples/cpu", "examples/fma", "examples/gpu", "examples/launcher",
    "examples/multi-model-wva", "examples/sim", "examples/spyre", "examples/spyre-s390x",
    "guides/agentic-serving", "guides/flow-control", "guides/optimized-baseline",
    "guides/pd-disaggregation", "guides/precise-prefix-cache-routing",
    "guides/predicted-latency-routing", "guides/tiered-prefix-cache", "guides/wide-ep-lws",
    "guides/workload-autoscaling",
]

HARNESSES: list[str] = [
    "aiperf", "guidellm", "inference-perf", "inferencemax", "nop", "vllm-benchmark",
]

WORKLOADS: list[str] = [
    "agentic_code_generation.yaml", "chatbot_sharegpt.yaml", "chatbot_synthetic.yaml",
    "code_completion_synthetic.yaml", "dataset.yaml", "fixed_dataset.yaml",
    "guide_multimodal-serving_1.yaml", "guide_optimized-baseline_1.yaml",
    "guide_pd-disaggregation_1.yaml", "guide_pd-disaggregation_2.yaml",
    "guide_precise-prefix-cache-routing_1.yaml", "guide_predicted-latency-routing_1.yaml",
    "guide_tiered-prefix-cache_1.yaml", "guide_wide-ep-lws_1.yaml",
    "guide_workload-autoscaling_1.yaml", "nop.yaml", "otel_traces.yaml",
    "random_concurrent.yaml", "sanity_concurrent.yaml", "sanity_random.yaml",
    "shared_prefix_multi_turn_chat.yaml", "shared_prefix_synthetic.yaml",
    "shared_prefix_synthetic_heavy.yaml", "shared_prefix_synthetic_short.yaml",
    "sharegpt.yaml", "sonnet_concurrent.yaml", "summarization_synthetic.yaml",
    "synthetic.yaml",
]

WORKLOADS_BY_HARNESS: dict[str, list[str]] = {
    "aiperf": ["dataset.yaml", "synthetic.yaml"],
    "guidellm": [
        "chatbot_synthetic.yaml", "guide_optimized-baseline_1.yaml",
        "guide_precise-prefix-cache-routing_1.yaml", "guide_workload-autoscaling_1.yaml",
        "sanity_concurrent.yaml", "sanity_random.yaml", "shared_prefix_synthetic.yaml",
        "summarization_synthetic.yaml",
    ],
    "inference-perf": [
        "agentic_code_generation.yaml", "chatbot_sharegpt.yaml", "chatbot_synthetic.yaml",
        "code_completion_synthetic.yaml", "guide_multimodal-serving_1.yaml",
        "guide_optimized-baseline_1.yaml", "guide_pd-disaggregation_1.yaml",
        "guide_pd-disaggregation_2.yaml", "guide_precise-prefix-cache-routing_1.yaml",
        "guide_predicted-latency-routing_1.yaml", "guide_tiered-prefix-cache_1.yaml",
        "guide_wide-ep-lws_1.yaml", "otel_traces.yaml", "random_concurrent.yaml",
        "sanity_random.yaml", "shared_prefix_multi_turn_chat.yaml",
        "shared_prefix_synthetic.yaml", "shared_prefix_synthetic_heavy.yaml",
        "shared_prefix_synthetic_short.yaml", "summarization_synthetic.yaml",
    ],
    "inferencemax": ["random_concurrent.yaml"],
    "nop": ["nop.yaml"],
    "vllm-benchmark": [
        "fixed_dataset.yaml", "random_concurrent.yaml", "sanity_random.yaml",
        "sharegpt.yaml", "sonnet_concurrent.yaml",
    ],
}

# Scenarios mirror the spec tree in this repo revision.
SCENARIOS: list[str] = list(SPECS)


def frozen_catalog() -> dict[str, Any]:
    """A catalog dict shaped exactly like ``app.tools.setup.catalog.build_catalog`` returns,
    so it can be dropped straight into ``ToolContext._catalog``."""
    return {
        "present": True,
        "repo_path": "<frozen-snapshot>",
        "specs": list(SPECS),
        "harnesses": list(HARNESSES),
        "workloads": list(WORKLOADS),
        "workloads_by_harness": {k: list(v) for k, v in WORKLOADS_BY_HARNESS.items()},
        "scenarios": list(SCENARIOS),
    }
