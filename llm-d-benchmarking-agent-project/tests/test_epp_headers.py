"""Phase 66 — EPP HTTP-header decoder (interpret 429s + x-llm-d-request-dropped-reason).

Pure-DATA phase: the rejected-vs-evicted-vs-broken judgment lives ENTIRELY in
knowledge/epp_headers.yaml, never in a Python if/elif. These tests assert the data catalog
loads, is reachable via read_knowledge("epp_headers"), is ON-DEMAND (de-inlined: NOT in
CORE_KNOWLEDGE / not baked into every system prompt, but listed in the on-demand knowledge
index so the agent knows to load it — results_interpretation.md routes here on a drop/429),
documents the x-llm-d-request-dropped-reason enum (both rejected-saturated and
evicted-priority) and the four SLO/objective/fairness header names, the deprecated aliases,
and frames drops as capacity/eviction (not breakage) with a remedy.

No GPU, no live cluster, no network, no real benchmark run.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.agent.prompt import CORE_KNOWLEDGE, build_system_prompt
from app.tools import probe

# The four header names the spec/HERMETIC-TEST require to be decoded, plus the two enum values.
REQUIRED_HEADER_NAMES = (
    "x-llm-d-slo-ttft-ms",
    "x-llm-d-slo-tpot-ms",
    "x-llm-d-inference-objective",
    "x-llm-d-inference-fairness-id",
)
REQUIRED_DROPPED_REASONS = ("rejected-saturated", "evicted-priority")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_knowledge_file(rel: str) -> str:
    return (_project_root() / rel).read_text()


def _epp_data() -> dict:
    data = yaml.safe_load(_read_knowledge_file("knowledge/epp_headers.yaml"))
    assert isinstance(data, dict)
    return data


# ---- (1) the catalog loads + is reachable via read_knowledge ---------------

def test_epp_headers_loads_and_is_valid_yaml():
    data = _epp_data()
    assert data["source"] == "llm-d/docs/api-reference/epp-http-headers.md"


def test_epp_headers_reachable_via_read_knowledge(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="epp_headers")
    assert out["name"] == "epp_headers.yaml"
    assert out["topic"] == "epp_headers"
    data = yaml.safe_load(out["content"])
    assert isinstance(data, dict)
    # The on-demand loader also accepts the full basename.
    out2 = probe.read_knowledge(tool_ctx, name="epp_headers.yaml")
    assert out2["name"] == "epp_headers.yaml"


# ---- (2) de-inlined: on-demand only (NOT in CORE), but listed in the index ----

def test_epp_headers_not_in_core_knowledge():
    # De-inlined to save ~2.5k tokens off the always-on prefix: this is a late-phase
    # failure-decoder, not interview/plan/deploy material. It loads on demand instead.
    assert "epp_headers.yaml" not in CORE_KNOWLEDGE


def test_epp_headers_indexed_for_on_demand_in_system_prompt(tool_ctx):
    prompt = build_system_prompt(tool_ctx)
    # NOT inlined verbatim as a core section anymore...
    assert "# Knowledge: epp_headers.yaml" not in prompt
    # ...but listed in the on-demand knowledge index so the agent knows to read_knowledge() it
    # (results_interpretation.md routes here when a run shows drops/429s).
    assert "epp_headers (epp_headers.yaml)" in prompt


# ---- (3) documents the dropped-reason enum (rejected/evicted/broken JUDGMENT) ----

def test_dropped_reason_enum_documents_both_required_values():
    enum = _epp_data()["dropped_reason_enum"]
    for reason in REQUIRED_DROPPED_REASONS:
        assert reason in enum, f"{reason} missing from dropped_reason_enum"
        entry = enum[reason]
        # Each value carries the cause + remedy + the capacity-not-breakage judgment.
        assert entry["cause"].strip()
        assert entry["remedy"].strip()
        assert entry["capacity_not_breakage"] is True


def test_dropped_reason_enum_covers_all_doc_values_with_correct_prefixes():
    """Every value from the upstream doc is catalogued, each tagged rejected-* / evicted-*."""
    enum = _epp_data()["dropped_reason_enum"]
    expected = {
        "rejected-saturated": "rejected",
        "rejected-ttl-expired": "rejected",
        "rejected-context-cancelled": "rejected",
        "evicted": "evicted",
        "evicted-queue-pressure": "evicted",
        "evicted-priority": "evicted",
    }
    assert set(enum) == set(expected)
    for value, prefix in expected.items():
        assert enum[value]["prefix"] == prefix


def test_rejected_saturated_reads_as_capacity_with_scale_remedy():
    """rejected-saturated = at admission capacity, shed before serving -> lower load / scale out."""
    entry = _epp_data()["dropped_reason_enum"]["rejected-saturated"]
    cause = entry["cause"].lower()
    remedy = entry["remedy"].lower()
    assert "capacity" in cause
    assert ("lower" in remedy and "concurrency" in remedy) or "scale out" in remedy


def test_evicted_priority_reads_as_preemption_not_failure():
    """evicted-priority = preempted mid-flight by higher-priority work (not a failure)."""
    entry = _epp_data()["dropped_reason_enum"]["evicted-priority"]
    cause = entry["cause"].lower()
    assert "priority" in cause and ("preempt" in cause or "higher-priority" in cause)
    # Remedy points at raising this request's objective priority or adding capacity.
    assert "objective" in entry["remedy"].lower() or "capacity" in entry["remedy"].lower()


# ---- (4) the four SLO/objective/fairness header names are decoded -----------

def test_four_required_header_names_are_decoded():
    data = _epp_data()
    decoded: dict[str, str] = {}
    for entry in data["slo_headers"] + data["request_headers"]:
        decoded[entry["name"]] = entry["meaning"]
    for name in REQUIRED_HEADER_NAMES:
        assert name in decoded, f"header {name} not decoded in epp_headers.yaml"
        assert decoded[name].strip(), f"header {name} has no meaning"


def test_slo_ttft_header_decoded_as_admission_target():
    slo = {e["name"]: e["meaning"].lower() for e in _epp_data()["slo_headers"]}
    assert "first token" in slo["x-llm-d-slo-ttft-ms"] or "ttft" in slo["x-llm-d-slo-ttft-ms"]
    assert "admi" in slo["x-llm-d-slo-tpot-ms"] or "admi" in slo["x-llm-d-slo-ttft-ms"]


# ---- (5) deprecated aliases + response-header prefix scheme -----------------

def test_deprecated_aliases_map_to_canonical_names():
    da = _epp_data()["deprecated_aliases"]
    assert da["canonical_wins"] is True
    assert da["case_insensitive"] is True
    pairs = {a["alias"]: a["canonical"] for a in da["aliases"]}
    assert pairs["x-gateway-inference-objective"] == "x-llm-d-inference-objective"
    assert pairs["x-slo-ttft-ms"] == "x-llm-d-slo-ttft-ms"
    assert pairs["x-slo-tpot-ms"] == "x-llm-d-slo-tpot-ms"
    # Every canonical target is a real x-llm-d-* header.
    assert all(c.startswith("x-llm-d-") for c in pairs.values())


def test_response_header_prefix_scheme_distinguishes_retry_cost():
    rh = {e["name"]: e for e in _epp_data()["response_headers"]}
    drop = rh["x-llm-d-request-dropped-reason"]
    # Only on EPP-generated 429s (so a drop is a decision, not a crash).
    assert "429" in drop["meaning"]
    scheme = drop["prefix_scheme"]
    assert "retry" in scheme["rejected"].lower()  # rejected-* = cheap to retry
    assert "evicted" in str(scheme).lower()


# ---- (6) narration reframes a failure fraction as a saturation/eviction signal ----

def test_narration_reframes_failures_as_capacity_not_breakage():
    narration = _epp_data()["narration"].lower()
    assert "broken" in narration  # explicitly says do NOT call it broken
    assert "capacity" in narration or "admission" in narration
    assert "eviction" in narration or "evicted" in narration


# ---- (7) results_interpretation.md routes the agent here --------------------

def test_results_interpretation_routes_to_epp_headers():
    md = _read_knowledge_file("knowledge/results_interpretation.md")
    assert 'read_knowledge("epp_headers")' in md
    assert "x-llm-d-request-dropped-reason" in md
    # It frames the routing around a non-100% success / 429.
    assert "429" in md


# ---- (8) no Python decision logic — the judgment is DATA only ---------------

def test_classification_is_data_not_python():
    """The rejected/evicted/broken classification must NOT live in any Python module — assert the
    enum values appear in the YAML but not as branch literals in the prompt/probe code."""
    yaml_text = _read_knowledge_file("knowledge/epp_headers.yaml")
    for reason in ("rejected-saturated", "evicted-priority", "rejected-ttl-expired",
                   "evicted-queue-pressure"):
        assert reason in yaml_text
    prompt_src = (_project_root() / "app" / "agent" / "prompt.py").read_text()
    probe_src = (_project_root() / "app" / "tools" / "probe.py").read_text()
    # The dropped-reason enum values must not be hard-coded into Python (no if/elif on them).
    for reason in ("rejected-saturated", "evicted-priority"):
        assert reason not in prompt_src
        assert reason not in probe_src
