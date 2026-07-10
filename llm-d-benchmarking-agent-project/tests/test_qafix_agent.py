# Merged QA-fix agent tests (knowledge-honesty + security/governance + tools/capacity/history/config/report).
# Concatenated from three sources; each section is preserved below under its own
# `# ── <original file> ──` banner with the original module docstring kept verbatim
# as a comment block. The honesty section's `_read` helper is renamed `_kread` to
# avoid colliding with the security section's differently-bodied `_read`.
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from app.agent.prompt import HARD_RULES, ROLE, build_system_prompt
from app.capacity.planner import (
    _deep_merge,
    apply_overrides,
    classify_diagnostics,
    plan_config_for_spec,
)
from app.config import get_settings


# ── test_qafix_honesty_knowledge.py ──
# """QA-fix regression guards — the *honesty* rules added to the agent's brain.
#
# These are hermetic content assertions over the editable ``knowledge/`` files. They lock in
# the anti-fabrication guidance added to fix a batch of QA findings (absent-metric / P99
# fabrication, SIMULATE probe narration as real host facts, trusting user-supplied data as
# validated, "live catalog" claimed without a tool call, and throughput-vs-concurrency).
#
# The JUDGMENT lives in the markdown/yaml; these tests only assert the guidance is PRESENT and
# loads cleanly, so a future edit can't silently strip it. No network / repo / cluster needed.
# """
def _kdir() -> Path:
    return get_settings().knowledge_dir


def _kread(name: str) -> str:
    # Resolve by basename through the topic-folder layout (basenames stay globally unique).
    return next(_kdir().rglob(name)).read_text(encoding="utf-8")


# ---- absent-metric / P99 fabrication (findings sim-1 ×3, sim-2 ×2) -------------------

def test_results_interpretation_has_absent_metric_floor() -> None:
    txt = _kread("results_interpretation.md").lower()
    assert "honesty floor" in txt
    assert "ttft_ms_p99" in txt  # names the field that is absent in SIMULATE
    assert "not available" in txt
    assert "cannot be verified" in txt
    # must explicitly forbid deriving p99 from p90/p50
    assert "p90/p50" in txt or "from p90" in txt


def test_analysis_absent_metric_is_inconclusive_not_verdict() -> None:
    txt = _kread("analysis.md").lower()
    assert "inconclusive" in txt
    assert "extrapolate" in txt
    # never a definitive verdict on an unmeasured metric
    assert "unmeasured metric" in txt


# ---- self-estimate misattributed to the sim engine (finding sim-1 21:30) -------------

def test_results_interpretation_forbids_misattributing_estimate() -> None:
    txt = _kread("results_interpretation.md").lower()
    assert "placeholder output" in txt  # the exact false attribution to forbid
    assert "you** estimated it" in txt  # attribute the estimate to yourself


# ---- SIMULATE honesty: read-only probes are REAL; simulated mutation OUTCOMES are not ----

def test_sim_integration_readonly_real_mutations_simulated() -> None:
    txt = " ".join(_kread("sim_integration.md").lower().split())  # collapse line-wraps
    # read-only commands run for real → trust their genuine output (fix for the blind no-op world)
    assert "read-only commands run for real" in txt
    assert "trust" in txt
    # the OUTCOME of a simulated mutation must never be narrated as real
    assert "nothing was actually deployed or benchmarked" in txt
    # the specific over-claims to forbid (a no-op standup/run did NOT make these true)
    assert "the stack is deployed" in txt
    assert "the endpoint is serving" in txt


# ---- trusting user-supplied data as validated (sim-1 03:30 / 17:30, sim-2 09:10) -----

def test_results_interpretation_only_validated_reports_authoritative() -> None:
    txt = _kread("results_interpretation.md").lower()
    assert "pasted, typed, or recalled" in txt
    assert "unverified input, not data" in txt
    # verbal disclaimer is not a substitute for refusing the numbers
    assert "verbal disclaimer is not a substitute" in txt
    # ghost-job + "today's" recency framing
    assert "your job completed before the crash" in txt
    assert "today's" in txt


def test_history_refuses_unverified_baselines() -> None:
    txt = _kread("history.md").lower()
    assert "never seed history/trends with user-asserted numbers" in txt
    assert "re-run" in txt


