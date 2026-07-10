"""(B) Bug report — pure finding assembly / dedup + artifact writer. NO LLM, NO quota.

Assembles the bug-hunter's :class:`Finding`s (each a DETERMINISTIC oracle hit, optionally
annotated with the explorer's advisory ``llm_triage``) into a reviewable report, deduping a
recurring class so one repeated invariant doesn't spam the report. The severity map + oracle
policy live in ``oracle.md`` (data); this module is mechanism only.

Only a deterministic finding with ``severity >= high`` may fail a build — the LLM triage is
advisory and never flips a build red (see ``oracle.md`` + ``test_bughunt_live.py``).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ORACLE_PATH = Path(__file__).resolve().parent / "oracle.md"

# Ordering for "is this >= high?" comparisons. Mechanism — the category→severity MAPPING is the
# oracle asset's judgment; this is just the comparable ranking of the named levels.
_SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Category → severity, mirroring oracle.md's severity map. Kept here as the machine contract the
# deterministic oracle emits against; oracle.md is the human-readable source of truth.
SEVERITY_BY_CATEGORY = {
    "state_corruption": "high",
    "crash": "high",
    "5xx": "high",
    "contract": "medium",
    "synthetic_leak": "medium",
}
DEFAULT_SEVERITY = "info"


def oracle_version(path: Path = ORACLE_PATH) -> str:
    """Pull the ``version:`` from the oracle asset's front matter (loud failure if absent)."""
    text = path.read_text()
    m = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
    if not m:
        raise ValueError(f"oracle {path} is missing a 'version:'")
    return m.group(1)


def severity_for(category: str) -> str:
    return SEVERITY_BY_CATEGORY.get(category, DEFAULT_SEVERITY)


def severity_ge(severity: str, floor: str) -> bool:
    """True if ``severity`` ranks at or above ``floor`` (e.g. ``severity_ge('high', 'high')``)."""
    return _SEVERITY_ORDER.get(severity, 0) >= _SEVERITY_ORDER.get(floor, 0)


@dataclass
class Finding:
    """One bug-hunter finding. ``deterministic`` distinguishes a real oracle hit (can gate a
    build) from an LLM-only suspicion (advisory). ``invariant`` is the human-readable violation
    string the invariant battery returned; ``category`` drives the severity map."""

    category: str
    title: str
    oracle: str                      # which oracle produced it (e.g. "session_invariant")
    deterministic: bool = True
    seed: int | None = None
    action_index: int | None = None
    repro_actions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    llm_triage: str = ""             # advisory; never gates

    @property
    def severity(self) -> str:
        return severity_for(self.category)

    def dedup_key(self) -> tuple[str, str, str]:
        """One recurring class (same category + invariant + severity) collapses to one finding."""
        return (self.category, self.evidence.get("invariant", self.title), self.severity)


def categorize_invariant(problem: str) -> str:
    """Map an invariant battery violation STRING to an oracle category. Pure string inspection of
    the proven invariant messages emitted by ``app_driver`` — deterministic, no judgment."""
    p = problem.lower()
    if "ahead of" in p or "diverges" in p or "duplicate in_flight" in p \
            or "shared across sessions" in p or "not persisted" in p or "not re-emitted" in p \
            or "not cleared" in p or "not recorded" in p:
        return "state_corruption"
    if "synthetic" in p or "leaked" in p:
        return "synthetic_leak"
    if "returned 5" in p or "server error" in p or "500" in p:
        return "crash"
    if "protocol_error" in p or "handshake" in p or "did not get a pong" in p \
            or "not rejected" in p:
        return "contract"
    return "contract"


def finding_from_invariant(
    problem: str, *, seed: int, action_index: int, repro_actions: list[str],
    oracle: str = "deterministic_invariant",
) -> Finding:
    """Build a deterministic :class:`Finding` from one invariant-battery violation string."""
    category = categorize_invariant(problem)
    return Finding(
        category=category,
        title=problem,
        oracle=oracle,
        deterministic=True,
        seed=seed,
        action_index=action_index,
        repro_actions=list(repro_actions),
        evidence={"invariant": problem},
    )


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Collapse repeats of one class (by ``dedup_key``), keeping the FIRST occurrence (its
    ``repro_actions`` is the shortest reproduction seen). Order-stable."""
    seen: dict[tuple[str, str, str], Finding] = {}
    out: list[Finding] = []
    for f in findings:
        key = f.dedup_key()
        if key in seen:
            continue
        seen[key] = f
        out.append(f)
    return out


def build_bug_report(
    findings: list[Finding],
    *,
    explorer_model: str,
    seeds: list[int],
    actions_budget: int,
    total_actions: int,
    no_findings_note: str = "",
) -> dict[str, Any]:
    """Assemble the bug-report dict (see ``docs/reference/VALIDATION.md`` for the shape). Pure: dedups,
    numbers the findings BUG-NNN, and records the run's provenance (oracle version, model, seeds,
    budget)."""
    deduped = dedup_findings(findings)
    det_high = [f for f in deduped if f.deterministic and severity_ge(f.severity, "high")]
    note = no_findings_note or (
        f"{len(deduped)} finding(s); {len(det_high)} deterministic >= high."
        if deduped else "0 oracle violations."
    )
    return {
        "oracle_version": oracle_version(),
        "explorer_model": explorer_model,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seeds": list(seeds),
        "actions_budget": actions_budget,
        "total_actions": total_actions,
        "n_deterministic_high": len(det_high),
        "findings": [
            {
                "id": f"BUG-{i + 1:03d}",
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "oracle": f.oracle,
                "deterministic": f.deterministic,
                "seed": f.seed,
                "action_index": f.action_index,
                "repro_actions": f.repro_actions,
                "evidence": f.evidence,
                "llm_triage": f.llm_triage,
            }
            for i, f in enumerate(deduped)
        ],
        "no_findings_note": note,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Compact human-readable render of a bug report (the ``.md`` artifact)."""
    lines = [
        f"# Exploratory bug-hunt report (oracle v{report['oracle_version']})",
        "",
        f"- explorer model: `{report['explorer_model']}`",
        f"- generated: {report['generated_at']}",
        f"- seeds: {report['seeds']} · budget/seed: {report['actions_budget']} · "
        f"total actions: {report['total_actions']}",
        f"- deterministic >= high findings: **{report['n_deterministic_high']}**",
        f"- {report['no_findings_note']}",
        "",
    ]
    if report["findings"]:
        lines += ["## Findings", "", "| id | severity | category | title | det? |", "|---|---|---|---|---|"]
        for f in report["findings"]:
            title = (f["title"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {f['id']} | {f['severity']} | {f['category']} | {title} | "
                f"{'yes' if f['deterministic'] else 'no (advisory)'} |"
            )
    else:
        lines.append("No findings. The deterministic oracle saw no invariant violations.")
    return "\n".join(lines) + "\n"


def write_bug_report(report: dict[str, Any], eval_dir: Path) -> Path:
    """Write ``bughunt-<ts>.json`` + ``.md`` under ``eval_dir`` (gitignored ``workspace/eval/``)."""
    eval_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    json_path = eval_dir / f"bughunt-{ts}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (eval_dir / f"bughunt-{ts}.md").write_text(render_markdown(report))
    return json_path
