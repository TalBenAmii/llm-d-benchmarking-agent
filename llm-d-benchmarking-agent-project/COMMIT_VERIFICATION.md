# COMMIT VERIFICATION — every commit, audited against the code

> A tick list of all **122 commits** on `main` (HEAD `9adf0a7`), each checked to confirm the
> code it *describes* is actually implemented in the current working tree. Companion to
> [`FEATURES.md`](FEATURES.md) (the feature inventory + how to see each one).
>
> **Method:** for each commit, read the full message + the changed-file stat, then locate the
> concrete capability in the *current* tree (`path:line` — a function, class, route, asset, or
> test). This is a **static implementation audit** (does the described code exist), complemented
> by the **live runtime checks** captured in `FEATURES.md` for the user-facing surfaces.
>
> **Verdict legend:** ✅ implemented-as-described · ⚠️ partial / moved / renamed · ❌ missing ·
> 📝 doc-only (markdown status/docs) · 🔀 merge roll-up (code carried by a member commit).

## Bottom line

| | Count | Result |
|---|---|---|
| Substantive feature / fix / chore commits | **58** | **✅ all implemented as described** |
| Merge roll-ups | 36 | 🔀 verified via member commits |
| Doc-only commits | 28 | 📝 confirmed (ROADMAP/PROGRESS/docs reflect the change) |
| **Total** | **122** | **0 missing · 0 partial** |

Of the ✅ feature commits, **16 were also verified live** this session (HTTP endpoints, auth/rate-limit, deploy renders, structured logging, GC, persistent chats, metrics) — see the Evidence log in `FEATURES.md`.

---

## Era 1 — Pre-roadmap foundations (MVP → flow harness → sweeps → kind ownership → persistent chats)