# ---- "live catalog" without a tool call (sim-2 00:45 + 19:10) ------------------------

def test_multi_harness_no_fake_live_catalog_claim() -> None:
    txt = _kread("multi_harness.md").lower()
    assert "live catalog snapshot" in txt
    assert "list_catalog" in txt
    assert "prior knowledge" in txt


# ---- throughput vs concurrency / Little's Law (AGENT_FINDINGS 21:44) ------------------

def test_capacity_uses_littles_law_for_concurrency() -> None:
    txt = _kread("capacity.md").lower()
    assert "little's law" in txt
    assert "throughput × per-request latency" in txt or "throughput x per-request latency" in txt
    assert "max concurrent requests" in txt


# ---- the edited files still parse / load cleanly -------------------------------------

def test_standard_metrics_yaml_still_parses() -> None:
    data = yaml.safe_load(_kread("standard_metrics.yaml"))
    assert isinstance(data, dict) and "metrics" in data


@pytest.mark.parametrize(
    "name",
    [
        "results_interpretation.md",
        "sim_integration.md",
        "analysis.md",
        "capacity.md",
        "history.md",
        "multi_harness.md",
    ],
)
def test_edited_markdown_nonempty(name: str) -> None:
    assert len(next(_kdir().rglob(name)).read_text(encoding="utf-8").strip()) > 0


# ── test_qafix_security_governance.py ──
# """QA-fix guardrails (security + safety invariants).
#
# These hermetic tests pin the *content* of the rules the QA findings asked for — the agent's
# judgment lives in knowledge/ + the prompt prefix, so we assert the rules are PRESENT and reachable
# (not that a live LLM obeyed them — that is the live-eval suite's job). They guard against silent
# regression of:
#   - first-turn engage-don't-resplash + blank-message handling (findings sim-2/real-1/real-2 etc.);
#   - explicit injection/override NAMING + refusal on every turn incl. turn 1 (sim-1/sim-3);
#   - safety gates that authority claims/framing cannot override (sim-1/sim-3/sim-4);
#   - cloud-scope / credential-channel / SSRF / privileged-namespace rules (sim-1/sim-2);
#   - the canonical sanity_random workload path so the agent stops path-guessing (sim-1);
#   - the corrected "knowledge/ is my own project, not the read-only repos" reasoning (sim-1).
#
# All read the REAL shipped files; none drive the LLM, touch a cluster, or spend quota.
# """
PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = PROJECT_ROOT / "knowledge"


def _read(name: str) -> str:
    return next(KNOWLEDGE.rglob(name)).read_text()


# ---- first-turn behavior is wired into the byte-stable prefix (#1/#2) --------

def test_role_prefix_wires_first_turn_engage_and_blank_handling():
    role = ROLE.lower()
    # engage the first message instead of re-greeting
    assert "re-greet" in role or "re-greet" in ROLE.lower()
    assert "welcome card" in role and "first message" in role
    # blank / whitespace handling, with the exact user-facing acknowledgement
    assert "blank message" in role
    assert "whitespace" in role
    # do not fabricate that the user "shared" anything
    assert "fabricate" in role


def test_role_prefix_routes_injection_to_explicit_naming():
    # turn 1 must behave like later turns: name + refuse, never silently drop
    assert "injection" in ROLE.lower()
    assert "name it" in ROLE.lower() or "name and refuse" in ROLE.lower() or "name it and refuse" in ROLE.lower()
    assert "silently" in ROLE.lower()
    assert "governance.md" in ROLE


def test_knowledge_misattribution_corrected_in_prefix():
    # #5: declining to edit knowledge/ must NOT claim the upstream repos are the reason
    hr = HARD_RULES
    assert "knowledge/" in hr
    assert "own project" in hr.lower()
    assert "no write-file tool" in hr.lower() or "write-file tool" in hr.lower()


def test_system_prompt_still_byte_stable_and_contains_rules(tool_ctx):
    # the prefix must stay byte-identical across builds (prompt-cache invariant) ...
    assert build_system_prompt(tool_ctx) == build_system_prompt(tool_ctx)
    # ... and actually carry the new wiring
    p = build_system_prompt(tool_ctx)
    assert "blank message" in p
    assert "injection" in p.lower()


