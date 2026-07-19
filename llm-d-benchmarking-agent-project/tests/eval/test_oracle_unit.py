"""(B) ALWAYS-ON, hermetic unit tests for the bug-hunter's DETERMINISTIC oracle + report
assembly — NO quota.

Two layers, both hermetic:
  1. Pure-function tests over SYNTHETIC fixtures: invariant→category mapping, severity map, the
     finding dedup, the report assembly + gate logic (only deterministic ``severity >= high``
     gates; advisory LLM findings never do).
  2. An end-to-end DETERMINISTIC bug-hunt: ``run_bughunt`` without ``use_llm`` (the seeded-RNG
     fallback selector) drives the REAL app over a couple of seeds and asserts ZERO oracle
     violations — the always-on guard that the invariant oracle + the driver wiring still hold.
     This is the bug-hunter's "0 findings" baseline, run for free on every push.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

from .bug_report import (
    Finding,
    build_bug_report,
    categorize_invariant,
    dedup_findings,
    finding_from_invariant,
    oracle_version,
    render_markdown,
    severity_for,
    severity_ge,
    write_bug_report,
)
from .explorer import ACTION_NAMES, max_selector_calls, run_bughunt

# The end-to-end hermetic hunt needs the bench repo (the agent loop reads the live catalog).
_BENCH_PRESENT = get_settings().bench_repo.is_dir()


def test_oracle_asset_has_version() -> None:
    assert oracle_version()  # non-empty; raises if the asset lost its version


def test_invariant_categorization() -> None:
    """Each proven invariant-battery message maps to the right oracle category. The cases use the
    REAL wording the battery emits (app_driver.py) and exercise every branch of
    ``categorize_invariant`` including the new-action messages and the default fallback."""
    cases = {
        # --- state_corruption (every triggering substring) ---
        "session ab12: on-disk transcript (5 msgs) is AHEAD of in-memory (3 msgs)": "state_corruption",
        "session ab12: on-disk transcript diverges from the in-memory prefix": "state_corruption",
        "session ab12 has duplicate in_flight_approvals: [x, x]": "state_corruption",
        "approval request_id r1 shared across sessions a and b (state leak)": "state_corruption",
        "parked gate not persisted on session s1": "state_corruption",
        "pending approval NOT re-emitted on reconnect to s1": "state_corruption",
        "gate not cleared after resolve on s1: []": "state_corruption",
        "decision not recorded after resolve on s1": "state_corruption",
        # new-action invariants (set_auto_approve / reopen_after_delete) — same categories, no new class.
        "session ab12: persisted auto_approve diverges from in-memory (False vs True)": "state_corruption",
        "reopened deleted session ab12: resume returned resumed=True — stale state not cleared":
            "state_corruption",
        # --- synthetic_leak ---
        "synthetic pre-probe leaked into history as a user message": "synthetic_leak",
        "session s1 title leaked synthetic pre-probe text": "synthetic_leak",
        "session ab12: persisted title leaked synthetic text: 'live catalog snapshot'": "synthetic_leak",
        # --- crash (5xx / server error) ---
        "/api/sessions returned 500": "crash",
        "DELETE /api/namespaces/llmd-quickstart returned 503": "crash",
        "/api/jobs?namespace=llmd-quickstart returned 500": "crash",
        "unexpected server error frame: {'kind': 'agent_error'}": "crash",
        # --- contract (protocol / liveness) ---
        "malformed bad_type frame not rejected as protocol_error": "contract",
        "ping did not get a pong": "contract",
        "WS handshake never completed before ready": "contract",  # defensive (no current emitter)
    }
    for msg, expected in cases.items():
        assert categorize_invariant(msg) == expected, msg
    # An unknown/novel anomaly falls back to the conservative 'contract' category (never silently
    # dropped) — the final return in categorize_invariant.
    assert categorize_invariant("some entirely novel anomaly the oracle has not seen") == "contract"


def test_severity_map_and_ordering() -> None:
    assert severity_for("state_corruption") == "high"
    assert severity_for("crash") == "high"
    assert severity_for("contract") == "medium"
    assert severity_for("synthetic_leak") == "medium"
    assert severity_for("unknown_category") == "info"
    assert severity_ge("high", "high")
    assert severity_ge("critical", "high")
    assert not severity_ge("medium", "high")
    assert not severity_ge("info", "medium")


def test_finding_from_invariant_and_severity() -> None:
    f = finding_from_invariant(
        "session ab12: on-disk transcript (5 msgs) is AHEAD of in-memory (3 msgs)",
        seed=42, action_index=17, repro_actions=["new_chat", "send_message", "switch_chat"],
    )
    assert f.deterministic is True
    assert f.category == "state_corruption"
    assert f.severity == "high"
    assert f.seed == 42 and f.action_index == 17
    assert f.repro_actions[-1] == "switch_chat"
    assert f.evidence["invariant"].startswith("session ab12")


def test_dedup_collapses_one_recurring_class() -> None:
    """One recurring invariant class collapses to a single finding (no spam); distinct classes
    are kept. The FIRST occurrence's repro (shortest) survives."""
    msg = "session x: on-disk transcript (5 msgs) is AHEAD of in-memory (3 msgs)"
    a = finding_from_invariant(msg, seed=1, action_index=3, repro_actions=["a", "b", "c"])
    b = finding_from_invariant(msg, seed=7, action_index=9, repro_actions=["a", "b", "c", "d", "e"])
    other = finding_from_invariant(
        "approval request_id r1 shared across sessions a and b (state leak)",
        seed=1, action_index=4, repro_actions=["a"],
    )
    deduped = dedup_findings([a, b, other])
    assert len(deduped) == 2
    assert deduped[0].repro_actions == ["a", "b", "c"]  # first occurrence kept