| Commit | Type | Claim (short) | Verdict | Evidence (path:line or test) | Note |
|---|---|---|---|---|---|
| 2c4f986 | feat | iter1 MVP: FastAPI backend, agent loop, tools, chat UI, allowlist | ✅ | app/agent/loop.py:28 (AgentLoop), app/main.py:173 (FastAPI), app/security/allowlist.py:38, app/tools/registry.py, ui/ | All MVP pieces present + grown |
| 4e7ad8e | feat | Re-skin chat UI in llm-d brand theme (purple/Red Hat, dark+light) | ✅ | ui/styles.css:9 (--brand-purple #7f317f), ui/index.html:14 (Red Hat fonts), ui/app.js:29 (applyTheme + localStorage) | Hexagon mark + persisted toggle |
| 0a13396 | feat | run.sh one-command start (venv, install, .env, uvicorn, flags) | ✅ | run.sh:30-34 (flags), run.sh:56-59 (uv/venv), run.sh:77-78 (HOST/PORT) | All flags present |
| 58f0829 | feat | Flow-validation harness + CI: golden-transcript flows, no exec | ✅ | tests/flows/harness.py:66 (CaptureRunner), tests/flows/flows.py, scripts/validate_flows.py, Makefile:45, .github/workflows/agent-flow-validation.yml | Harness+CI+VALIDATION.md |
| 8c9b00e | merge | Merge feature/agent-flow-validation | 🔀 | same tree as 58f0829 | Roll-up |
| 507c6d6 | feat | 6 more guide flows via shared `_guide_deploy_flow` factory (→12) | ✅ | tests/flows/flows.py:143 (factory), :208-256 (5 guide flows); 12 named flows | GPU guides live_eval=False |
| 1468429 | feat | Sweeps & A/B: compare_reports tool + experiment DoE sweep | ✅ | app/tools/compare.py:52, app/validation/report.py:332/416, execute.py:14 (experiment), registry.py:282, knowledge/sweep_playbook.md | 10th tool wired |
| 0d0a8d8 | fix | Fix 2 sweep/compare gaps (_result_location, baseline index map) | ✅ | execute.py:140-154 (experiment→workspace), compare.py:100-108 (baseline map) | tests/test_sweep.py |
| b891463 | feat | Agent installs Docker+kind; run_command + project-script + fetch_key_docs | ✅ | app/tools/command.py:21, runner.py:126, scripts/install_prereqs.sh, security/allowlist.yaml:141/284, probe.py:190, knowledge/key_docs.yaml | 12-tool registry |
| db0c01c | merge | Merge feature/agent-installs-docker-kind | 🔀 | same tree as b891463 | Roll-up |
| ec2eb47 | feat | Persist chats + recent-chats sidebar; WS resume + session APIs | ✅ | session.py:163/190/194, main.py:392 (resumed)/:404 (history)/:220-226 (sessions API), ui/index.html:31, tests/test_sessions.py:53 | Path-traversal guard tested |
| 04c06fe | merge | Merge feature/persistent-chats | 🔀 | same tree as ec2eb47 | Roll-up |
| 243f458 | chore | Stop tracking nested llm-d / llm-d-benchmark repos | ✅ | `git ls-files` shows neither gitlink tracked | Dirs remain on disk untracked |
| e200bf1 | chore | Drop tracked .gitignore; exclude nested repos locally | ✅ | root .gitignore untracked; .git/info/exclude:11-12 | No phantom nested-repo changes |

**Era 1: 11 ✅ · 3 🔀 · 0 ❌**

---

## Era 2 — Roadmap Phases 0–3 (transparency, parallel sessions, K8s orchestrator)

| Commit | Type | Claim (short) | Verdict | Evidence (path:line or test) | Note |
|---|---|---|---|---|---|
| a8ee8c2 | feat | Phase 0: roadmap scaffolding + worktree-portable tests | ✅ | tests/conftest.py:18-22 (settings-resolved bench repo), ROADMAP.md:33 | conftest no sibling-path assumption |
| 4f200ab | feat | Phase 1: emit a `command` event for every executed command | ✅ | events.py:40 (COMMAND), context.py:110/185/228 (_emit_command), session.py:81 (record), tests/test_command_events.py | persist/replay round-trip |
| daf5486 | feat | Phase 1: UI command log + debug view | ✅ | ui/index.html:74/82, ui/app.js:46-61/515-532, styles.css:363-391, tests/flows/test_flows.py:41-50 | parity test |
| 9e98393 | doc | Phase 1 docs | 📝 | README.md:48, ROADMAP.md:44 | doc-only |
| f57a153 | merge | Merge Phase 1 | 🔀 | rolls up 4f200ab/daf5486 | Roll-up |
| 74fb732 | feat | Phase 2: parallel sessions, run cap, background-safe runs | ✅ | config.py:102 (max_concurrent_runs), context.py:74 (semaphore), main.py:81/379-413, runner.py:68/197/223 (killpg/start_new_session), tests/test_concurrency.py | runner lifecycle fix |
| 977de1a | doc | Phase 2 docs + Phase-3 deferrals | 📝 | ROADMAP.md:60/74 | doc-only |
| 80fd17a | merge | Merge Phase 2 | 🔀 | rolls up 74fb732 | Roll-up |
| 0d1106c | feat | Phase 3a: KubeClient + kubectl allowlist for Jobs | ✅ | orchestrator/kube.py:58 (Protocol)/:74-178 (apply/logs/delete), security/allowlist.yaml:111/209-241, tests/test_orchestrator.py | shells kubectl via ToolContext |
| 8ab5697 | feat | Phase 3b: Job lifecycle — manifest/submit/watch/logs/reconstruct | ✅ | job.py:276 (build_job_manifest, backoffLimit:0, restartPolicy:Never)/:364, controller.py:131/148/188/255, tests/test_orchestrator_controller.py | reconstruct from labels |
| a77b165 | feat | Phase 3c+3d: fault classification + retry/dead-letter | ✅ | faults.py:15-20/99-111 (6 kinds), controller.py:179/280 (run_with_retries), tests/test_orchestrator_faults.py, _retry.py | RunOutcome records attempts |
| 9cc48a3 | feat | Phase 3e: parallel sweep + cleanup | ✅ | controller.py:350 (run_sweep Semaphore)/:462 (cleanup terminal-only), tests/test_orchestrator_sweep.py | PVC untouched |
| a68cde5 | feat | Phase 3e: wire orchestrate_benchmark_run tool | ✅ | tools/orchestrate.py:70/90-97, config.py:107, schemas.py:107, registry.py:247/286, knowledge/orchestrator.md, tests/test_orchestrator_tool.py | tool registered |
| ed731f1 | fix | Phase 3 review: watch busy-loop, sweep isolation, classify gap, hardening | ✅ | controller.py:160-177 (monotonic max_wait)/:323-329, job.py:220 (DNS-1123)/:290-293 (drop ALL caps) | 4 review fixes present |
| 550a37d | doc | Phase 3 docs (orchestrator DONE) | 📝 | ROADMAP.md:79 | doc-only |
| 397c594 | merge | Merge Phase 3 orchestrator | 🔀 | rolls up 0d1106c→ed731f1; full app/orchestrator/ tree | Roll-up |

**Era 2: 10 ✅ · 3 📝 · 3 🔀 · 0 ❌**

---

## Era 3 — Roadmap Phases 4–10 (analyzer, capacity, observability, storage, packaging, multi-harness, docs)

| Commit | Type | Claim (short) | Verdict | Evidence (path:line or test) | Note |
|---|---|---|---|---|---|
| 29c4cb0 | feat | Phase 4: analyzer — goodput, SLO filtering, Pareto/DoE | ✅ | analysis.py:69 (SLOTargets)/:181 (evaluate_slo)/:378 (pareto)/:145 (goodput), tools/analyze.py, session_plan.py:20, tests/test_analyze.py | — |
| 659690e | fix | Carry full percentile ladder through `_stat` | ✅ | report.py:51-52 (p0p1..p99p9+mean)/:100, tests/test_analyze.py:140/160 (37.5% pinned) | regression pinned |
| d01d31b | merge | Merge Phase 4 | 🔀 | rolls up 29c4cb0+659690e | — |
| a58120a | doc | Mark Phase 4 done | 📝 | ROADMAP.md:102 | doc-only |
| a7db900 | feat | Phase 6: capacity pre-flight (check_capacity) | ✅ | capacity/planner.py:75/27-29, tools/capacity.py:36, scripts/capacity_check.py, registry.py:115/273, tests/test_capacity.py | — |
| bb47552 | merge | Merge Phase 6 | 🔀 | rolls up a7db900 | — |
| 13284c7 | doc | Mark Phase 6 done | 📝 | ROADMAP.md:120 | doc-only |
| 6c36cde | feat | Phase 7: observability — /metrics, instrumentation, live metrics | ✅ | observability/metrics.py:138 (render_prometheus)/instrument.py:38, main.py:208 (/metrics)/:186, context.py:123 + controller.py:139/305, tools/observe.py:44, deploy/observability/ | — |
| c7e5b35 | merge | Merge Phase 7 | 🔀 | rolls up 6c36cde | — |
| dc6cd1f | doc | Mark Phase 7 done | 📝 | ROADMAP.md:128 | doc-only |
| 60d356d | feat | Phase 5: historical storage + trends UI | ✅ | storage/history.py:115/226 (trend)/:282, tools/history.py, main.py:256/266 (/api/history[/trend]), ui/index.html:38, ui/app.js:254 (sparkline) | — |
| f211463 | merge | Merge Phase 5 | 🔀 | rolls up 60d356d | — |
| cd3f1da | doc | Mark Phase 5 done | 📝 | ROADMAP.md:113, PROGRESS.md:231 | doc-only |
| 0819e66 | feat | Phase 8: image + Helm/Kustomize + least-priv RBAC | ✅ | Dockerfile:78 (USER 10001), packaging/assets.py:39, deploy/helm/.../rbac.yaml:14-21, deploy/kustomize/base/rbac.yaml, orchestrate.py:101/147 (SA) | — |
| 742e20d | merge | Merge Phase 8 | 🔀 | rolls up 0819e66 | — |
| 7d30dc7 | doc | Mark Phase 8 done | 📝 | ROADMAP.md:136, PROGRESS.md:215 | doc-only |
| 0cc56bb | feat | Phase 10: multi-harness orchestration in one session | ✅ | report.py:477 (compare_across_harnesses)/:277, tools/multiharness.py:50, registry.py:187/283, knowledge/multi_harness.md | 17 tools at this point |
| 60546f4 | merge | Merge Phase 10 | 🔀 | rolls up 0cc56bb | — |
| af34a18 | doc | Mark Phase 10 done | 📝 | ROADMAP.md:151, PROGRESS.md:194 | doc-only |
| 312f952 | doc | Phase 9: documentation suite | ✅📝 | docs/API.md, ARCHITECTURE.md, DEPLOYMENT.md, USER_GUIDE.md, docs/README.md (all substantive) | docs-only by design |
| a707bf5 | merge | Merge Phase 9 | 🔀 | rolls up 312f952 | — |
| 0ac97bd | doc | Mark Phase 9 done | 📝 | ROADMAP.md:144, PROGRESS.md:184 | doc-only |
| af878d4 | merge | Merge feature/roadmap (phases 0-10) | 🔀 | roll-up of all above | — |
| 6cb372e | chore | workflow update | ✅ | .claude/workflows/roadmap-autopilot.js, finish-roadmap-recovery.js (repo root) | harness scripts, not app code |

**Era 3: 9 ✅ · 7 📝 · 8 🔀 · 0 ❌**

---

## Era 4 — chat-UI fixes + Roadmap v2 Phases 11–18 + Simulate Mode + prompt perf

| Commit | Type | Claim (short) | Verdict | Evidence (path:line or test) | Note |
|---|---|---|---|---|---|
| 5aff57f | fix | Markdown render + themed scrollbar + per-session Channel + approval persistence | ✅ | ui/app.js:406 (renderMarkdown), styles.css:248, channel.py:45/125/147, session.py:64/87, main.py:298/492, tests/test_ws.py | 4 fixes |
| 0b691d7 | merge | Merge fix/chat-ui-issues | 🔀 | same as 5aff57f | Roll-up |
| a0d4211 | fix | Accept any flag once exe+subcommand allowlisted; greedy value consume | ✅ | allowlist.py:271/301-305, allowlist.yaml:14, tests/test_allowlist.py | metachar screen kept |
| 81fd21c | doc | Open Roadmap v2 section | 📝 | ROADMAP.md:177 | doc-only |
| 7d74ed0 | feat | Phase 11: structured JSON logging + correlation IDs | ✅ | logging.py:54 (JsonFormatter)/:38, logctx.py:24 (contextvars), config.py:96-97, loop.py:45, main.py:57, tests/test_logging.py | no new dep · **live-verified** (startup JSON) |
| effedbb | merge | Merge Phase 11 | 🔀 | same as 7d74ed0 | Roll-up |
| 5ee3fc3 | doc | Mark Phase 11 done | 📝 | ROADMAP.md:181 | doc-only |
| 25e9888 | feat | Phase 12: optional Bearer auth + token-bucket rate limit + CORS | ✅ | auth.py:57 (compare_digest)/:110 (TokenBucket)/:154 (RateLimiter)/:198/:89, config.py:72-88, main.py:155 (install_cors), tests/test_api_trust.py | default OFF · **live-verified 401/200/429** |
| f7bba88 | merge | Merge Phase 12 | 🔀 | same as 25e9888 | Roll-up |
| f72b859 | doc | Mark Phase 12 done | 📝 | ROADMAP.md:193 | doc-only |
| 7ffd7c3 | feat | Phase 13: per-command timeouts + usage quotas as YAML data | ✅ | allowlist.py:47 (timeout_s)/:53/:85, quota.py:43 (QuotaCounter)/:28, context.py:148, execute.py:16-18 (Python timeout table removed), tests/test_governance.py | `_TIMEOUTS` gone |
| 6c5582f | merge | Merge Phase 13 | 🔀 | same as 7ffd7c3 | Roll-up |
| ffddf99 | doc | Mark Phase 13 done | 📝 | ROADMAP/PROGRESS | doc-only |
| 26b3052 | feat | Animated working indicator: spinning hexagon + live status | ✅ | ui/index.html:93/96, ui/app.js:23/637/692, styles.css (working/hexagon) | pure frontend |
| 86c3855 | feat | Phase 15: WS frame Pydantic validation (tagged union) + bounded live buffer | ✅ | ws_schemas.py:39-68/:76, channel.py:54 (deque maxlen)/:57, events.py:61 (NON_TURN_EVENTS), main.py:352, tests/test_ws.py | socket survives bad frame |
| 0e4974d | fix | Guard non-JSON/binary WS decode; exclude lifecycle frames from buffer | ✅ | channel.py:94, events.py:61, main.py:428, tests/test_ws.py | — |
| 043e7c3 | fix | GC orchestrator workspace/jobs/*.yaml (file not dir) | ✅ | retention.py:59 (ManagedArea "jobs",file)/:43-45, tests/test_retention.py | was scanning 0 |
| e294fea | merge | Merge Phase 15 | 🔀 | same as 86c3855/0e4974d | Roll-up |
| 19af393 | doc | Mark Phase 15 done | 📝 | ROADMAP/PROGRESS | doc-only |
| 80b2e05 | feat | Phase 18: retention GC + startup self-check + /readyz | ✅ | retention.py:6 (run_gc)/:14 (self_check)/:16/:49, config.py:127-129, main.py:103/195 (/readyz), tests/test_retention.py, test_readyz.py | **live-verified GC + /readyz** |
| 4f1edec | merge | Merge Phase 18 | 🔀 | same as 80b2e05 | Roll-up |
| 455fb4f | doc | Mark Phase 18 done | 📝 | ROADMAP.md:230 | doc-only |
| b61d8eb | feat | Phase 16: cancel/reattach + graceful shutdown + /readyz; cancel_run tool | ✅ | lifecycle.py:57 (RunRegistry)/:99, tools/cancel.py:21, ws_schemas.py:56 (CancelIn), main.py:85/133/479, runner.py:64/231, tests/test_run_lifecycle.py | — |
| cf8ff4c | fix | Deterministic cancel slot-release (no shield false-positive) + NOTES.txt | ✅ | lifecycle.py:31/:118/:121/:136, templates/NOTES.txt | True only on task.done() |
| 8db8b70 | merge | Merge Phase 16 | 🔀 | same as b61d8eb/cf8ff4c | Roll-up |
| f687ded | doc | Mark Phase 16 done | 📝 | ROADMAP/PROGRESS | doc-only |
| 21fe902 | doc+data | Ops docs (SECURITY/TROUBLESHOOTING/CONTRIBUTING/CHANGELOG) + Prometheus alert rules | ✅ | docs/{SECURITY,TROUBLESHOOTING,CONTRIBUTING,CHANGELOG}.md, deploy/observability/alerts.rules.yaml:25 (5 alerts), tests/test_ops_docs.py | docs+data |
| 494637a | merge | Merge Phase 17 | 🔀 | same as 21fe902 | Roll-up |
| 6cfc35c | doc | Mark Phase 17 done | 📝 | ROADMAP/PROGRESS | doc-only |
| 0de4f5d | feat | Phase 14: ruff + mypy + coverage CI gates | ✅ | pyproject.toml:53/85/106, Makefile:7 (COV_FAIL_UNDER 85)/:32-38, CI workflow, tests/test_quality_gates.py | gate 85% |
| 0635ac2 | merge | Merge Phase 14 | 🔀 | same as 0de4f5d | Roll-up |
| 69cb208 | doc | Mark Phase 14 done | 📝 | ROADMAP/PROGRESS | doc-only |
| 3c050e8 | perf | Cut prompt overhead: core-inline knowledge + read_knowledge, strip schema titles, cap history | ✅ | prompt.py:91 (CORE_KNOWLEDGE)/:101, probe.py:248 (read_knowledge)/:260, registry.py:296 (_strip_titles)/:313, tests/test_new_tools.py | ~20.4K→~12.3K |
| f5fe430 | merge | Merge perf/reduce-prompt-tokens | 🔀 | same as 3c050e8 | Roll-up |
| 81f2b42 | feat | Simulate Mode: SimRunner no-op, skip per-command approval, synthetic report | ✅ | runner.py:255 (SimRunner), config.py:63 (simulate), prompt.py:64 (SIMULATE_NOTE), context.py:120, main.py:72, tests/test_simulate.py | upfront plan approval kept |
| 9e8cc54 | merge | Merge feature/simulate-mode | 🔀 | same as 81f2b42 | Roll-up |
| ed77f76 | merge | Merge feature/roadmap-v2 (phases 11-18) | 🔀 | aggregates all above; conftest.py:7 (SIMULATE neutralize + pytest-timeout) | Roll-up |
| 43f10c8 | chore | "some aux stuff" | ✅ | +.claude/workflows/roadmap-v2-autopilot.js, roadmap-v3-autopilot.js, PROPOSAL_ROADMAP.md, 2×.env.bak (no app code) | housekeeping; all present |

**Era 4: 17 ✅ · 9 📝 · 12 🔀 · 0 ❌**

---

## Era 5 — Roadmap v3 Phases 19–26 (proposal gaps) + token-tracking

> ⚠️ Mapping correction: in the audit dispatch three SHAs were paired with the wrong one-liner;
> the rows below use the **correct** SHA→subject mapping. All three behaviors are present.

| Commit | Type | Claim (short) | Verdict | Evidence (path:line or test) | Note |
|---|---|---|---|---|---|
| 473f188 | doc | Open Roadmap v3 section | 📝 | ROADMAP.md:278 | doc-only |
| baff0dd | feat | Phase 20: well-lit-path advisor (workload shape → scenario) | ✅ | knowledge/welllit_path_advisor.yaml, prompt.py:85, knowledge/deploy_path_playbook.md:32, tests/test_welllit_advisor.py | wired + playbook-referenced |
| 350943d | feat | Phase 19: DOE experiment-file generator + token-characteristics elicitation | ✅ | app/tools/doe.py:144 (generate_doe_experiment), app/validation/doe.py:146/194, registry.py:278, schemas.py:355, knowledge/sweep_playbook.md, tests/test_doe.py | 21-tool registry here |
| e7b81f9 | feat | Phase 25: surface §3.4 metrics (KV-cache hit rate, schedule delay, GPU util) | ✅ | report.py:219 (extract_standard_metrics), analysis.py:287-289, analyze.py:96, knowledge/standard_metrics.yaml:32/49/62, tests/test_standard_metrics.py | `None` when absent |
| 2d0d96b | fix | `run` output uses `local` destination, not an abspath | ✅ | execute.py:96 (setdefault output local)/:97, schemas.py:96-99, flows.py:132 (-r local) | — |
| 7fe4d80 | fix | DOE run constants under `design.run.constants` (not top-level) | ✅ | app/validation/doe.py:242-245 (+ comment 238-241), tests/test_doe.py | — |
| e1de77e | merge | Merge Phase 19 | 🔀 | folds 350943d | Roll-up |
| 923d957 | doc | Mark Phase 19 done | 📝 | PROGRESS.md:52 | doc-only |
| d0037c1 | merge | Merge Phase 20 | 🔀 | folds baff0dd | Roll-up |
| 6a299a9 | doc | Mark Phase 20 done | 📝 | PROGRESS.md:416 | doc-only |
| 62e98b2 | merge | Merge Phase 25 | 🔀 | folds e7b81f9 | Roll-up |
| bbad604 | doc | Mark Phase 25 done | 📝 | PROGRESS.md:428 | doc-only |
| cf40bc4 | feat | Phase 21: real-time benchmark-pod log streaming | ✅ | kube.py:69/131 (stream_log_lines), controller.py:196 (_tail_logs)/:188, context.py:201 (on_line), tools/orchestrate.py (on_log_line), tests/test_orchestrator_logstream.py | async tail + isolation |
| 1276907 | merge | Merge Phase 21 | 🔀 | folds cf40bc4 | Roll-up |
| 177217b | doc | Mark Phase 21 done | 📝 | PROGRESS.md:441 | doc-only |
| 7e9e88a | feat | Phase 23: resource mgmt (node affinity / GPU selection / anti-starvation) | ✅ | job.py:48 (class Scheduling: node_selector/tolerations/affinity/gpu_count/avoid_labels :73-79), orchestrate.py (threads scheduling), schemas.py:130, knowledge/resource_management.md, tests/test_resource_management.py | full field set |
| e9f7cd2 | merge | Merge Phase 23 | 🔀 | folds 7e9e88a | Roll-up |
| 3ed4ee8 | doc | Mark Phase 23 done | 📝 | PROGRESS.md:453 | doc-only |
| 6927fca | feat | Phase 22: DOE checkpoint/resume via per-sweep ConfigMap | ✅ | checkpoint.py:77 (SweepCheckpoint)/:174 (CheckpointStore), controller.py (run_sweep sweep_id + reconstruct_sweep), kube.py:65 (list_configmaps), allowlist.yaml:114 (cm), tests/test_orchestrator_checkpoint.py | idempotent resume |
| 37ecf94 | merge | Merge Phase 22 | 🔀 | folds 6927fca | Roll-up |
| ff50363 | doc | Mark Phase 22 done | 📝 | PROGRESS.md:32 | doc-only |
| dca71dc | feat | Phase 24: endpoint health-check before submit (+ approval-gated standup suggestion) | ✅ | orchestrator/readiness.py (analyze_endpoints), tools/readiness.py:69 (check_endpoint_readiness)/:93, orchestrate.py:88/118 (require_ready_endpoint gate), registry.py:274, tests/test_endpoint_readiness.py | gate + suggestion |
| ef571bd | merge | Merge Phase 24 | 🔀 | folds dca71dc | Roll-up |
| 1fdd001 | doc | Mark Phase 24 done | 📝 | PROGRESS.md:470 | doc-only |
| 013889c | test | Phase 26: opt-in llm-d-inference-sim integration tests + non-gating CI | ✅ | tests/integration/{conftest,sim_report,test_sim_integration}.py, CI workflow:122 (sim-integration, continue-on-error:133), test_quality_gates.py:144, knowledge/sim_integration.md | env-gated, non-gating |
| 2ffe115 | merge | Merge Phase 26 | 🔀 | folds 013889c | Roll-up |
| ed06ef4 | doc | Mark Phase 26 done | 📝 | PROGRESS.md:11 | doc-only |
| 9ff5e1d | merge | Merge feature/roadmap-v3 into main | 🔀 | folds Phases 19-26 | Roll-up |
| 0bfb799 | feat | Token usage counter + provider-agnostic prompt caching + knowledge shrink | ✅ | provider.py:28 (Usage)/:68 (cache_key), anthropic_provider.py:40/46 (system cache), openai_provider.py:34 (gated prompt_cache_key), events.py:47 (USAGE), session.py:76 (session_total), ui/app.js:82/93 (Σ chip), tests/test_llm_caching_usage.py | 4 parts present |
| 9adf0a7 | merge | Merge feature/token-tracking into main (HEAD) | 🔀 | folds 0bfb799 | Roll-up |

**Era 5: 11 ✅ · 9 📝 · 10 🔀 · 0 ❌**

---

## Notes & caveats found during the audit

- **Doc drift:** `CLAUDE.md` says "18 tools"; the registry now defines **22** (`app/tools/registry.py:build_registry`). Tool count grew across commits (10 → 12 → 17 → 21 → 22).
- **Sibling-repo artifacts:** the Benchmark Report example/schema referenced by some analyzer tests lives in the read-only `llm-d-benchmark` sibling repo (not in this tree, by design); tests use hand-built summaries so they stay hermetic.
- **`6cb372e` / `43f10c8`** are housekeeping commits (autopilot workflow scripts under `.claude/workflows/`, `PROPOSAL_ROADMAP.md`, `.env.bak` snapshots) — no app code; all files present.
- **Live cross-check:** the user-facing subset (logging, /healthz, /readyz, /metrics, persistent chats, history/trend endpoints, auth 401/200, rate-limit 429, Helm/Kustomize render) was additionally exercised against a running server — see `FEATURES.md` → Evidence log.

_Audit performed against `main` @ `9adf0a7` on 2026-06-02 via five parallel code-audit passes, one per era; every substantive commit's described code was located in the current tree._
</content>
