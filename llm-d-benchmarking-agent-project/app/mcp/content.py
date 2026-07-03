"""The MCP knowledge-exposure surface — three formerly-separate modules stitched together:

- ``knowledge/`` files exposed as ``doc://knowledge/<stem>`` MCP resources (+ a traversal guard),
- workflow prompts that inject the relevant ``knowledge/`` playbooks as MCP prompts,
- the server-level ``instructions`` string advertised at MCP initialize.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

from app.agent.prompt import _one_line_purpose
from app.tools.knowledge_access import EXCLUDED_KNOWLEDGE_FILES

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server


# --- resources — expose every ``knowledge/`` file as an MCP resource ----------------------------
# Expose every ``knowledge/`` file as an MCP resource under ``doc://knowledge/<stem>``.
#
# Source of truth is the same glob the system-prompt builder uses (``app/agent/prompt.py``):
# ``knowledge/*.md|*.yaml|*.yml`` minus ``EXCLUDED_KNOWLEDGE_FILES``. The module-level functions are
# pure and unit-testable; ``register_resources`` is the thin decorator wiring.
_SCHEME = "doc"
_HOST = "knowledge"


def _knowledge_files(knowledge_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.md", "*.yaml", "*.yml"):
        files.extend(knowledge_dir.glob(pattern))
    files = [f for f in files if f.name not in EXCLUDED_KNOWLEDGE_FILES]
    return sorted(files, key=lambda p: p.stem)


def _mime(path: Path) -> str:
    return "text/markdown" if path.suffix == ".md" else "application/yaml"


def _stem_of_uri(uri: object) -> str:
    """Last path segment of a ``doc://knowledge/<stem>`` URI. String-parsed so it works whether the
    SDK hands us a ``pydantic.AnyUrl`` or a plain ``str``."""
    return str(uri).rstrip("/").rsplit("/", 1)[-1]


def list_resource_objects(knowledge_dir: Path) -> list[types.Resource]:
    return [
        types.Resource(
            uri=AnyUrl(f"{_SCHEME}://{_HOST}/{f.stem}"),
            name=f.stem,
            description=_one_line_purpose(f),
            mimeType=_mime(f),
        )
        for f in _knowledge_files(knowledge_dir)
    ]


def read_resource_contents(knowledge_dir: Path, uri: object) -> list[ReadResourceContents]:
    stem = _stem_of_uri(uri)
    index = {f.stem: f for f in _knowledge_files(knowledge_dir)}  # whitelist → no path traversal
    path = index.get(stem)
    if path is None:
        raise ValueError(f"unknown resource: {uri}")
    return [ReadResourceContents(content=path.read_text(encoding="utf-8"), mime_type=_mime(path))]


def register_resources(server: Server, knowledge_dir: Path) -> None:
    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return list_resource_objects(knowledge_dir)

    @server.read_resource()
    async def read_resource(uri: object) -> list[ReadResourceContents]:
        return read_resource_contents(knowledge_dir, uri)


# --- prompts — workflow prompts that inject the relevant ``knowledge/`` playbooks --------------
# Workflow prompts: a small set of user-invokable templates that inject the relevant ``knowledge/``
# playbook plus the workflow directive, so a connecting agent runs the benchmark the way this agent
# would. The substance is loaded from ``knowledge/`` at call time (never duplicated here); only the
# short directive lines are new prose.
#
# Module-level functions are pure/unit-testable; ``register_prompts`` is the thin decorator wiring.
@dataclass(frozen=True)
class _PromptSpec:
    name: str
    description: str
    arguments: tuple[tuple[str, str, bool], ...]  # (name, description, required)
    playbooks: tuple[str, ...]
    directive: str


_PROMPTS: tuple[_PromptSpec, ...] = (
    _PromptSpec(
        name="benchmark_this_model",
        description="Run a full llm-d benchmark for one model: interview, plan, run, explain.",
        arguments=(
            ("model", "Model to benchmark (HF id or served name)", False),
            ("goal", "What the user cares about: latency, throughput, or cost", False),
            ("slo", "Any SLO target, e.g. 'p95 TTFT < 2s'", False),
        ),
        playbooks=("quickstart_playbook.md", "welllit_path_advisor.yaml"),
        directive=(
            "Drive a full llm-d benchmark for model={model}, goal={goal}, slo={slo}.\n"
            "Workflow: probe_environment first; ground in list_catalog and the doc://knowledge/* "
            "resources; propose_session_plan and get it approved; check_capacity; deploy; run; then "
            "locate_and_parse_report and explain the result for a non-expert. Use the playbooks below."
        ),
    ),
    _PromptSpec(
        name="pick_deploy_path",
        description="Choose the right deploy path (kind_sim / guide / gpu) for a model and accelerator.",
        arguments=(
            ("model", "Model under consideration", False),
            ("accelerator", "Accelerator available, e.g. A100 / H100 / none", False),
        ),
        playbooks=("deploy_path_playbook.md", "welllit_path_advisor.yaml"),
        directive=(
            "Recommend a deploy path for model={model} on accelerator={accelerator}. Weigh the "
            "trade-offs using the playbooks below, then state the choice and why."
        ),
    ),
    _PromptSpec(
        name="interpret_this_report",
        description="Interpret a Benchmark Report and give an SLO verdict for a non-expert.",
        arguments=(("report_path", "Path to the run's report, if known", False),),
        playbooks=("results_interpretation.md", "analysis.md"),
        directive=(
            "Interpret the benchmark report (report_path={report_path}). Use locate_and_parse_report "
            "and analyze_results, then explain the numbers and the SLO verdict in plain language, "
            "guided by the playbooks below. Never scrape numbers from raw logs."
        ),
    ),
    _PromptSpec(
        name="design_a_sweep",
        description="Design a parameter sweep / DoE toward an objective.",
        arguments=(("objective", "What the sweep should optimize or explore", False),),
        playbooks=("sweep_playbook.md",),
        directive=(
            "Design a benchmark sweep for objective={objective}. Use generate_doe_experiment and "
            "orchestrate_sweep, following the playbook below to pick factors and levels."
        ),
    ),
    _PromptSpec(
        name="goal_seek_to_slo",
        description="Iteratively sweep the config space to hit an SLO at best goodput.",
        arguments=(("slo", "The SLO target to hit, e.g. 'p95 TTFT < 2s'", True),),
        playbooks=("sweep_playbook.md",),
        directive=(
            "Goal-seek toward slo={slo}: run iterative DoE sweep rounds (generate_doe_experiment / "
            "orchestrate_sweep) and narrow the factor ranges around analyze_results' SLO-feasible "
            "frontier each round, following the goal-seeking section of the playbook below."
        ),
    ),
)

_BY_NAME = {p.name: p for p in _PROMPTS}


def _load_playbooks(knowledge_dir: Path, names: tuple[str, ...]) -> str:
    chunks: list[str] = []
    for name in names:
        try:
            body = (knowledge_dir / name).read_text(encoding="utf-8")
        except OSError:
            continue
        chunks.append(f"## {name}\n\n{body}")
    return "\n\n".join(chunks)


def list_prompt_objects() -> list[types.Prompt]:
    return [
        types.Prompt(
            name=p.name,
            description=p.description,
            arguments=[
                types.PromptArgument(name=n, description=d, required=r) for (n, d, r) in p.arguments
            ],
        )
        for p in _PROMPTS
    ]


def build_prompt_result(
    knowledge_dir: Path, name: str, arguments: dict[str, str] | None = None
) -> types.GetPromptResult:
    spec = _BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"unknown prompt: {name}")
    values = {n: (arguments or {}).get(n, "") for (n, _d, _r) in spec.arguments}
    body = spec.directive.format(**values)
    playbooks = _load_playbooks(knowledge_dir, spec.playbooks)
    text = f"{body}\n\n---\nReference playbooks (from knowledge/):\n\n{playbooks}"
    return types.GetPromptResult(
        description=spec.description,
        messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))],
    )


def register_prompts(server: Server, knowledge_dir: Path) -> None:
    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return list_prompt_objects()

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
        return build_prompt_result(knowledge_dir, name, arguments)


# --- instructions — the server-level ``instructions`` advertised at MCP initialize -------------
# Many clients fold this into their system prompt, so even a client that never reads a
# ``doc://knowledge/*`` resource inherits the basic "how this agent behaves" shape. This is a
# trimmed restatement of ``app/agent/prompt.py::ROLE`` with the web-UI specifics (welcome card,
# synthetic pre-probe messages, sidebar) removed — the substance of the judgment still lives in
# ``knowledge/`` and is delivered as resources/prompts, never duplicated here.
INSTRUCTIONS = """\
You are the llm-d Benchmarking Assistant, exposed over MCP. You help people who do NOT know the
llm-d-benchmark tooling run benchmarks anyway, by driving the tools on this server on their behalf.
Be friendly and concise, and explain what you are about to do in plain language before doing it.

Workflow, end to end:
1. Understand the use case (ask brief clarifying questions if needed).
2. Sense the environment with probe_environment FIRST. Do not assume, check.
3. Ground yourself in the real procedure before planning: read the doc://knowledge/* resources and
   use list_catalog / fetch_key_docs. Never invent spec / harness / workload names or steps.
4. If a healthy stack already serves the target namespace, do NOT redeploy, offer to benchmark the
   running stack instead.
5. Propose a SessionPlan (propose_session_plan) and get it approved before any mutating step, then
   run a capacity pre-flight (check_capacity) to confirm the plan fits before deploying.
6. Deploy (standup), validate, then benchmark (run) via execute_llmdbenchmark or the orchestrator.
7. Locate and parse the Benchmark Report, then explain the results for a non-expert from the
   validated report, tying them to the user's goal. Never scrape numbers from raw logs.

Read the doc://knowledge/* resources for judgment: which spec/harness/workload to choose, deploy-path
selection, capacity sizing, and how to read SLO verdicts. Mutations are gated: the user approves each
tool call in your client, so always say what a step will do before you call it.
"""
