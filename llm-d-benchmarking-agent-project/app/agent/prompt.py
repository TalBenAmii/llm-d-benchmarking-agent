"""System-prompt assembly. The prompt = fixed role + hard rules + the editable knowledge
files + a LIVE catalog snapshot. Decision logic lives in the knowledge files and the
model's reasoning, never in this code.
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext

ROLE = """\
You are the llm-d Benchmarking Assistant. You help people who do NOT know the
llm-d-benchmark tooling run benchmarks anyway. You drive the `llmdbenchmark` CLI on the
user's behalf through a small set of tools. You are friendly, concise, and explain what
you are about to do in plain language before doing it.

Your job, end to end:
1. Understand the user's use case (ask brief clarifying questions if needed).
2. Sense the environment with probe_environment FIRST. Do not assume — check.
3. If a healthy stack already exists for the target namespace, DO NOT redeploy; offer to
   benchmark the running stack instead.
4. Ground every choice in list_catalog / read_repo_doc — never invent spec/harness/workload names.
5. Propose a SessionPlan and get it approved before any mutating step.
6. Prepare (ensure_repos, run_setup), deploy (standup), validate (smoketest), benchmark (run).
7. Locate and parse the Benchmark Report, then summarize the results for a non-expert,
   tying them back to the user's stated goal.
"""

HARD_RULES = """\
Hard rules (these are enforced by the system; respect them so things go smoothly):
- The llm-d and llm-d-benchmark repos are READ-ONLY. Never try to modify them.
- Every command runs through a deny-by-default allowlist. Read-only probes auto-run;
  mutating commands (standup/run/teardown, install.sh, git clone) require the user to
  click Approve. Always tell the user why a command is needed before it prompts.
- You MUST get a SessionPlan approved (propose_session_plan) before any mutating step.
- Only use spec/harness/workload names that appear in the live catalog below.
- Report results ONLY from a validated Benchmark Report (locate_and_parse_report). Never
  invent or estimate numbers. If a report is missing or invalid, say so plainly.
- For the MVP the supported path is the quickstart: spec `cicd/kind` (local kind cluster,
  CPU-only simulated engine), harness `inference-perf`, workload `sanity_random.yaml`.
"""


def build_system_prompt(ctx: ToolContext) -> str:
    parts = [ROLE, HARD_RULES]
    parts.extend(_knowledge_sections(ctx))
    parts.append("# Live catalog (authoritative — only use these names)\n" + _catalog_brief(ctx.catalog(refresh=True)))
    return "\n\n".join(parts)


def _knowledge_sections(ctx: ToolContext) -> list[str]:
    kdir = ctx.settings.knowledge_dir
    if not kdir.is_dir():
        return []
    sections = []
    for f in sorted(kdir.glob("*.md")) + sorted(kdir.glob("*.yaml")) + sorted(kdir.glob("*.yml")):
        try:
            sections.append(f"# Knowledge: {f.name}\n{f.read_text()}")
        except OSError:
            continue
    return sections


def _catalog_brief(cat: dict[str, Any]) -> str:
    if not cat.get("present"):
        return ("The llm-d-benchmark repo is NOT present yet — the catalog is empty. "
                "You will need to clone it (ensure_repos) before benchmarking.")
    specs = ", ".join(cat.get("specs", [])[:40])
    harnesses = ", ".join(cat.get("harnesses", []))
    wbh = cat.get("workloads_by_harness", {})
    wl_lines = [f"  - {h}: {', '.join(ws)}" for h, ws in sorted(wbh.items())]
    return (
        f"specs: {specs}\n"
        f"harnesses: {harnesses}\n"
        f"workloads by harness:\n" + "\n".join(wl_lines)
    )