# ---- conversation_style.md (CORE) reinforces first-turn + injection ----------

def test_conversation_style_first_message_section():
    cs = _read("conversation_style.md").lower()
    assert "first message" in cs
    assert "re-greet" in cs or "don't re-greet" in cs
    assert "blank message" in cs
    assert "injection" in cs and "governance.md" in cs


def test_welcome_card_declares_itself_the_only_greeting():
    w = _read("welcome.md").lower()
    assert "only greeting" in w
    # parser-affecting structure unchanged: still has the Capabilities + Nudge sections
    raw = _read("welcome.md")
    assert "### Capabilities" in raw and "### Nudge" in raw


# ---- governance.md safety invariants (#3) -----------------------------------

def test_governance_safety_gates_present():
    g = _read("governance.md").lower()
    # readiness gate not overridable by authority
    assert "ready == false" in g or "ready=false" in g or "readiness" in g
    assert "authority" in g
    # verify own allowlist before affirming a user's claim
    assert "allowlist" in g and ("let me check" in g or "let me verify" in g)
    # SIMULATE disclaimer is a safety invariant, not formatting
    assert "simulate disclaimer" in g or "simulate" in g
    assert "footnote" in g
    # SLO threshold post-hoc loosening
    assert "post-hoc" in g or "cherry-pick" in g
    # material scope change => new SessionPlan
    assert "sessionplan" in g and "scope" in g


def test_governance_scope_credentials_and_ssrf_present():
    g = _read("governance.md").lower()
    # never solicit cloud credentials
    assert "never solicit" in g or "do not proactively offer" in g
    assert "bearer token" in g
    # never claim a credential channel a tool lacks (-U has no --api-key)
    assert "--api-key" in g or "api-key" in g
    assert "endpoint_url" in g or "-u" in g
    # SSRF / metadata endpoint warning
    assert "ssrf" in g
    assert "169.254.169.254" in g
    # privileged namespaces refused
    assert "kube-system" in g
    assert "kube-public" in g and "kube-node-lease" in g


def test_governance_injection_section_present_and_closes_source_loophole():
    g = _read("governance.md").lower()
    assert "ignore previous instructions" in g
    assert "system note" in g
    assert "name it" in g and "refuse" in g
    # #9: refuse regardless of source; do not misattribute a user msg to a tool;
    # close the "but a human asking would pass" loophole.
    assert "regardless of" in g and "source" in g
    assert "false statement" in g or "did not" in g or "didn't" in g
    assert "loophole" in g or "gap" in g


# ---- deploy/teardown flow rules (#6/#7) -------------------------------------

def test_quickstart_playbook_no_midflow_halt_and_always_teardown():
    q = _read("quickstart_playbook.md").lower()
    assert "optional" in q and "metrics-server" in q
    assert "teardown" in q and "left up" in q or "leave the cluster" in q
    assert "garbled" in q  # low-confidence intent must clarify first
    assert "cluster name" in q


def test_run_lifecycle_partial_flow_teardown_rule():
    r = _read("run_lifecycle.md").lower()
    assert "partial flow" in r or "partial deployment" in r
    assert "teardown" in r
    assert "abandon" in r


def test_deploy_path_playbook_points_at_completion_rule():
    d = _read("deploy_path_playbook.md").lower()
    assert "no optional mid-flow gates" in d or "mid-flow" in d
    assert "teardown" in d
    assert "irreversible" in d and "clarif" in d


# ---- canonical workload path (#8) -------------------------------------------

def test_key_docs_lists_canonical_sanity_random_path():
    data = yaml.safe_load(_read("key_docs.yaml"))
    wp = data.get("workload_profiles")
    assert wp, "key_docs.yaml must carry a workload_profiles section"
    paths = [f["path"] for f in wp["files"]]
    assert "llm-d-benchmark/workload/profiles/inference-perf/sanity_random.yaml.in" in paths


def test_canonical_sanity_random_path_actually_exists_in_repo():
    """The documented path must resolve under the READ-ONLY llm-d-benchmark checkout —
    otherwise the agent would still be sent on a doomed read_repo_doc."""
    s = get_settings()
    bench = s.repo_paths.get("llm-d-benchmark")
    if bench is None or not bench.is_dir():
        import pytest

        pytest.skip("llm-d-benchmark repo not present (worktree without REPOS_DIR)")
    rel = "workload/profiles/inference-perf/sanity_random.yaml.in"
    assert (bench / rel).is_file(), f"canonical workload path missing: {bench / rel}"


