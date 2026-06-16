"""QA-fix guardrails (security + safety invariants).

These hermetic tests pin the *content* of the rules the QA findings asked for — the agent's
judgment lives in knowledge/ + the prompt prefix, so we assert the rules are PRESENT and reachable
(not that a live LLM obeyed them — that is the live-eval suite's job). They guard against silent
regression of:
  - first-turn engage-don't-resplash + blank-message handling (findings sim-2/real-1/real-2 etc.);
  - explicit injection/override NAMING + refusal on every turn incl. turn 1 (sim-1/sim-3);
  - safety gates that authority claims/framing cannot override (sim-1/sim-3/sim-4);
  - cloud-scope / credential-channel / SSRF / privileged-namespace rules (sim-1/sim-2);
  - the canonical sanity_random workload path so the agent stops path-guessing (sim-1);
  - the corrected "knowledge/ is my own project, not the read-only repos" reasoning (sim-1).

All read the REAL shipped files; none drive the LLM, touch a cluster, or spend quota.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.agent.prompt import HARD_RULES, ROLE, build_system_prompt
from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = PROJECT_ROOT / "knowledge"


def _read(name: str) -> str:
    return (KNOWLEDGE / name).read_text()


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