def test_dedup_multi_class_keeps_first_per_class_and_is_order_stable() -> None:
    """Distinct invariant CLASSES are each kept (one finding per class) in first-seen order, and the
    FIRST occurrence of each class survives — the real contract. (The explorer feeds findings in
    trace-growth order, so the first occurrence also happens to carry the shortest repro; this does
    NOT assert "shortest", which would be a misread of the dedup rule.)"""
    a_msg = "session x: on-disk transcript (5 msgs) is AHEAD of in-memory (3 msgs)"  # state_corruption
    b_msg = "session y title leaked synthetic pre-probe text"                        # synthetic_leak
    c_msg = "/api/sessions returned 500"                                             # crash
    a1 = finding_from_invariant(a_msg, seed=1, action_index=1, repro_actions=["n"])
    b1 = finding_from_invariant(b_msg, seed=1, action_index=2, repro_actions=["n", "s"])
    a2 = finding_from_invariant(a_msg, seed=2, action_index=8, repro_actions=["n", "s", "w", "c", "x"])
    b2 = finding_from_invariant(b_msg, seed=2, action_index=9, repro_actions=["n", "s", "w", "c", "x", "y"])
    c1 = finding_from_invariant(c_msg, seed=3, action_index=3, repro_actions=["q"])
    deduped = dedup_findings([a1, b1, a2, b2, c1])
    # one finding per class, in first-seen order:
    assert [f.category for f in deduped] == ["state_corruption", "synthetic_leak", "crash"]
    # the FIRST occurrence of each class is the survivor (its repro + seed), not a later repeat:
    assert deduped[0].repro_actions == ["n"] and deduped[0].seed == 1
    assert deduped[1].repro_actions == ["n", "s"] and deduped[1].seed == 1


def test_report_assembly_and_gate_with_deterministic_high() -> None:
    """A deterministic state_corruption finding is high → it gates (n_deterministic_high >= 1),
    is numbered BUG-001, and the markdown renders."""
    high = finding_from_invariant(
        "session x: on-disk transcript (5 msgs) is AHEAD of in-memory (3 msgs)",
        seed=42, action_index=17, repro_actions=["new_chat", "send_message"],
    )
    report = build_bug_report(
        [high], explorer_model="test-model", seeds=[42], actions_budget=30, total_actions=30,
    )
    assert report["n_deterministic_high"] == 1
    assert report["findings"][0]["id"] == "BUG-001"
    assert report["findings"][0]["severity"] == "high"
    assert report["oracle_version"]
    assert "BUG-001" in render_markdown(report)


def test_advisory_llm_finding_never_gates() -> None:
    """An LLM-only suspicion (deterministic=False) with a high-looking category must NOT count
    toward the gate — only deterministic findings gate (oracle.md's core rule)."""
    advisory = Finding(
        category="state_corruption",   # would be 'high' by the map…
        title="LLM suspects a stale session",
        oracle="llm_triage",
        deterministic=False,           # …but it's advisory only
        llm_triage="hunch, no invariant fired",
    )
    report = build_bug_report(
        [advisory], explorer_model="m", seeds=[1], actions_budget=10, total_actions=10,
    )
    assert report["n_deterministic_high"] == 0  # advisory does NOT gate
    assert report["findings"][0]["deterministic"] is False


def test_no_findings_note_and_artifact(tmp_path) -> None:
    report = build_bug_report(
        [], explorer_model="m", seeds=[1, 7], actions_budget=30, total_actions=60,
    )
    assert report["findings"] == []
    assert "0 oracle violations" in report["no_findings_note"]
    json_path = write_bug_report(report, tmp_path / "eval")
    assert json_path.exists() and json_path.name.startswith("bughunt-")
    assert (tmp_path / "eval").glob("bughunt-*.md")


def test_action_vocabulary_matches_player() -> None:
    """The selector's action vocabulary must be real ``Player.act_*`` methods (a typo would make
    the LLM choose a non-existent action that the fallback silently masks)."""
    from .app_driver import Player

    for name in ACTION_NAMES:
        assert callable(getattr(Player, name)), name


def test_max_selector_calls_bound() -> None:
    """The worst-case quota is bounded + printable; zero with no provider (fallback)."""
    assert max_selector_calls([1, 7, 42], 30, has_provider=True) == 90
    assert max_selector_calls([1, 7, 42], 30, has_provider=False) == 0


@pytest.mark.skipif(not _BENCH_PRESENT, reason="bench repo not present")
async def test_deterministic_bughunt_finds_nothing(tmp_path) -> None:
    """End-to-end DETERMINISTIC hunt (no ``use_llm`` → seeded-RNG fallback selector) over the REAL
    app: a couple of seeds, a small budget, and assert ZERO oracle violations. This is the
    bug-hunter's always-on baseline — it proves the oracle + driver wiring hold on every push,
    with no quota (the fuzzer already proved these seeds healthy; this drives the SAME machinery
    through the explorer)."""
    from app.main import app

    findings, total = await run_bughunt(
        app, lambda: TestClient(app), tmp_path,
        seeds=[1, 7, 42, 99], actions_budget=12,  # no use_llm → deterministic fallback
    )
    assert total == 48  # 4 seeds * 12 actions, all played
    report = build_bug_report(
        findings, explorer_model="deterministic-fallback", seeds=[1, 7, 42, 99],
        actions_budget=12, total_actions=total,
    )
    assert report["n_deterministic_high"] == 0, f"unexpected findings: {report['findings']}"
    assert findings == [], f"deterministic hunt should find nothing, got: {findings}"