# ── test_qafix_tools_capacity_history_config_report.py ──
# """QA-fix regression tests (real-1/real-2/sim-1/sim-2 findings).
#
# Covers five fixes, all in our wrapper layer (the upstream planner stays read-only):
#
#   #1 check_capacity no longer crashes with AttributeError on minimal overrides — a
#      scenario key with a bare `None` value (examples/gpu's `decode:`) must NOT clobber the
#      default block (our _deep_merge now mirrors upstream's None-skip contract).
#   #2 a 0-replica / un-sized run is INCONCLUSIVE (feasible=None), not feasible:true; and a
#      `model` override also syncs model.huggingfaceId so the planner sizes + gates the
#      OVERRIDE model, not the spec default.
#   #3 locate_and_parse_report surfaces a generated_at timestamp (report time or file mtime).
#   #4 result_history supports start_date/end_date filtering (on stored_at) and advertises
#      supported_filters.
#   #5 write_and_validate_config flags fabricated vLLM flag names in an advisory
#      unrecognized_flags list (non-fatal).
# """
# ---- #1 + #2 : merge None-skip, no crash, model/huggingfaceId sync -----------

def test_deep_merge_skips_none_override_keeping_default_dict():
    # A YAML key present-but-null (scenario `decode:` with all sub-keys commented) must NOT
    # wipe the default block — that None-deref is the real-1 #1 crash root cause.
    base = {"decode": {"enabled": True, "replicas": 1}, "model": {"name": "x"}}
    over = {"decode": None, "model": {"name": "y"}}
    out = _deep_merge(base, over)
    assert out["decode"] == {"enabled": True, "replicas": 1}  # default kept, not None
    assert out["model"]["name"] == "y"  # a real override still wins


def test_plan_config_gpu_spec_minimal_overrides_no_none_section(bench_repo):
    # real-1 #1: examples/gpu + {model, gpu_memory_gb} used to leave decode=None and crash the
    # planner. The merged decode block must now be a populated dict with the default replicas.
    pc, applied = plan_config_for_spec(
        bench_repo, "examples/gpu",
        overrides={"model": "meta-llama/Llama-3.1-70B-Instruct", "gpu_memory_gb": 80},
    )
    assert isinstance(pc.get("decode"), dict)
    assert pc["decode"].get("replicas") == 1  # default preserved, not wiped to None/0


def test_model_override_syncs_huggingface_id(bench_repo):
    # real-2 #2: the planner sizes/gates `model.huggingfaceId or model.name`; overriding only
    # model.name left huggingfaceId at the spec default (facebook/opt-125m). A `model` override
    # must drive BOTH so the EVALUATED model is the one the user asked for.
    pc, applied = plan_config_for_spec(
        bench_repo, "examples/gpu",
        overrides={"model": "meta-llama/Llama-3.1-405B-Instruct", "gpu_memory_gb": 10},
    )
    assert pc["model"]["name"] == "meta-llama/Llama-3.1-405B-Instruct"
    assert pc["model"]["huggingfaceId"] == "meta-llama/Llama-3.1-405B-Instruct"
    assert any("huggingfaceId" in a for a in applied)


def test_explicit_huggingface_id_override_not_clobbered_by_model():
    # If the caller passes both, the explicit huggingface_id wins (no silent overwrite).
    pc = {"model": {"name": "d", "huggingfaceId": "d"}}
    apply_overrides(pc, {"model": "org/Foo", "huggingface_id": "org/Foo-HF"})
    assert pc["model"]["name"] == "org/Foo"
    assert pc["model"]["huggingfaceId"] == "org/Foo-HF"


