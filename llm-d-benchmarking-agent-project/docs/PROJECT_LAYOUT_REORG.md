# Project layout reorg: proposal (open decision)

Status: proposed, not executed. A menu of folder-regrouping moves, ranked by value-vs-cost.
Pick a subset to apply; each is an independent worktree with loader/path/test updates and a green
suite before merge. `app/` proper is already cleanly subpackaged and is left untouched; the
mess is concentrated in four flat folders.

## The problem
Four folders hold too many files with no internal grouping:

| Folder | Files | Symptom |
|---|---|---|
| `knowledge/` | 63 | flat wall of `.md`/`.yaml`; no topic structure |
| `app/tools/` | 31 | setup / run / analyze / security tools all mixed at one level |
| `docs/` | 24 | guides, reference, and project-meta docs intermixed |
| `scripts/` | 14 | installers, path-coupled bridges, and eval runners intermixed |

## Ranked recommendation (value ÷ cost)

| Order | Move | Benefit | Cost / risk | Verdict |
|---|---|---|---|---|
| 1 | `docs/` regroup | high clarity | wide but mechanical path-ref churn; no code risk | do first |
| 2 | `knowledge/` regroup | biggest navigation win (63→~10) | switch mirrored globs to `rglob`; one-time hash/cache re-baseline | do |
| 3 | `scripts/` regroup | low | edits `security/allowlist.yaml` (path-coupled bridges) | optional |
| 4 | `app/tools/` subpackages | low (pkg already flattens) | ~96 test imports break | skip / last |

---

## 1. `docs/` (24 → 3 groups + existing `history/`)
```
docs/
├─ README.md
├─ guides/     USER_GUIDE DEPLOYMENT INTERACTIVE_TEST_GUIDE CLUSTER_SERVICE_DEPLOY
│              GPU_CLUSTER_RUNBOOK TROUBLESHOOTING
├─ reference/  API ARCHITECTURE SECURITY VALIDATION MCP CONTEXT FEATURES
│              PROJECT_BRAIN_REFERENCE BENCHMARK_FEATURE_COVERAGE UPSTREAM_REUSE_PATHS USEFUL_REPO_DOCS
├─ project/    CHANGELOG TODO CONTRIBUTING CONFIG_AUDIT_LOG
└─ history/    (unchanged; proposal archive)
```
Touches: every `docs/<X>.md` path reference: the root + project `CLAUDE.md` maps (10+ refs), the
`coding-guidelines` / `finish-implementation` skills, memory pointers, and cross-doc links. No code,
no import risk; purely mechanical.

## 2. `knowledge/` (63 → ~10 topic folders)
```
knowledge/
├─ CLAUDE.md
├─ conversation/   conversation_style⭐ welcome governance
├─ deploy/         deploy_path_playbook quickstart_playbook gateway_class gateway_readiness
│                  stack_discovery multi_stack autoscaling teardown resource_management
│                  capacity preconditions⭐ accelerators.yaml infra_providers.yaml
│                  infrastructure_preconditions.yaml
├─ run/            orchestrator run_lifecycle model_override harness_debug harness_sizing
│                  collect_only step_select phase_timeouts runconfig_roundtrip cloud_results_sink
├─ workload/       author_spec_workload convert_guide vllm_overrides dataset_replay
│                  conversation_replay shared_prefix_workloads router_features epp_headers.yaml
├─ sweeps/         sweep_playbook sweep_authoring sweep_validity sweep_results sweep_goalseek
├─ analysis/       analysis results_interpretation standard_metrics.yaml multi_harness
│                  benchmark_feature_coverage
├─ observability/  observability observability_grafana observability_monitoring
│                  observability_streaming observability_tracing logging
├─ persistence/    reproducibility history workspace_lifecycle
├─ routing/        usecase_to_profile.yaml⭐ welllit_path_advisor.yaml⭐
└─ reference/      api_trust packaging sim_integration key_docs.yaml useful_repo_docs.md
```
⭐ = CORE (always assembled into the prompt). Confirm exact membership against `knowledge/CLAUDE.md`
before moving; leaf assignments above are a first cut, refine at implementation.

Touches (hard invariant: the two globs must stay identical):
- `app/tools/knowledge_access.py:206`: the flat `glob("*.md"|"*.yaml"|"*.yml")` → `rglob`.
- `app/storage/provenance.py:128`: the mirrored knowledge-hash glob → `rglob` (must equal the above).
- `app/agent/prompt.py`: reuses the same set to build the brain.
- `read_knowledge("name")` resolves by stem (`_match_knowledge_basename`), so name-based lookups
  survive a move only if the file enumeration recurses. Basenames must stay globally unique.

One-time side effects (harmless): `provenance.knowledge_hash` re-baselines; the prompt-cache
prefix re-warms once.

## 3. `scripts/` (14 → 3 groups)
```
scripts/
├─ install/   install_local install_service install_prereqs install_metrics_server
│             install-git-hooks setup-claude-plan kind_egress_heal _env.sh
├─ bridges/   aggregate_runs.py capacity_check.py provision_hf_secret.py   ← path-coupled
└─ eval/      validate_flows.py run_eval_isolated.sh
```
Touches: the `bridges/` scripts are invoked through the allowlist, so moving them edits
`security/allowlist.yaml` (and the docstrings in `app/capacity/__init__.py`, `app/tools/aggregate_runs.py`,
`app/tools/execute.py` that name their paths). Low navigation benefit; do only if the other moves land.

## 4. `app/tools/` (31 → phase subpackages), mirrors `tool_loader.py`
```
app/tools/  __init__.py registry.py context.py command_exec.py tool_loader.py
├─ setup/    probe probe_parse catalog repos plan capacity config_artifact convert_guide discover
├─ run/      execute orchestrate manage_runs doe shell gated_access skill_gate
├─ analyze/  analyze compare aggregate_runs report_locate workload_profile history reproducibility
├─ access/   knowledge_access suggest
└─ schemas/  (unchanged)
```
Touches: `registry.py` imports `from app.tools import (...)`; the package `__init__.py` re-exports,
so `registry.py` is insulated. But ~96 test import sites reference `app.tools.<module>` directly
and break unless updated (or shim modules are left behind). Benefit is low: the package already
flattens the surface and `tool_loader.py` already groups tools by phase logically. Recommend
skipping or doing last.

---

## Cross-cutting (every move)
- Rewrite the folder map in the project `CLAUDE.md` (and the monorepo-root pointer where affected).
- Keep each move on its own worktree; gate on the hermetic pytest suite + ruff before `--no-ff` merge.
- Push / PR only on explicit go-ahead.
