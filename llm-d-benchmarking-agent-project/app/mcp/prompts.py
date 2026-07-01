"""Workflow prompts: a small set of user-invokable templates that inject the relevant ``knowledge/``
playbook plus the workflow directive, so a connecting agent runs the benchmark the way this agent
would. The substance is loaded from ``knowledge/`` at call time (never duplicated here); only the
short directive lines are new prose.

Module-level functions are pure/unit-testable; ``register_prompts`` is the thin decorator wiring.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import mcp.types as types

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server


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