def test_classify_zero_replica_skip_is_inconclusive_not_feasible():
    # real-2 #2: every method skipped for 0 replicas => VRAM sizing bypassed => feasibility was
    # NOT evaluated. Must be feasible=None (inconclusive), never feasible:true.
    diags = [
        "Deployment method is modelservice -- checking decode and prefill configurations",
        "decode is disabled or has 0 replicas -- skipping",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is None
    assert v.sizing_evaluated is False
    assert "0 decode/prefill replicas" in v.inconclusive_reason


def test_classify_gpu_memory_check_skipped_is_inconclusive():
    # Sizing bypassed because model architecture / GPU memory was unknown — the count line is
    # NOT a fit verdict, so feasibility is inconclusive.
    diags = [
        "[decode] 10 GB per GPU, 10 x 0.95 = 9.5 GB available",
        "[decode] Each replica requires 1 GPUs, total available GPU memory = 9.5 GB",
        "[decode] WARNING: Model architecture info not available -- skipping memory checks.",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is None
    assert v.sizing_evaluated is False


def test_classify_real_sizing_stays_feasible():
    # A run where VRAM sizing actually completed (no skip line) stays feasible:true.
    diags = [
        "[decode] 80 GB per GPU, 80 x 0.9 = 72.0 GB available GPU memory",
        "[decode] mistral requires 13.49 GB; KV cache fits",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is True
    assert v.sizing_evaluated is True


def test_classify_empty_and_info_only_stay_feasible():
    # Absence of sizing facts is NOT evidence of a skip — must not regress to inconclusive.
    assert classify_diagnostics([]).feasible is True
    info_only = classify_diagnostics(["[decode] Max concurrent requests: 128"])
    assert info_only.feasible is True


def test_classify_hard_error_wins_over_skip():
    # An ERROR / will-fail is authoritative even when a skip line is also present.
    diags = [
        "decode is disabled or has 0 replicas -- skipping",
        "[decode] ERROR: DEPLOYMENT WILL FAIL: cannot serve",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is False
    assert v.will_fail is True


def test_classify_sizing_exception_is_inconclusive_not_feasible():
    # BUG-030: the "...available GPU memory" line is the GPU-COUNT summary, NOT proof sizing ran.
    # When a method's KV-cache sizing THROWS (only a WARNING under ignoreFailedValidation) while
    # the other method is 0-replica-skipped, feasibility was NOT evaluated -> must be inconclusive,
    # never feasible:true (else the agent tells the user a deployment "fits" before a doomed standup).
    unsized = [
        "[decode] Each replica requires 1 GPUs, total available GPU memory = 72.0 GB",
        "[decode] WARNING: Cannot estimate model memory or KV cache for meta/Llama-3.1-405B: HTTP 401",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v = classify_diagnostics(unsized)
    assert v.feasible is None and v.sizing_evaluated is False
    # A genuine fit (the real KV-cache fit lines) with a 0-replica skip elsewhere stays feasible:true.
    fit = [
        "[decode] Allocatable KV cache memory: 30.00 GB",
        "[decode] Per-request KV cache (max_model_len=4096): 0.50 GB",
        "[decode] Max concurrent requests (worst case, each at max_model_len): 60",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v2 = classify_diagnostics(fit)
    assert v2.feasible is True and v2.sizing_evaluated is True


def test_classify_gpu_count_shortfall_is_infeasible_not_feasible():
    # BUG-030 class: under the DEFAULT enforce=False path the upstream planner tags its
    # "<N> GPUs are required per replica" GPU-COUNT shortfall as a WARNING (not ERROR / not
    # DEPLOYMENT WILL FAIL), then continues sizing as if the GPUs existed and emits the FIT
    # KV-cache markers. So the spec asks TP=4 (4 GPUs/replica) on a 2-GPU pod — a hard
    # won't-deploy — yet the verdict read feasible:true. It must be infeasible (the same
    # condition is ERROR -> infeasible under enforce=True).
    diags = [
        "[decode] WARNING: Accelerator requested is 2 but TP x PP x DP = 4 x 1 x 1 "
        "= 4 GPUs are required per replica",
        "[decode] 80 GB per GPU, 80 x 0.9 = 72.0 GB available",
        "[decode] Each replica requires 4 GPUs, total available GPU memory = 288.0 GB",
        "[decode] Allocatable KV cache memory: 200.00 GB",
        "[decode] Per-request KV cache (max_model_len=4096): 0.50 GB",
        "[decode] Max concurrent requests (worst case, each at max_model_len): 400",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is False
    assert v.will_fail is True
    # The benign "Some GPUs will be idle" warning must NOT trip the shortfall marker.
    idle = classify_diagnostics(
        [
            "[decode] WARNING: Each replica requires 1 GPUs, but 2 requested per pod. "
            "Some GPUs will be idle.",
            "[decode] Allocatable KV cache memory: 30.00 GB",
            "[decode] Per-request KV cache (max_model_len=4096): 0.50 GB",
        ]
    )
    assert idle.feasible is True


def test_classify_fma_method_is_inconclusive_not_feasible():
    # BUG-030/035 class, the fma early-return path: for an `fma` (fast model actuation)
    # deployment the upstream run_capacity_planner returns EARLY with NO sizing at all —
    # "Deployment method is fma -- skipping vLLM capacity validation" — and the bridge falls
    # back to the framing log lines. classify saw no FAIL/ERROR/skip/sized marker and read the
    # un-evaluated run as feasible:true (the `examples/fma` catalog spec sets fma.enabled:true,
    # so the agent would tell the user a deployment "fits" though capacity was never checked).
    # It must downgrade to INCONCLUSIVE (feasible=None), like every other un-sized path.
    diags = [
        "Validating vLLM configuration against Capacity Planner "
        "(deployment will continue even if validation fails)",
        "Deployment method is fma -- skipping vLLM capacity validation",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is None
    assert v.sizing_evaluated is False
    assert v.will_fail is False
    assert "fma" in v.inconclusive_reason
    # A genuine fit (the real KV-cache fit lines) must STILL read feasible:true — the fma
    # marker must not false-match a sized run.
    fit = classify_diagnostics(
        [
            "[decode] Allocatable KV cache memory: 30.00 GB",
            "[decode] Per-request KV cache (max_model_len=4096): 0.50 GB",
            "[decode] Max concurrent requests (worst case, each at max_model_len): 60",
        ]
    )
    assert fit.feasible is True and fit.sizing_evaluated is True


# ---- #3 : locate_and_parse_report generated_at -------------------------------

def _write_min_report(path: Path, *, end: str | None) -> None:
    import yaml
    run: dict = {"uid": "run-123"}
    if end is not None:
        run["time"] = {"start": "2026-05-01T00:00:00Z", "end": end}
    path.write_text(yaml.safe_dump({"run": run, "scenario": {}, "results": {}}))


def test_report_generated_at_prefers_report_time(tool_ctx):
    from app.tools.analyze.report_locate import _report_generated_at
    from app.validation.report import load_report
    p = tool_ctx.workspace / "benchmark_report_v0.2.yaml"
    tool_ctx.workspace.mkdir(parents=True, exist_ok=True)
    _write_min_report(p, end="2026-06-10T12:00:00Z")
    when, source = _report_generated_at(load_report(p), p)
    assert when == "2026-06-10T12:00:00Z"
    assert "run.time.end" in source


def test_report_generated_at_falls_back_to_mtime(tool_ctx):
    from app.tools.analyze.report_locate import _report_generated_at
    from app.validation.report import load_report
    p = tool_ctx.workspace / "benchmark_report_v0.2.yaml"
    tool_ctx.workspace.mkdir(parents=True, exist_ok=True)
    _write_min_report(p, end=None)  # no run.time block
    when, source = _report_generated_at(load_report(p), p)
    assert when is not None  # an ISO mtime string
    assert "mtime" in source


def test_locate_report_result_carries_generated_at(tool_ctx):
    from app.tools.analyze.report_locate import locate_and_parse_report
    p = tool_ctx.workspace / "benchmark_report_v0.2.yaml"
    tool_ctx.workspace.mkdir(parents=True, exist_ok=True)
    _write_min_report(p, end="2026-06-10T12:00:00Z")
    res = locate_and_parse_report(tool_ctx)
    assert res["found"] is True
    assert "generated_at" in res
    assert res["generated_at"] == "2026-06-10T12:00:00Z"


# ---- #4 : result_history date filtering --------------------------------------

@pytest.mark.asyncio
async def test_result_history_list_advertises_supported_filters(tool_ctx):
    from app.tools.analyze.history import result_history
    res = await result_history(tool_ctx, action="list")
    assert "supported_filters" in res
    assert "start_date" in res["supported_filters"] and "end_date" in res["supported_filters"]


def test_filter_by_date_inclusive_bounds():
    from app.storage.history import HistoryRecord
    from app.tools.analyze.history import _filter_by_date

    def rec(epoch: float) -> HistoryRecord:
        return HistoryRecord(id=str(epoch), stored_at=epoch, label=None)

    # 2026-05-15, 2026-06-20 (UTC)
    import datetime as _dt
    may15 = _dt.datetime(2026, 5, 15, tzinfo=_dt.UTC).timestamp()
    jun20 = _dt.datetime(2026, 6, 20, tzinfo=_dt.UTC).timestamp()
    recs = [rec(may15), rec(jun20)]
    out, applied = _filter_by_date(recs, "2026-05-01", "2026-06-15")
    assert [r.stored_at for r in out] == [may15]  # jun20 excluded
    assert applied.get("start_date") == "2026-05-01"


def test_filter_by_date_end_is_inclusive_of_whole_day():
    import datetime as _dt

    from app.storage.history import HistoryRecord
    from app.tools.analyze.history import _filter_by_date
    # A record stored late on the end-date day must be INCLUDED (bare end date => 23:59:59).
    late = _dt.datetime(2026, 6, 15, 23, 30, tzinfo=_dt.UTC).timestamp()
    out, _ = _filter_by_date([HistoryRecord(id="x", stored_at=late, label=None)],
                             None, "2026-06-15")
    assert len(out) == 1


def test_filter_by_date_bad_input_reports_error_not_crash():
    from app.storage.history import HistoryRecord
    from app.tools.analyze.history import _filter_by_date
    recs = [HistoryRecord(id="x", stored_at=time.time(), label=None)]
    out, applied = _filter_by_date(recs, "not-a-date", None)
    assert "error" in applied
    assert out == recs  # unfiltered, but the error is machine-visible


# ---- #5 : unrecognized_flags advisory ----------------------------------------

def test_unrecognized_flags_catches_fabricated_vllm_flags(bench_repo):
    from app.tools.setup.config_artifact import _scenario_reference, unrecognized_flags
    ref = _scenario_reference(bench_repo)
    content = {
        "name": "fab",
        "vllmCommon.flags.enablePrefixCachingV2": True,   # fabricated (sim-1)
        "vllmCommon.kvCacheSharingStrategy": "cross-session",
        "speculativeDecodeTokenBudget": 512,
        "vllmCommon.flags.enableChunkedPrefillV3": True,
    }
    flagged = unrecognized_flags(content, ref)
    assert "vllmCommon.flags.enablePrefixCachingV2" in flagged
    assert "speculativeDecodeTokenBudget" in flagged
    assert len(flagged) == 4


def test_unrecognized_flags_passes_real_flags(bench_repo):
    from app.tools.setup.config_artifact import _scenario_reference, unrecognized_flags
    ref = _scenario_reference(bench_repo)
    content = {
        "name": "real",
        "vllmCommon.flags.enforceEager": True,
        "vllmCommon.flags.noPrefixCaching": True,
        "schedulerName": "custom",
        "routing.servicePort": 8080,
    }
    assert unrecognized_flags(content, ref) == []


def test_unrecognized_flags_no_reference_returns_empty():
    # No repo truth => we don't guess (advisory stays silent rather than false-flagging).
    from app.tools.setup.config_artifact import unrecognized_flags
    assert unrecognized_flags({"foo.bar": 1}, {}) == []


@pytest.mark.asyncio
async def test_write_config_surfaces_unrecognized_flags_non_fatally(tool_ctx):
    from app.tools.setup.config_artifact import write_and_validate_config
    res = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="fab.yaml",
        content={"name": "fab", "vllmCommon.flags.enablePrefixCachingV2": True,
                 "vllmCommon.flags.enforceEager": True},
    )
    # Non-fatal: the file is still authored (shape passes) but the fabricated flag is surfaced.
    assert res["valid"] is True
    assert "vllmCommon.flags.enablePrefixCachingV2" in res["unrecognized_flags"]
    assert "vllmCommon.flags.enforceEager" not in res["unrecognized_flags"]
    assert "unrecognized_flags_note" in res
