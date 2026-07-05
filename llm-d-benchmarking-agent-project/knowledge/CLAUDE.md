# knowledge/ — the agent's editable brain (judgment, not mechanism)

These ~62 markdown/yaml files hold ALL judgment (which spec/harness/workload, what flags, how
to read results, capacity rules, EPP drop decoding, …). **No Python, no `if/elif`** — decision
logic that belongs in a model's reasoning lives here, loaded at runtime. This file is meta-guidance
for *you* editing these files; it is deliberately excluded from the runtime knowledge glob (see below).

## CORE vs on-demand — the cost rule
- **CORE** files are inlined **verbatim into every system prompt** (the `CORE_KNOWLEDGE` tuple in
  `app/agent/prompt.py` is the source of truth — currently `preconditions.md`,
  `usecase_to_profile.yaml`, `quickstart_playbook.md`, `conversation_style.md`). They cover the
  phases reached BEFORE the agent would know to ask for a specific guide. NOTE: `key_docs.yaml`
  (its content is delivered live by the `fetch_key_docs` tool) and `deploy_path_playbook.md` (a
  post-interview deploy-path-choice guide) are deliberately ON-DEMAND, not CORE.
- **On-demand** files (everything else) are auto-discovered by a `*.md`/`*.yaml`/`*.yml` glob and
  listed in a one-line **index**; the model pulls one with `read_knowledge("<topic>")` when a tool's
  description cues it. **There is no manual index file** — discovery is the glob + each file's first heading.
- **Adding to CORE is expensive**: it inflates the always-on, prompt-cached prefix on *every* call
  (~300–500 tok/file). Default to on-demand; only promote to CORE if the content is needed in the first
  half of a session (interview/plan/deploy). The prefix has already been trimmed to its low-risk floor.

## Invariants / gotchas
- **After ANY edit, check `wc -c` ≤ ~6,000** (the whole-guide `read_knowledge` clamp,
  `DEFAULT_TOOL_RESULT_BUDGET`). Adding even one bullet to an over-budget file EVICTS its own tail
  from the preview — and `dropped_sections` only names HEADINGS past the cut, so vanished
  mid-section bullets give the agent zero signal (a live probe regressed exactly this way,
  2026-07-04). Over budget → split into a new file + a stub cross-cue, don't trim facts.
- **Renaming a file breaks its `read_knowledge('<stem>')` cues** (and any test). Grep `knowledge/` for the
  old stem before renaming. Cross-file cueing convention: a file says `read_knowledge('other')` to defer.
- **Test-pinned content** — keep these or hermetic tests fail:
  `epp_headers.yaml` (`dropped_reason_enum` incl. `rejected-saturated`, `evicted-priority`, each with
  `cause`/`remedy`/`capacity_not_breakage`), `welllit_path_advisor.yaml` (10 archetypes + required fields),
  `readiness_probes.md` (startup-judgment phrases).
- **`welllit_path_advisor.yaml` is snapshot-gated** by `tests/flows/catalog_snapshot.py`: every archetype's
  `scenario` must be in the snapshot SPECS and each `benchmark_workload` in WORKLOADS. Adding an archetype whose
  spec/workload isn't in the snapshot yet requires running `make snapshot-catalog` in the SAME change. (The other
  advisor files — `usecase_to_profile.yaml`/`key_docs.yaml`/`deploy_path_playbook.md` — are free text, not gated.)
- **`CLAUDE.md` / `README.md` here are NOT knowledge** — they're filtered out of the glob in
  `app/agent/prompt.py::_knowledge_sections` and `app/tools/knowledge_access.py::_knowledge_files`
  (and `read_knowledge` won't return them). Locked by `tests/test_knowledge_meta_excluded.py`. If you add
  another meta/doc file here, add its name to that exclusion set or it leaks into the agent's prompt.

## Scoped checks (run after editing knowledge files)
```bash
pytest tests/test_epp_headers.py tests/test_welllit_advisor.py \
       tests/test_serving_readiness.py tests/test_new_tools.py tests/test_knowledge_meta_excluded.py
```
