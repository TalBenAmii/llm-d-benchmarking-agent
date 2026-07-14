# knowledge/ — the agent's editable brain (judgment, not mechanism)

These 62 markdown/yaml files hold ALL judgment (which spec/harness/workload, what flags, how
to read results, capacity rules, EPP drop decoding, …). **No Python, no `if/elif`** — decision
logic that belongs in a model's reasoning lives here, loaded at runtime. This file is meta-guidance
for *you* editing these files; it is deliberately excluded from the runtime knowledge glob (see below).

## Layout — 10 topic subfolders (files resolve by BASENAME/STEM, not path)
The files are grouped into topic folders for navigation only; **every enumeration site walks the
tree RECURSIVELY (`rglob`) and resolves a guide by its basename/stem**, so the folder a file sits
in is transparent to `read_knowledge`, `fetch_key_docs`, the prompt index, `knowledge_hash`, and the
MCP resource surface. Move a file between folders freely — just keep its basename unique (see gotcha).
```
knowledge/
├─ CLAUDE.md      (this file — excluded from the runtime glob)
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
└─ reference/     packaging sim_integration key_docs.yaml useful_repo_docs.md
```
⭐ = CORE (inlined into every prompt — the `CORE_KNOWLEDGE` tuple in `app/agent/prompt.py`).

## CORE vs on-demand — the cost rule
- **Adding to CORE is expensive**: CORE (the ⭐ files above) is inlined verbatim into the always-on,
  prompt-cached prefix on *every* call (~300–500 tok/file). Default to on-demand; only promote if the
  content is needed in the first half of a session (interview/plan/deploy) — the prefix is already at
  its low-risk floor. `key_docs.yaml` / `deploy_path_playbook.md` / `quickstart_playbook.md` are
  deliberately ON-DEMAND (served by `fetch_key_docs` / post-interview choice / the skill-grounding gate).
- **On-demand** files (everything else) are auto-discovered by the recursive glob and listed in a
  one-line **index**; the model pulls one with `read_knowledge("<topic>")` when a tool's description
  cues it. **There is no manual index file** — discovery is the glob + each file's first heading.

## Invariants / gotchas
- **After ANY edit, check `wc -c` ≤ ~6,000** (the whole-guide `read_knowledge` clamp,
  `DEFAULT_TOOL_RESULT_BUDGET`). Adding even one bullet to an over-budget file EVICTS its own tail
  from the preview — and `dropped_sections` only names HEADINGS past the cut, so vanished mid-section
  bullets give the agent zero signal. Over budget → split into a new file + a stub cross-cue, don't trim facts.
- **Renaming a file breaks its `read_knowledge('<stem>')` cues** (and any test). Grep `knowledge/` for the
  old stem before renaming. Cross-file cueing convention: a file says `read_knowledge('other')` to defer.
- **Basenames AND stems must stay globally unique across ALL subfolders.** Resolution is by
  basename/stem over the recursive glob, so two files sharing a basename/stem (in different folders)
  would silently shadow each other. Locked by `tests/platform/test_knowledge_stem_uniqueness.py`. Adding a file:
  drop it in the fitting topic folder with a fresh basename — the layout is navigational, nothing pins
  a file to a folder (no code joins a hard-coded `knowledge/<folder>/…` path except the four direct
  joins in `knowledge_access.py`/`cards.py`/`report_metrics.py`, which pin `reference/`, `conversation/`,
  `analysis/`).
- **Test-pinned content** — keep these or hermetic tests fail:
  `epp_headers.yaml` (`dropped_reason_enum` incl. `rejected-saturated`, `evicted-priority`, each with
  `cause`/`remedy`/`capacity_not_breakage`), `welllit_path_advisor.yaml` (10 archetypes + required fields),
  `readiness_probes.md` (startup-judgment phrases).
- **`welllit_path_advisor.yaml` is snapshot-gated** by `tests/flows/catalog_snapshot.py`: every archetype's
  `scenario` must be in the snapshot SPECS and each `benchmark_workload` in WORKLOADS. Adding an archetype whose
  spec/workload isn't in the snapshot yet requires running `make snapshot-catalog` in the SAME change. (The other
  advisor files — `usecase_to_profile.yaml`/`key_docs.yaml`/`deploy_path_playbook.md` — are free text, not gated.)
- **`CLAUDE.md` / `README.md` here are NOT knowledge** — they're filtered out of the glob in
  `app/agent/prompt.py::_knowledge_sections` and `app/tools/access/knowledge_access.py::_knowledge_files`
  (and `read_knowledge` won't return them). Locked by `tests/platform/test_knowledge_meta_excluded.py`. If you add
  another meta/doc file here, add its name to that exclusion set or it leaks into the agent's prompt.

## Scoped checks (run after editing knowledge files)
```bash
pytest tests/platform/test_epp_headers.py tests/tools/test_welllit_advisor.py \
       tests/orchestrator/test_serving_readiness.py tests/tools/test_new_tools.py \
       tests/platform/test_knowledge_meta_excluded.py tests/platform/test_knowledge_stem_uniqueness.py
```
