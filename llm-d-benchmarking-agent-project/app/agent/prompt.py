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
3. Ground yourself in the real procedure with fetch_key_docs (and list_catalog) before
   planning a deploy — never invent spec/harness/workload names or steps.
4. If a healthy stack already exists for the target namespace, DO NOT redeploy; offer to
   benchmark the running stack instead.
5. Propose a SessionPlan and get it approved before any mutating step. Then run a capacity
   pre-flight (check_capacity) to confirm the plan will fit BEFORE deploying — especially
   when the user wants a non-default model, longer context, or a real GPU. If it comes back
   infeasible, do not stand up; explain why and adjust (see knowledge/capacity.md).
6. Prepare: if probe shows Docker or the kind binary missing, offer to install them with
   run_command(["install_prereqs.sh", …]); then ensure_repos and run_setup. If the
   quickstart needs a local kind cluster and none exists, create it yourself with
   run_command (kind create cluster).
7. Deploy (standup), validate (smoketest), benchmark (run).
8. Locate and parse the Benchmark Report, then summarize the results for a non-expert,
   tying them back to the user's stated goal.
"""

HARD_RULES = """\
Hard rules (these are enforced by the system; respect them so things go smoothly):
- The llm-d and llm-d-benchmark repos are READ-ONLY. Never try to modify them.
- Every command runs through a deny-by-default allowlist. Read-only probes auto-run;
  mutating commands (standup/run/teardown, install.sh, install_prereqs.sh, git clone,
  kind create/delete) require the user to click Approve. Always tell the user why a
  command is needed before it prompts.
- Before proposing a deployment SessionPlan you MUST call fetch_key_docs (task="quickstart"
  for the kind path) and follow the real flow/flags it returns — do not rely on memory.
- You MUST get a SessionPlan approved (propose_session_plan) before any mutating step.
- The kind cluster is yours to manage: if probe_environment shows no kind cluster, create
  it with run_command(["kind","create","cluster","--name","llmd-quickstart"]) (mutating —
  it will prompt).
- You CAN install the prerequisites that install.sh does not (the Docker daemon and the
  kind binary): if probe shows them missing, install them with
  run_command(["install_prereqs.sh","--docker","--kind"]) (or "--all"). It is mutating
  (prompts) and needs root or passwordless sudo — if it reports it cannot get privileges,
  or that the Docker daemon could not be auto-started (common on WSL), relay that to the
  user. run_setup (install.sh) still handles kubectl/helm/helmfile/jq/yq/etc.
- Only use spec/harness/workload names that appear in the live catalog below.
- Report results ONLY from a validated Benchmark Report (locate_and_parse_report). Never
  invent or estimate numbers. If a report is missing or invalid, say so plainly.
- For the MVP the supported path is the quickstart: spec `cicd/kind` (local kind cluster,
  CPU-only simulated engine), harness `inference-perf`, workload `sanity_random.yaml`.
"""


# Knowledge partition. CORE files are inlined into every system prompt — they cover the
# phases the model reaches BEFORE it would know to ask for a specific guide (interview /
# plan / deploy / basic results). The rest are interpretation guides tied to a specific
# later-phase tool; they are listed in a compact INDEX and the model pulls the one it needs
# with read_knowledge("<topic>") when that tool's description points at it. This is
# mechanism only — the "what to load when" lives in the index text and the tool
# descriptions (the agent's reasoning), not in any decision branch here.
CORE_KNOWLEDGE = (
    "glossary.md",
    "preconditions.md",
    "deploy_path_playbook.md",
    "usecase_to_profile.yaml",
    "quickstart_playbook.md",
    "results_interpretation.md",
    "key_docs.yaml",
)


def build_system_prompt(ctx: ToolContext) -> str:
    parts = [ROLE, HARD_RULES]
    parts.extend(_knowledge_sections(ctx))
    parts.append("# Live catalog (authoritative — only use these names)\n" + _catalog_brief(ctx.catalog(refresh=True)))
    return "\n\n".join(parts)


def _knowledge_sections(ctx: ToolContext) -> list[str]:
    kdir = ctx.settings.knowledge_dir
    if not kdir.is_dir():
        return []
    all_files = sorted(kdir.glob("*.md")) + sorted(kdir.glob("*.yaml")) + sorted(kdir.glob("*.yml"))
    core_set = set(CORE_KNOWLEDGE)

    sections: list[str] = []
    # (a) Inline the CORE guides, in the declared CORE_KNOWLEDGE order (with any other file
    # that happens to be marked core appended), so the always-needed material is verbatim.
    inlined: set[str] = set()
    ordered_core = [f for n in CORE_KNOWLEDGE for f in all_files if f.name == n]
    ordered_core += [f for f in all_files if f.name in core_set and f not in ordered_core]
    for f in ordered_core:
        try:
            sections.append(f"# Knowledge: {f.name}\n{f.read_text()}")
            inlined.add(f.name)
        except OSError:
            continue

    # (b) Index the rest (on-demand). Each line = topic + filename + one-line purpose
    # (its first heading). The model loads the relevant guide with read_knowledge("<topic>")
    # BEFORE interpreting that kind of result — the tool descriptions point here.
    index_lines: list[str] = []
    for f in all_files:
        if f.name in inlined:
            continue
        index_lines.append(f"- {f.stem} ({f.name}) — {_one_line_purpose(f)}")
    if index_lines:
        sections.append(
            "# Knowledge index (on-demand — load with read_knowledge(\"<topic>\"))\n"
            "These deeper guides are NOT inlined to save space. Each is tied to a specific "
            "later-phase tool (that tool's description tells you when to consult it). BEFORE "
            "you interpret that kind of result or make that decision, call "
            "read_knowledge(\"<topic>\") to load the full guide — do not act on memory.\n"
            + "\n".join(index_lines)
        )
    return sections


def _one_line_purpose(f) -> str:
    """The file's first non-empty line (its heading), stripped of leading '#', as a
    one-line purpose for the on-demand index."""
    try:
        for line in f.read_text().splitlines():
            s = line.strip()
            if s:
                return s.lstrip("#").strip()
    except OSError:
        pass
    return f.stem


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
