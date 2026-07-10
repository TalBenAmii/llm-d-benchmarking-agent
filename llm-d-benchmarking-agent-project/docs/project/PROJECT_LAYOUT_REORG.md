# Project layout reorg — execution plan (approved: all four moves)

Status: **approved 2026-07-10 — all four moves, automation-first. Move A (docs/) EXECUTED
2026-07-10 (branch `reorg/docs` → main); move B (knowledge/) EXECUTED 2026-07-10 (branch
`reorg/knowledge` → main, sibling `llm-d-bench-mcp` merged first; knowledge_hash + prompt
bytes verified bit-identical); move C (scripts/) EXECUTED 2026-07-10 (branch `reorg/scripts` →
main; 11 scripts → install/·bridges/·eval/, root run.sh·install_local.sh·_env.sh stayed; sibling
needs no change — it sources the unmoved `scripts/_env.sh`); D pending.** Supersedes the earlier
"proposed, not executed" menu version. Owner decision: do docs/, knowledge/, scripts/ AND
app/tools/, using tools (`git mv` + scripted path rewrites) rather than manual edits.
`app/` proper (other than `app/tools/`) stays untouched. (This doc now lives at
`docs/project/PROJECT_LAYOUT_REORG.md` after move A; §B/C/D below are unchanged and still to run.)

Execution order: **A docs → B knowledge → C scripts → D app/tools** (rising code risk; each
lands on main before the next starts).

## Corrections vs the original proposal (verified against the repo 2026-07-10)

- **`app/tools/__init__.py` is EMPTY (0 bytes).** The claim "the package `__init__.py`
  re-exports, so `registry.py` is insulated" was **false** — `registry.py:18-43` imports 22
  handler modules directly and must be rewritten (mechanical).
