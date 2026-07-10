# Config / model-drift audit log — read on demand

Per the large-codebase best-practices guide ("review config after major model releases; retire
workarounds built for old model limitations"). Append a dated entry whenever you reorganize the
per-turn config (CLAUDE.md, system prompt, hooks, skills, memory) or audit it after a model
release. Split out of `PROJECT_BRAIN_REFERENCE.md` (it's pure history — loaded only when doing
the next such review).

- **2026-06-07 — Opus 4.8 (Claude Code) / agent runtime = Sonnet 4.6.** Reviewed `CLAUDE.md` +
  the agent's system-prompt `ROLE`/`HARD_RULES` (`app/agent/prompt.py`): **no stale model-era
  workarounds found** — both encode project facts + domain procedure, not model coaxing. The
  always-on prompt prefix was already trimmed ~33.6k→~28.1k tok across 4 commits (the lowest-risk
  levers are exhausted; further CORE trimming needs the live-LLM eval, which is off-limits unless
  user-explicit). Re-review after the next major model release.
- **2026-06-13 — context-budget pass (Claude Code = Opus 4.8).** Moved the reference/history
  sections out of the always-on `CLAUDE.md` into `PROJECT_BRAIN_REFERENCE.md`; converted the
  `use-worktree-when-implementing` memory into a `PreToolUse(Edit|Write)` worktree gate and the
  `rtx5070-gpu-cluster` memory into a keyword-gated `UserPromptSubmit` injection
  (`.claude/hooks/gpu_context.sh`). Goal: shrink the per-turn always-on prefix without losing the
  knowledge — it now loads on demand / on relevance instead of every turn.
  *(Historical — those hooks were removed in the 2026-06-20 entry below.)*
- **2026-06-20 — Claude Code hook teardown (Claude Code = Opus 4.8).** Removed the entire `.claude/`
  hook suite + the tool-lessons/context feature (per user request, decluttering): deleted
  `git_add_guard`, `gpu_context`, `context_sync`, `tool_lesson_inject`, `transcript_error_capture`,
  `record_lesson`, `ruff_autofix` (plus the already-removed `tool_error_capture`, `reflect_session_end`,
  `recon_lib` + `reconcile-before-merge` skill), and the whole root `context/` dir (tool-lessons data,
  auto-index, `trace_tokens.py`). Dropped the `hooks` + `env` blocks from `.claude/settings.json` (now
  permissions-only). Lint moved off the per-edit `ruff_autofix` hook onto a local
  `.git/hooks/{pre-commit,pre-merge-commit}` ruff gate scoped to `main` (gates committed/merged code only;
  not version-controlled — recreate on fresh clone). **Net: zero Claude Code hooks.**
- **2026-06-21 — graphify dev code-nav removed (per user request).** Deleted the graphify integration
  entirely: the `graphify-out/` code-graph dir, the custom subdir-aware `.git/hooks/post-commit` rebuild
  hook, the `.gitignore` entries, the CLAUDE.md / this-file usage sections, and the global graphify skill +
  `graphifyy` binary. **Net now: zero git hooks beyond the `main`-scoped pre-commit/pre-merge lint+test gate.**
  (The unmerged `worktree-graphify-runtime-tool` prototype branch + its OPEN_ITEMS / PROPOSAL_GAP_REPORT
  entries were left intact as a record.)
- **2026-06-22 — config reconstruction (Claude Code = Opus 4.8).** Reorganized the per-turn config to cut
  always-on bloat: created a global `~/.claude/CLAUDE.md` (reply format, ask-when-in-doubt + precedence,
  plan-mode task-sizing, web-search, and the project-`CLAUDE.md`-as-folder-map convention); moved coding
  conventions into a `coding-guidelines` skill and the commit→review→merge definition-of-done into a
  `finish-implementation` skill; pruned ~16 now-redundant/historical auto-memories (live open-risks kept).
  Rewrote the project `CLAUDE.md` into a **folder map + non-negotiables + on-demand pointers** — relocated
  "what's built", the upstream reuse paths, and the test-env/finish-loop prose (now in `tests/CLAUDE.md` +
  the `finish-implementation` skill).
- **2026-06-22 (follow-up) — reference debloat + split.** Restored two memories pruned earlier the same
  day (`moonshot-poc`, `llm-d-benchmark-quickstart`). Debloated `PROJECT_BRAIN_REFERENCE.md` (dropped the
  feature-list prose + stale "36 tools"/"35-tool" counts → point at `FEATURES.md` + `registry.py`; the
  doc-map and run-locally blocks duplicated `docs/README.md`/`docs/guides/DEPLOYMENT.md` → collapsed to pointers)
  and **split it** into this `CONFIG_AUDIT_LOG.md` + `UPSTREAM_REUSE_PATHS.md`, leaving the brain-ref a slim
  orientation hub. Added the seven missing per-folder `app/{capacity,readiness,packaging,observability,llm,
  storage,web}/CLAUDE.md` files (file-level detail, per the new folder-map convention).
- **2026-06-28 — `run-llm-d-benchmark` skill rewritten (dropped `run_only.sh`); all 3 read-only repos verified current.**
  Upstream commit `3eb03f5` rewrote `skills/run-llm-d-benchmark/SKILL.md` to drive the `llmdbenchmark` CLI
  directly (no more `run_only.sh` existing-stack entrypoint) and renamed the no-llm-d-baseline knob
  `base_url`→`ENDPOINT_URL`. No wiring change needed — skills are read live via `key_docs.yaml`→`fetch_key_docs`,
  the clone-URL allowlist is unchanged, and nothing here ever executed the skill's `run_only.sh`. Refreshed the
  two now-stale "skill's run_only.sh entrypoint" notes in `knowledge/key_docs.yaml` + `author_spec_workload.md`.
  **No version drift (corrects an earlier note in this entry):** the "v0.8.0" is the *skills* repo's own PR
  label (`benchmark-v080`) — **llm-d-benchmark has NOT released a 0.8.0.** Its latest tag/release is **v0.7.0**
  (`gh release view` = v0.7.0) and our checkout is fully current: `main` == `origin/main`, 0 ahead / 0 behind,
  `git describe` → `v0.7.0-1-g09e0c39c`. The released v0.7.0 CLI already supports the skill's assumptions
  (`--harness` at `cli.py:828`, `ENDPOINT_URL`, auto token substitution via `profile_renderer.py`), so there
  is nothing to pull or bump. Verified same day: `llm-d`@`7edda66`, `llm-d-benchmark`@`09e0c39c`,
  `llm-d-skills`@`5a14639` all 0/0 vs their `origin/main`. The skill's "v0.8.0" label simply runs ahead of the
  benchmark repo's release numbering.
- **2026-06-28 — `llm-d-skills` promoted to REQUIRED; knowledge adapters dedup'd to defer to it (reverses 4eb3323).**
  Made the upstream skills the canonical default for deploy/teardown/benchmark/compare/autoscale procedures, per
  user request. **config.py:** folded `SKILLS_REPO_NAME` into `repo_paths` (the readiness gate + provenance/
  reproducibility set + command-runner `repo:<name>` resolution) and removed the now-redundant `readable_repo_paths`
  property (its 2 call sites — `repos.py`, `knowledge_access.py` — point at `repo_paths`). Net: `/readyz` now 503s
  when the skills repo is absent (fail-loudly per rule 7), reversing 4eb3323's "separate readable from required / un-gate
  readiness" decision — that's the behavior the user asked for. `ref` is still never applied to the independently-
  versioned skills repo (`repos.py` guard kept). **Tests:** updated the 3 frozen tests 4eb3323 had protected —
  `test_retention._make_repos` now creates 3 repo dirs; `test_reproducibility_tools` expects the 3-repo SHA set.
  **Knowledge dedup:** the 5 skill-adapter files were already mostly delta; removed the 3 short skill-step *recap*
  blocks that duplicated the upstream procedure (`teardown.md` L21–34 helm/kustomize steps, `autoscaling.md` WVA-setup
  recap, `deploy_path_playbook.md` deploy-step enumeration — the last is CORE, so trimming also cut prompt-prefix bytes),
  each replaced with a "read the skill, don't restate it" pointer; the delta (SessionPlan gate, CLI, BR-v0.2, tool names,
  CPU-sim caveats, welllit map) is untouched. `sweep_playbook.md` + `author_spec_workload.md` were ~95% delta already → left as-is.
  **Docs:** `UPSTREAM_REUSE_PATHS.md` + `FEATURES.md` skills sections relabel it the 3rd REQUIRED repo and note the defer-not-restate adapters. Full suite green (2147 passed).
- **2026-07-05 — model ids gained a 4th home: `scripts/setup-claude-plan.sh`'s interactive menu.** The new
  Claude-plan wiring script hardcodes its model picker (`claude-sonnet-4-6` recommended · `claude-haiku-4-5` ·
  `claude-opus-4-8` + custom), alongside `app/config.py`'s `AGENT_SDK_MODEL` default, `.env.example`, and
  `docs/guides/DEPLOYMENT.md`'s "the setup script recommends sonnet-4-6" note. On the next model-generation bump,
  update all four together — `git grep -l 'claude-sonnet-4-6\|claude-haiku-4-5\|claude-opus-4-8' -- scripts
  docs .env.example app/config.py` finds them.