- **`tool_loader.py` defines no phase groups.** Groups live in `registry.py:586-608`
  (`_TOOL_GROUPS`: setup/run/analyze/**advanced** + STARTER_KIT; there is no "access" group),
  and several modules span groups (probe, orchestrate, compare). The subpackage layout below is
  **navigational**, not a mirror of the runtime tool groups — don't try to force alignment.
- **CORE knowledge** = exactly `preconditions.md`, `usecase_to_profile.yaml`,
  `conversation_style.md` (`CORE_KNOWLEDGE`, `app/agent/prompt.py:262-266`).
  `welllit_path_advisor.yaml` is **NOT** core (the old ⭐ was wrong).
- The knowledge listing omitted **`readiness_probes.md`** (→ `deploy/`); the scripts grouping
  omitted **`run.sh`**.
- Real blast radius of the app/tools move: **262 import statements** (230 dotted
  `from app.tools.<mod> import …`, 31 `from app.tools import <mod>`, 1 `import app.tools.x as`)
  **+ 41 string literals** (`patch("app.tools.setup.probe.shutil.which")` ×33 etc.) — not "~96".
- **The sibling `llm-d-bench-mcp/` repo is coupled to moves B and D**: it flat-globs
  `knowledge/` (`llm_d_bench_mcp/content.py:35-38`, stem-keyed `doc://knowledge/<stem>`),
  hardcodes playbook basenames (`content.py:167-171`), and imports
  `app.tools.access.knowledge_access` (`content.py:19`). Each of B and D needs a matching sibling
  commit (dep-repo lands first, per the finish loop).
- **`scripts/install_local.sh` is a published curl entry point**
  (`README.md:107` at monorepo root → raw.githubusercontent URL). It — and the canonical
  launcher `run.sh` — **stay at `scripts/` root**.
- 5 of the moved shell scripts self-locate via `dirname "$0"/..`; each needs its root
  resolution bumped one level (exact list in §C).
- Good news found: `provenance.knowledge_hash` hashes `f.name` + bytes only
  (`provenance.py:149`), and the prompt's on-demand index uses `f.stem`/`f.name` — so if all
  globs go recursive **sorted by basename**, both the knowledge hash and the prompt bytes stay
  **unchanged** (no cache re-warm, no re-baseline). This is an acceptance check, not luck.

## Execution rules (every move)

- One move = one branch off main, implemented by one agent, finished via the
  **finish-implementation** loop (commit → review → `--no-ff` merge; the main-only git hook
  runs ruff+pytest at the merge — **never run pytest/ruff manually**; iterate until the hook
  passes). Load the **coding-guidelines** skill before touching code, and
  **manage-context-files** before editing any `CLAUDE.md`.
- Moves use **`git mv`** (preserves the 100755 index modes despite `core.fileMode=false`;
  never delete+re-add).
- Path rewrites are scripted, not manual. Pattern (run from the monorepo root, owned repos
  only — **never** touch `llm-d/`, `llm-d-benchmark/`, `llm-d-skills/`, `.claude/worktrees/`):

  ```bash
  # map.txt: one "OLD<TAB>NEW" pair per line
  while IFS=$'\t' read -r old new; do
    git grep -lzF "$old" -- . ':!llm-d' ':!llm-d-benchmark' ':!llm-d-skills' \
      | xargs -0 -r sed -i "s|$(printf '%s' "$old" | sed 's/[.[\*^$/]/\\&/g')|$new|g"
  done < map.txt
  ```

  For dotted Python module paths use word-boundary sed (`s/app\.tools\.probe\b/…/g` — `\b`
  correctly does NOT match inside `app.tools.setup.probe_parse` because `_` is a word char).
- **Stale-path gate** after each move: `git grep -nE '<old-path-regex>'` across owned repos
  must return zero hits (historical docs under `docs/history/` and `CHANGELOG` get their links
  rewritten too — broken links are worse than archive purity).
- Rewrite the folder map in the project `CLAUDE.md` (+ monorepo-root pointer where affected)
  as part of each move, and the affected folder-level `CLAUDE.md`.
- Push / PR only on explicit user go-ahead. The two memory-dir pointers to
  `docs/reference/PROJECT_BRAIN_REFERENCE.md` (`~/.claude/projects/-home-tal-llm-d-benchmarking-agent/memory/MEMORY.md:38`,
  `llm-d-benchmarking-agent.md:16`) are updated once, in move A.

---

## A. `docs/` (23 top-level → README + 3 groups + existing history/ images/)

```
docs/
├─ README.md      (stays — the index; rewrite its links)
├─ guides/        USER_GUIDE DEPLOYMENT INTERACTIVE_TEST_GUIDE CLUSTER_SERVICE_DEPLOY
│                 GPU_CLUSTER_RUNBOOK TROUBLESHOOTING
├─ reference/     API ARCHITECTURE SECURITY VALIDATION MCP CONTEXT FEATURES
│                 PROJECT_BRAIN_REFERENCE BENCHMARK_FEATURE_COVERAGE UPSTREAM_REUSE_PATHS
│                 USEFUL_REPO_DOCS
├─ project/       CHANGELOG TODO CONTRIBUTING CONFIG_AUDIT_LOG PROJECT_LAYOUT_REORG (this doc)
├─ history/       (unchanged)
└─ images/        (unchanged)
```

**~140 reference sites**, all catalogued; the rewrite is mechanical:

- **Outside docs/ (49)**: monorepo-root `README.md` (15 link lines, path appears twice per
  line), project `CLAUDE.md` (8 lines), project `README.md` (3), `tests/test_ops_docs.py`
  (`DOCS / "SECURITY.md"` → `DOCS / "reference/SECURITY.md"` etc. at :76,88,98,114 — README
  stays), docstrings in `tests/eval/{__init__,bug_report,judge,scorecard}.py` +
  `tests/flows/__init__.py` + `tests/CLAUDE.md:69` + `Makefile:71` (`docs/reference/VALIDATION.md`),
  `scripts/install_local.sh:39` (GPU_CLUSTER_RUNBOOK), `scripts/run_eval_isolated.sh:20`,
  `testing/local-cluster/README.md:29,98`, `knowledge/benchmark_feature_coverage.md:63`,
  `knowledge/packaging.md:31`, `knowledge/useful_repo_docs.md:10,57`.
  (`llm-d-bench-mcp/DESIGN.md:8` points into `docs/history/` — unchanged.)
- **Inside docs/ (89 relative links)**: group-aware rewrite — a link from `docs/README.md` to a
  moved file becomes `guides/X.md`/`reference/X.md`/`project/X.md`; links **between** files in
  different groups become `../<group>/X.md`; same-group links stay bare `X.md`;
  `../CLAUDE.md`/`../../README.md`/`../knowledge/...` links from moved files gain one more
  `../`. `history/plan.md`'s `](../FEATURES.md)` → `](../reference/FEATURES.md)`, etc.
- **Verification**: (1) stale-path grep for `docs/(API|ARCHITECTURE|…all 22…)\.md` = zero hits;
  (2) a throwaway link-checker pass — extract every relative `](…)` target under `docs/` and
  assert the file exists (run ad hoc; don't add it to the repo).
- Update this doc's own Status line (executed) when the move lands.

## B. `knowledge/` (62 runtime files → 10 topic folders; `CLAUDE.md` stays at root)

```
knowledge/
├─ CLAUDE.md      (stays — rewrite its map + gotchas)
├─ conversation/  conversation_style⭐ welcome governance
├─ deploy/        deploy_path_playbook quickstart_playbook gateway_class gateway_readiness
│                 readiness_probes stack_discovery multi_stack autoscaling teardown
│                 resource_management capacity preconditions⭐ accelerators.yaml
│                 infra_providers.yaml infrastructure_preconditions.yaml
├─ run/           orchestrator run_lifecycle model_override harness_debug harness_sizing
│                 collect_only step_select phase_timeouts runconfig_roundtrip cloud_results_sink
├─ workload/      author_spec_workload convert_guide vllm_overrides dataset_replay
│                 conversation_replay shared_prefix_workloads router_features epp_headers.yaml
├─ sweeps/        sweep_playbook sweep_authoring sweep_validity sweep_results sweep_goalseek
├─ analysis/      analysis results_interpretation standard_metrics.yaml multi_harness
│                 benchmark_feature_coverage
├─ observability/ observability observability_grafana observability_monitoring
│                 observability_streaming observability_tracing logging
├─ persistence/   reproducibility history workspace_lifecycle
├─ routing/       usecase_to_profile.yaml⭐ welllit_path_advisor.yaml
└─ reference/     api_trust packaging sim_integration key_docs.yaml useful_repo_docs.md
```
⭐ = CORE (`CORE_KNOWLEDGE`, `app/agent/prompt.py:262-266`) — matched by `f.name`, so CORE
survives the move untouched. Counts: 3+15+10+8+5+5+6+3+2+5 = 62 ✓.

**Code changes — the glob mirror goes recursive in ALL FIVE places at once, with an identical
sort key (`key=lambda p: p.name`) so ordering, prompt bytes, and `knowledge_hash` stay stable:**

1. `app/agent/prompt.py:342` — `glob` → `rglob`, sort by basename (currently sorts by full
   path — switching to basename-sort is what keeps prompt bytes identical).
2. `app/tools/access/knowledge_access.py:206-208` — `_knowledge_files` → `rglob` (already
   basename-sorted). `_match_knowledge_basename` (:211-218) needs no change — it already
   matches by `f.name`/`f.stem` and hard-rejects `/` in requests; stems stay the contract.
3. `app/storage/provenance.py:127-142` — `_KNOWLEDGE_GLOBS` walk → recursive.
4. `llm-d-bench-mcp/llm_d_bench_mcp/content.py:35-38` — recursive; its `{f.stem: f}` index
   (:67) then just works. Fix `_load_playbooks` (:167-171) to resolve via that stem index
   instead of `knowledge_dir / name` joins.
5. `tests/eval/test_playbook_skill_grounding.py:19` — `glob("*.md")` → `rglob` (it would
   otherwise silently scan zero files).

**Direct joins to update (pin new subpaths):** `knowledge_access.py:131`
(`reference/key_docs.yaml`), `knowledge_access.py:471` (`reference/useful_repo_docs.md`),
`app/agent/cards.py:32,39` (`conversation/welcome.md`), `app/validation/report_metrics.py:73`
(`analysis/standard_metrics.yaml`).

**Test joins (~25 files)**: every `knowledge_dir / "<basename>"` /
`parents[…] / "knowledge" / "<basename>"` join gains its group segment — scripted map
`"<basename>"` → `"<group>/<basename>"` applied only to lines matching a knowledge-join
pattern. Also literal strings: `tests/test_epp_headers.py:37-42`
(`knowledge/workload/epp_headers.yaml`), `tests/test_simulate.py:207-210` **and the app code
that emits that prompt note** (grep for `sim_integration`) → `knowledge/reference/sim_integration.md`.
Full catalogue of affected test files: test_welllit_advisor, test_new_tools,
test_run_lifecycle, test_observability, test_tracing_config, test_phase_timeouts,
test_runconfig_roundtrip, test_multi_stack, test_dataset_replay, test_gateway_class,
test_cloud_results_sink, test_results_store, test_collect_only, test_harness_debug,
test_kustomize_block, test_step_select, test_analyze_plots, test_cluster_access,
test_model_override, test_infra_preconditions, test_aggregate_runs, test_qafix_agent,
tests/eval/test_operation_playbook_skill_map (its dict keeps bare basenames — those are
resolver names, leave them), tests/eval/test_key_docs_integrity:19,
tests/eval/test_skill_scenarios_cover_skill_tasks:19.

**Invariants / guards:**
- Basenames AND stems are all globally unique today — **add a small test** asserting recursive
  stem uniqueness (recursion makes future collisions silent otherwise).
- The exclusion set (`CLAUDE.md`, `README.md`) filters by name at every site — keep mirrored.
- `key_docs.yaml`'s `path: quickstart_playbook.md` entry resolves by basename — no change.
- Data/prose that names stems (read_knowledge cues, `app/readiness/probes.py:292,303`,
  registry descriptions) — **untouched** (stem contract preserved; also protects prompt-cache
  byte-stability).
- `Dockerfile:220` `COPY knowledge ./knowledge` is recursive — no change.
- Acceptance: `tests/test_knowledge_meta_excluded.py`, `tests/test_phase_tiered_tools.py`,
  `tests/test_context_mgmt.py` (byte-stability) pass unmodified; `knowledge_hash` for an
  unchanged tree is bit-identical before/after (verifiable in a REPL one-liner).
- Update `knowledge/CLAUDE.md` (map + "grep before renaming" gotcha now includes folders) and
  `app/storage/CLAUDE.md` (the three-way mirror note → five-way, incl. sibling).

## C. `scripts/` (14 → 3 entry points at root + 3 groups)

```
scripts/
├─ run.sh · install_local.sh · _env.sh     (STAY — public entry points + shared source lib;
│                                           install_local.sh is a published curl URL, README.md:107)
├─ install/   install_service.sh install_prereqs.sh install_metrics_server.sh
│             install-git-hooks.sh setup-claude-plan.sh kind_egress_heal.sh
├─ bridges/   aggregate_runs.py capacity_check.py provision_hf_secret.py   ← allowlisted
└─ eval/      validate_flows.py run_eval_isolated.sh
```

**Self-location fixes (moved scripts resolve the project root one level deeper):**
- `install_service.sh:48-49` — `PROJECT_DIR="$SCRIPT_DIR/.."` → `/../..`.
- `setup-claude-plan.sh:23,31` — `cd …/dirname/..` → `/../..` (the `source "scripts/_env.sh"`
  after the cd stays valid since `_env.sh` stays at root; update the `# shellcheck source=` hint).
- `run_eval_isolated.sh:40-42` — `PROJ="$here/.."` → `/../..`; `:123` →
  `scripts/eval/validate_flows.py`.
- `validate_flows.py:30` — `parent.parent` → `parents[2]`.
- `install_local.sh` (stays put) — update its callee paths: `:143` →
  `scripts/install/install_prereqs.sh`, `:193-194` → `scripts/install/setup-claude-plan.sh`.
- Safe as-is: `_env.sh` (root, unchanged — so `run.sh`, `install_local.sh`, and the
  cross-repo `llm-d-bench-mcp/scripts/install.sh:148` sourcing all keep working),
  `install-git-hooks.sh` (uses `git rev-parse`), the three bridges (arg-driven),
  `install_prereqs.sh`, `install_metrics_server.sh`, `kind_egress_heal.sh`.

**Reference updates (exact sites):**
- `security/allowlist.yaml` — the five `script:` values ONLY (:574, :597, :617, :641, :670 →
  `scripts/install/…` / `scripts/bridges/…`). **Entry KEYS are bare filenames = the logical
  argv contract — do not touch them, nor any bare-name mention in `app/agent/prompt.py`,
  `app/tools/registry.py` descriptions, or `knowledge/` files (prompt byte-stability).**
- Monorepo-root `install.sh:73` (install_service), `:184` (install_prereqs), `:407`
  (kind_egress_heal).
- Tests: `test_aggregate_runs.py:41,137` (join + `endswith("scripts/bridges/aggregate_runs.py")`),
  `test_capacity.py:284`, `test_capacity_gated.py:39`, `test_hf_secret.py:38`,
  `test_packaging.py:247`. (`test_menu_helpers.py:18` targets `_env.sh` — unchanged.)
- `Makefile:61,67,75,78,94` (validate_flows / run_eval_isolated),
  `.github/workflows/agent-flow-validation.yml:54`, `.env.example:11`
  (`./scripts/install/setup-claude-plan.sh`) `+ :27`, `ui/app.js:680` (tooltip text),
  `testing/cluster-service-sim/run.sh:42`, `fresh-env/README.md:13,16,35`,
  `tests/CLAUDE.md:23,61-65,85-86`, prose mentions across `docs/` (post-move-A paths) and
  monorepo-root `README.md:113-130,150,343` (NOT :107 — that's install_local.sh, unmoved).
- `git mv` preserves the 100755 modes; while here, fix the three 100644 executables
  (`install-git-hooks.sh`, `run_eval_isolated.sh`, `validate_flows.py`) with
  `git update-index --chmod=+x`.

## D. `app/tools/` (25 modules → 4 subpackages; 5 modules + schemas/ stay at top)

```
app/tools/   __init__.py(stays empty) registry.py context.py command_exec.py tool_loader.py CLAUDE.md
├─ setup/    probe probe_parse catalog repos plan capacity config_artifact convert_guide discover
├─ run/      execute orchestrate manage_runs doe shell gated_access skill_gate
├─ analyze/  analyze compare aggregate_runs report_locate workload_profile history reproducibility
├─ access/   knowledge_access suggest
└─ schemas/  (unchanged)
```
Grouping is **navigational** (primary phase); cross-subpackage imports (`capacity`→`gated_access`,
`plan`→`skill_gate`, `command_exec`→both, `context`→`catalog`, `workload_profile`→`catalog`,
`reproducibility`→`probe`) are absolute imports and stay legal — do not restructure code to
"fix" them. Each new subpackage gets an empty `__init__.py`; the top `__init__.py` **stays
empty — no compat re-export shim** (all callers are rewritten instead; the shim would be
permanent cruft guarding a one-time move).

**Automated rewrite (owned repos: monorepo project + `llm-d-bench-mcp/`):**
1. **Dotted paths** (230 imports + 41 string literals + importlib keys + the one
   `import app.tools.run.execute as`): per moved module `M`→group `G`, run
   `sed -E 's/\bapp\.tools\.M\b/app.tools.G.M/g'` over `git grep -l` hits. This single pass
   fixes `from app.tools.M import …`, `patch("app.tools.M.…")` (probe alone has 36 string
   sites), `tests/test_logging.py:198,217` logger names,
   `tests/test_mcp_import_surface.py:24-26` importlib keys, and all intra-package absolute
   imports. Apply the map file-path variant (`app/tools/M.py` → `app/tools/G/M.py`) to the 88
   doc/knowledge/comment mentions (incl. `security/allowlist.yaml:632,719` comments,
   `knowledge/step_select.md:4`, `knowledge/phase_timeouts.md:118`,
   `knowledge/infra_providers.yaml:8`, `knowledge/governance.md:7`, `docs/reference/API.md:8` link).
2. **Package-attr style** (31 lines of `from app.tools import a, b as c, …` — incl.
   `registry.py:18-43`'s 22-module block and `tests/conftest.py:77`): a small throwaway Python
   codemod — parse each such line, map every name through the module→group table, emit grouped
   `from app.tools.<G> import …` lines (names without a group — `context`, `tool_loader`,
   `schemas` — stay on an `from app.tools import …` line). Run it, delete it (don't commit the
   codemod).
3. **Sibling repo**: `llm-d-bench-mcp/llm_d_bench_mcp/content.py:19` →
   `from app.tools.access.knowledge_access import …`; its `server.py:21-22`/`adapters.py:18`
   import `context`/`registry` (unmoved — no change); update its tests
   (`tests/conftest.py:29`, `tests/test_mcp_server.py:15-16`) as the dotted pass dictates.
4. **Docs/maps**: rewrite `app/tools/CLAUDE.md` (the authoritative file map),
   `app/observability/CLAUDE.md:21` (`app.tools.run.manage_runs._parse_top_table`),
   `knowledge/CLAUDE.md:15,41` pointers, project `CLAUDE.md` tree.

**Verification:** stale-path gate `git grep -nE 'app\.tools\.(probe|execute|…all 25…)\b'`
and `app/tools/(probe|…)\.py` = zero hits in both owned repos; the merge hook's ruff+pytest
must pass in the monorepo AND `llm-d-bench-mcp`'s own gate for its repo.

---

## Cross-cutting

- After all four land: one final sweep commit updating the project `CLAUDE.md` map (if any
  drift accumulated), `docs/reference/FEATURES.md` / `docs/reference/PROJECT_BRAIN_REFERENCE.md`
  status lines, and a dated entry in `docs/project/CONFIG_AUDIT_LOG.md`.
- Sibling-repo commits (moves B, D) land on `llm-d-bench-mcp`'s own main **before** the
  monorepo merge that depends on them.
- No pushes / PRs anywhere without an explicit user go-ahead.
