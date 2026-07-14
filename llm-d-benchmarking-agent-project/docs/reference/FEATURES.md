# FEATURES: what this project does and how to see / verify each piece

> A single, evidence-backed inventory of every feature on `main` (MVP, roadmap v1 phases 0–10,
> v2 phases 11–18, v3 phases 19–26, token-tracking, ROADMAP_V4 phases 27–66 with all active
> phases merged, plus todo-batch follow-ups), each with a concrete way to see or verify it.
> Note: most features are backend/ops/trust plumbing with no chat-UI surface by design — they
> live at the HTTP/WS, cluster, security, and CI surfaces; this file shows where.

Legend for the "How to see / verify" column:
- 🟢 verified live in this session: exercised against the running app and the output observed (see the [Evidence log](#evidence-log) at the bottom).
- 🔵 driveable in the browser/cluster: observable by using the chat UI or a kind cluster (needs the LLM key, which is configured, and/or a cluster).
- ⚪ artifact / config: verify by rendering an artifact or reading a file; no live server needed.

---

## 0. The one-paragraph map

A local chat assistant (FastAPI backend + static chat UI over a WebSocket) that drives the
`llm-d-benchmark` CLI for non-experts: it interviews you, plans a run, checks preconditions,
deploys an llm-d stack, runs a benchmark, parses the report against the repo's schema, and
explains the numbers. Around that core sit a Kubernetes-native orchestrator, a results analyzer
(SLO/goodput/Pareto), cross-session history and trends, Prometheus observability, a
deny-by-default security command policy with per-action approval, optional CORS,
run lifecycle controls (cancel/reattach/readiness), workspace GC, a one-command Helm deploy,
and a token-usage counter with prompt caching. All judgment lives in editable `knowledge/`
files; Python is mechanism only.

---

## 1. How to launch it and see everything yourself

Launch it with `./scripts/run.sh`, then open http://127.0.0.1:8000. Full quickstart in the
[root README](../../../README.md#quick-start) / [`DEPLOYMENT.md`](../guides/DEPLOYMENT.md).

The browser chat is where the user-facing features live. The HTTP endpoints
(`/healthz`, `/readyz`, `/metrics`, `/api/sessions`, `/api/history`) carry the
operability features and are the easiest to verify with `curl`.

---

## 2. Core agent workflow (the MVP vertical)

| Feature | Where it lives | How to see / verify |
|---|---|---|
| End-to-end flow: probe → plan → standup → smoketest → run → parse report → summarize → teardown | `app/agent/loop.py`, `app/tools/*` | 🔵 In the chat: *"benchmark a small chat model on CPU"* and approve the plan (exercised in a real session). |
| **SessionPlan approval gate**: nothing mutating runs until you approve a structured plan | `app/validation/session_plan.py`, `propose_session_plan` tool | 🔵 Chat shows a plan card with Approve/Reject before any standup/run. |
| **Steering** (Claude-Code style): type a message WHILE the agent is working; it is queued and the running turn picks it up at its next step (no concurrent turn, no "please wait"). Also covers typing instead of approving at a gate (declines + steers) | `app/agent/loop.py` (drains `ctx.steer_messages` each step), `app/main.py` (queues mid-turn `user_message` + backstop), `app/ui/app.js` (composer stays usable mid-turn) | 🔵 Start a turn, then send another message before it finishes; the agent folds it in. ⚪ `tests/agent/test_ws.py::test_ws_typing_while_thinking_steers_the_same_turn`, `tests/agent/test_loop.py::test_mid_thinking_steer_extends_the_same_turn`, `tests/platform/test_concurrency.py::test_second_message_to_running_session_is_queued_as_steer`. |
| Use-case → `<spec, harness, workload>` triplet mapping (knowledge-driven, not hardcoded) | `knowledge/usecase_to_profile.yaml` | ⚪ Read the YAML; the LLM reasons over it (no `if/elif` in Python). |
| Concrete `llmdbenchmark run` argv + dry-run preview | `app/tools/run/execute.py` | 🔵 The "Executed commands" panel shows the exact argv; `--dry-run` is read-only. |
| Catalog grounding (specs/harnesses/workloads discovered from the repo, never invented) | `app/tools/setup/probe.py:list_catalog` | 🔵 `list_catalog` runs read-only at session start. |

---

## 3. Chat UI features (what you actually see in the browser)

| Feature | Where | How to see / verify |
|---|---|---|
| llm-d brand theme (the official llm-d mark, Red Hat fonts), light/dark toggle | `app/ui/index.html`, `app/ui/styles.css` | 🔵 Open the app; click the theme toggle (top-right). Persists in `localStorage`. |
| **Recent chats sidebar + resume** (Claude-web style) | `app/ui/app.js`, `GET /api/sessions`, WS `?session=<id>` | 🟢 `GET /api/sessions` returns the stored chats (observed: 100+ sessions). Click one to replay its transcript. |
| **Stored Results sidebar + metric trend sparkline** | `app/ui/index.html` (`#history`, `#trend-view`), `GET /api/history`, `/api/history/trend` | 🟢 Endpoints live. The sparkline appears once a result is stored via `result_history`; the agent proactively stores the first real run of a session as a baseline (directed by `knowledge/history.md`), so a fresh `/api/history` populates after your first benchmark. |
| **Per-run charts shown inline under the report summary** | `GET /api/sessions/{sid}/artifact`, `app/tools/analyze/report_locate.py` (`_discover_charts`), `app/ui/app.js` (`renderReportCharts`) | 🟢 After `locate_and_parse_report`, the harness's latency/throughput PNGs render as captioned images in the results card (read-only, image-only, path-traversal-hardened route). |
| **Token-usage counter** (real provider counts): a context-window chip `⛶ N ctx` (under the chat input, right-aligned on the hint row) shows the current prompt size sent to the model on the latest call (raw count, no model-limit denominator since the active model can change; persists across reloads), plus a live per-turn `↑up ↓down · N this turn (X calls · Y cached)` | `app/agent/events.py` (`usage` event → `context_window`), `app/ui/app.js` (`onUsage`/`appendTurnTokens`/`setContextWindow`) | 🔵 Visible during/after any chat turn. |
| **Model + reasoning-effort picker**: click the composer model badge to open a VSCode-style popover and switch the Anthropic model + reasoning effort for THIS chat. Per-session ephemeral override (never writes `.env` or mutates the provider singleton); effort is per-model (hidden for Haiku, clamped down on a model switch); the pick sticks in `localStorage` and re-syncs on reconnect. Agent-SDK provider only. | `app/llm/model_catalog.py` (`served_models`/`valid_selection`), `GET /api/provider` (`switchable`/`effort`/`models`), `set_model` WS frame, `app/agent/session.py` (`model_override`/`effort_override` → `open_provider_turn`), `app/ui/app.js`·`index.html`·`styles.css` | 🔵 Click the model badge → pick a model/effort → it applies to the next turn (agent-SDK provider only). ⚪ `tests/agent/test_model_picker.py`. |
| **Deterministic welcome card**: a consistent, code-emitted greeting (capability bullets + nudge) on a FRESH chat, with no LLM turn spent; never shown on resume | `knowledge/welcome.md` (judgment text), `app/agent/cards.py` (parser), `app/main.py` (`welcome` event on `not resumed`), `app/ui/app.js` (`renderWelcome`) | 🔵 Open a new chat: the welcome card + suggestion chips appear before you type. ⚪ `tests/agent/test_deterministic_msgs.py`. |
| **Structured post-run results card**: a deterministic summary (model/harness/requests + latency/throughput table + exact SLO verdicts + Pareto frontier for a sweep) built from the validated BR v0.2 summary, not LLM prose | `app/agent/cards.py`, `app/agent/loop.py` (`results_card` event after the report/analysis tool), `app/ui/app.js` (`renderResultsCard`) | 🔵 After `locate_and_parse_report` / `analyze_results` the card renders identically every run. ⚪ `tests/agent/test_deterministic_msgs.py`. |
| Animated "working" indicator (spinning llm-d mark + live status/tool name) | `app/ui/index.html` `#working`, `app/ui/app.js` | 🔵 Appears while the agent is thinking/running a tool. |
| Markdown rendering of assistant text | `app/ui/app.js` (renderer) | 🔵 Assistant replies render as formatted markdown. |
| Debug view (`>_`): show the executed commands inline in the chat | `app/ui/index.html` `#debug-toggle`, `app/ui/app.js` `addInlineCommand` | 🔵 Toggle top-right; reveals each command the agent ran inline, between the messages, in execution order (badged read-only/mutating + auto/approved). Toggle off to hide. |
| Approval cards persist across chat switches | `app/ui/app.js` | 🔵 Switch chats with a pending approval; it's still there. |
| Per-command approval for mutating actions; read-only probes auto-run. A per-session **Auto-approve** pill (bottom-left of the composer) skips the command card for the rest of the chat (the plan still asks); a command card also offers an "Approve & stop asking" button that approves it *and* flips auto-approve on in one click | `app/agent/loop.py`, `app/agent/channel.py`, `app/security/*`, `app/ui/index.html` `#autoapprove-toggle`, `app/ui/app.js` (`applyAutoApprove`/`addApprovalCard`) | 🔵 Standup/run/teardown prompt for approval; probes don't. Toggle the composer pill, or click "Approve & stop asking" on a command card, to auto-approve the rest. ⚪ `tests/agent/test_auto_approve.py`. |
| **Run progress stepper**: phased workflow rail (Pre-flight → Plan → Setup → Configure → Deploy → Benchmark → Analyze) that lights up as the agent works | `app/ui/index.html` `#run-steps`, `app/ui/app.js` (`renderRunSteps`/`advancePhase`) | 🔵 Appears once a benchmark starts; the active phase pulses. Per-chat (survives switches). Driven from the `tool_call` stream, no LLM cost. ⚪ `tests/platform/test_ui.py`. |
| **Stop button**: cancel an in-flight run from the UI (sends the `cancel` control frame; handles the `cancelled` event) | `app/ui/app.js` (`cancelRun`), `app/main.py` (Phase-16 cancel) | 🔵 Visible in the working line during a run; click to stop. ⚪ `tests/platform/test_ui.py`. |
| **Goodput gauge + binding constraint** in the results card: radial gauge of estimated goodput + the first missed SLO target | `app/ui/app.js` (`goodputGauge`, `renderResultsCard`) | 🔵 Renders in the SLO section of the results card when goodput is computed. |
| **Pareto frontier scatter** (sweeps): 2D objective-space plot with the frontier highlighted + SLO-infeasible points ringed | `app/ui/app.js` (`renderParetoCard`/`scatterPlot`) | 🔵 After `analyze_results` on a sweep. Renders from the per-run objective coordinates already on the result. |
| **A/B comparison delta bars** + **cross-harness table** | `app/ui/app.js` (`renderComparisonCard`/`deltaBar`, `renderHarnessCompareCard`) | 🔵 After `compare_reports` (direction-aware green/red deltas vs baseline) / `compare_harness_runs`. |
| **Live per-pod CPU/mem trend sparklines** in the resource side-panel | `app/ui/app.js` (`accumulateResourceHistory`/`renderResourceTrends`) | 🔵 During a run the side-panel shows rolling sparklines under the kubectl-top table. |
| **Copy buttons** on code/JSON blocks, **jump-to-latest** (reads "↓ N new messages" when messages arrive while you're scrolled up, else "↓ Latest"), **off-canvas mobile sidebar** | `app/ui/app.js`, `app/ui/styles.css` | 🔵 Hover a code block for Copy; scroll up and let new messages arrive: the jump button counts them, click to snap back; narrow the window for the hamburger. ⚪ `tests/platform/test_ui.py`. |
| **Pre-flight / status cards**: the read-only diagnostic tools render as friendly status cards instead of raw JSON: `probe_environment` → environment status grid; `check_capacity` → feasibility + diagnostics; `check_endpoint_readiness` → services/gateway/serving grid; `advise_accelerators` → CPU-only/accelerated + node table; `generate_doe_experiment` → treatment matrix; `orchestrate_benchmark_run` → outcome + per-attempt fault timeline | `app/ui/app.js` (`renderEnvStatus`/`renderCapacityCard`/`renderReadinessCard`/`renderAcceleratorCard`/`renderDoeCard`/`renderOrchestrateCard`) | 🔵 Each renders after its tool runs. ⚪ `tests/platform/test_ui.py`. |
| **Agent "what next?" suggestion buttons**: the agent offers follow-ups by CALLING `suggest_next_steps` (it chooses how many, up to 6) instead of asking in prose; they render as clickable pills (same style as the welcome chips) under its reply, and a tap sends that option's prompt (save baseline, compare, sweep…). Replay/share-safe (rides the tool result) | `app/ui/app.js` (`renderAgentSuggestions`) | 🔵 Appear under the agent's reply after it calls `suggest_next_steps` (e.g. post-`analyze_results`). ⚪ `tests/platform/test_ui.py`, `tests/agent/test_suggest_next_steps.py`. |
| **Copy-summary on results cards**: hover-reveal button copies a markdown summary (metrics + SLO table) to paste into a report/PR | `app/ui/app.js` (`resultsCardMarkdown`/`addCardCopy`) | 🔵 Hover a benchmark results card; click Copy. ⚪ `tests/platform/test_ui.py`. |
| **Guided Benchmark Builder**: a "✨ Design" wizard (header + welcome CTA) where a non-expert picks use-case / scale / token-shape / SLO targets / hardware via chips and inputs, sees a live plain-language preview, and sends it as a normal message. The agent does ALL `<scenario, harness, workload>` mapping; the form only phrases the request (thin code / thick agent) | `app/ui/index.html` `#builder`, `app/ui/app.js` (`composeBrief`/`openBuilder`/`submitBuilder`) | 🔵 Click "✨ Design", choose options, Send. ⚪ `tests/platform/test_ui.py`. |
| **Share a chat via link** (ChatGPT-style): the "🔗" header button mints a read-only public link to an *immutable snapshot*; delete revokes. `/share/<token>` serves the SPA read-only (no composer / sidebar / WebSocket) and replays the snapshot with the live renderers (token totals incl. cache + context-window, run-stage rail, inert next-step chips). The unguessable token is the only credential (no Bearer auth); pending approval gates are stripped | `app/storage/share.py` (`ShareStore`), `app/main.py` (`POST /api/sessions/{id}/share`, `GET /api/share/{token}`, `GET /share/{token}`, `DELETE /api/share/{token}`), `app/ui/index.html` `#share-dialog`, `app/ui/app.js` (`shareChat`/`bootShareView`) | 🔵 Click 🔗 on a started chat → copy the link → open it in a private window. ⚪ `tests/platform/test_share.py`, `tests/platform/test_ui.py`. |
| **UI preview harness**: drive every render path with fixture data, no backend/LLM | `app/ui/preview.html` | 🔵 Open `/static/preview.html` (or serve `app/ui/` and open `preview.html`) to see all of the above without a cluster. |

---

## 4. The agent tools (authoritative list: `app/tools/registry.py`)

> Note: `app/tools/registry.py:build_registry` is the authoritative count; the enumerated list
> below mirrors it. `run_shell` (arbitrary `bash -lc`) is the agent's always-on ad-hoc command
> tool, gated by the read-only/mutating classifier + approval, NOT the command policy.

**Sensing / grounding (read-only, auto-run):** `probe_environment`, `list_catalog`,
`inspect_workload_profile`, `estimate_run_duration`, `read_knowledge`, `search_knowledge`,
`read_repo_doc`, `fetch_key_docs`, `check_capacity`, `check_endpoint_readiness`,
`locate_and_parse_report`, `observe_run_metrics`, `discover_stack`, `advise_accelerators`.

**Planning / authoring:** `propose_session_plan`, `write_and_validate_config`,
`generate_doe_experiment`, `convert_guide_to_scenario`.

**Mutating (approval-gated):** `ensure_repos`, `run_setup`, `execute_llmdbenchmark`,
`run_shell` (arbitrary `bash -lc`; read-only commands auto-run, mutating/unknown ones prompt),
`orchestrate_benchmark_run`, `orchestrate_sweep` (parallel DoE-treatment Jobs
under a concurrency cap, with per-treatment retry/dead-letter + checkpoint/resume: the
proposal's parallel-treatment scheduling), `manage_orchestrated_runs` (list **read-only** /
stop / reap the orchestrator's K8s Jobs ON the cluster; `stop` deletes a still-running Job,
which `cancel_run` does NOT; also mirrored read-only at `GET /api/jobs`), `provision_hf_secret`.

**Analysis / history (read-only):** `compare_reports`, `compare_harness_runs`,
`analyze_results`, `aggregate_runs`, `result_history`, `cancel_run`.

**Goal-seeking (no dedicated tool):** "hit this SLO at best goodput" rides the DoE sweep
+ Pareto path: iterative `generate_doe_experiment`/`orchestrate_sweep` rounds narrowed by
`analyze_results`' SLO-feasible frontier, steered by the goal-seeking section of
`knowledge/sweep_playbook.md` (the closed-loop `autotune_search` tool was removed 2026-07-02).

**Reproducibility (read-only):** `export_run_bundle` (capture a provenance bundle: repo
SHAs + resolved config + validated report digest), `reproduce_run` (re-derive a rerun
proposal that goes back through the SessionPlan-approval + `--dry-run` gates).

**Conversation / UX (read-only, auto-run):** `suggest_next_steps` offers concrete
follow-ups (the agent chooses how many, up to 6) as clickable buttons instead of a prose
"want me to…?"; the agent's turn-ending discretionary offer (NOT an approval gate; mutations
still go through their own gates). See `knowledge/conversation_style.md`.

*How to verify each tool:* every tool has a focused test in `tests/` (e.g.
`tests/tools/test_new_tools.py`, `tests/tools/test_analyze.py`, `tests/orchestrator/test_capacity.py`,
`tests/platform/test_history.py`). In the chat, each tool call renders as a card with its inputs and
result.

---

## 5. Benchmark Orchestrator (Kubernetes-native, §3.3)

| Feature | Where | How to see / verify |
|---|---|---|
| Job-per-run / per-DOE-treatment manifest generation | `app/orchestrator/job.py:build_job_manifest` | ⚪ `pytest tests/orchestrator/test_orchestrator.py`; or `orchestrate_benchmark_run` in chat (needs a cluster). |
| Fault classification (OOM / timeout / unschedulable / evicted / image / run error) | `app/orchestrator/faults.py` (6 kinds) | ⚪ `tests/orchestrator/test_orchestrator.py`; surfaces as `llmdbench_orchestrator_run_faults_total` in `/metrics`. |
| Retry + dead-letter for transient faults; deterministic faults never retry | `controller.py:run_with_retries` | ⚪ `tests/orchestrator/test_orchestrator.py`. |
| Parallel DOE sweeps with a concurrency cap | `controller.py:run_sweep` (`asyncio.Semaphore`) | ⚪ `tests/orchestrator/test_orchestrator_tools.py`, `tests/tools/test_sweep.py`. |
| **Stateless reconstruct** from Job/pod labels | `controller.py:reconstruct` | ⚪ `tests/orchestrator/test_orchestrator.py`. |
| **Real-time pod log streaming** → live `output` events during a run (P21) | `controller.run_attempt` + `kube.stream_logs(follow=True)` | 🔵 Run `orchestrate_benchmark_run` on a cluster; logs stream into the console panel live, not just at the end. |
| **Checkpoint / resume** of long DOE sweeps via a per-sweep ConfigMap (P22) | `app/orchestrator/checkpoint.py` | ⚪ `tests/orchestrator/test_orchestrator*`; resume a sweep with the same `sweep_id` → completed treatments are skipped. |
| **Resource management**: nodeSelector / tolerations / affinity / GPU type + pod anti-affinity (P23) | `JobSpec`/`build_job_manifest` + `knowledge/resource_management.md` | ⚪ Pass `scheduling` to `orchestrate_benchmark_run`; inspect the rendered manifest in tests. |
| **Endpoint readiness gate** before submit (+ approval-gated standup suggestion) (P24) | `app/readiness/probes.py` + `diagnostics.py`, `check_endpoint_readiness` tool | ⚪ `tests/orchestrator/test_endpoint_readiness.py` + tool tests; reads `kubectl get endpoints`, refuses to submit against an unready endpoint. |
| Cleanup of terminal Jobs/ConfigMaps; results PVC preserved | `controller.py:cleanup` | ⚪ orchestrator tests. |

---

## 6. Results analysis, comparison & history (§3.4)

| Feature | Where | How to see / verify |
|---|---|---|
| Report parsing + plain-language summary (validated against repo BR-v0.2 schema) | `app/validation/report.py`, `locate_and_parse_report` | 🔵 The "Benchmark results" card in chat (seen in a real session). ⚪ `tests/tools/test_report_validation.py`. |
| SLO-aware filtering + **goodput estimate** + Pareto/DoE frontier | `app/validation/analysis.py`, `analyze_results` | ⚪ `tests/tools/test_analyze.py`. |
| A/B comparison of 2+ runs (per-metric deltas + per-metric winner) | `app/tools/analyze/compare.py` | ⚪ `tests/` (compare). |
| **Cross-harness** comparison (inference-perf vs guidellm on the same stack) | `app/tools/analyze/compare.py` (`compare_harness_runs`) | ⚪ `tests/tools/test_multiharness.py`. |
| Metric extraction incl. **KV-cache hit rate, schedule delay, GPU utilization** (P25) | `report.py`/`analysis.py` + `knowledge/standard_metrics.yaml` | ⚪ tests; `None` when a harness doesn't emit them. |
| **Cross-session result history** (`store`/`list`/`get`/`delete`) | `app/storage/history.py`, `result_history` tool, `GET /api/history` | 🟢 `GET /api/history` returns `records` + the 11 trendable `metrics`. Store one in chat to populate it. |
| **Metric trends over time** (`trend`) + sidebar sparkline | `GET /api/history/trend?metric=<m>` | 🟢 Live (see evidence). Valid metrics: `ttft, tpot, itl, request_latency, output_token_rate, total_token_rate, request_rate, success_rate_pct, kv_cache_hit_rate, gpu_utilization, schedule_delay`. |

> ✅ The harness's own PNG charts are surfaced inline. `inference-perf` writes
> `latency_vs_qps.png`, `throughput_vs_latency.png`, `throughput_vs_qps.png` into the session
> `analysis/` folder; `locate_and_parse_report` returns them as a `charts` list, the read-only
> `GET /api/sessions/{sid}/artifact` route serves the bytes (image-only, traversal-hardened), and
> the report-summary card renders them inline as captioned images alongside the text summary +
> trend sparkline.

---

## 7. Observability (§4, Phase 7 + 17)

| Feature | Where | How to see / verify |
|---|---|---|
| Prometheus metrics endpoint (agent's own counters/histograms/gauges) | `app/observability/metrics.py`, `GET /metrics` | 🟢 `curl /metrics` exposes `llmdbench_agent_commands_total`, `_command_duration_seconds`, `llmdbench_orchestrator_run_attempts_total`, `_run_faults_total`, `_runs_in_flight`, `_runs_submitted_total`, `_runs_terminal_total`. |
| Live cluster resource usage during a run (`kubectl top`) | `app/tools/run/manage_runs.py`, `observe_run_metrics` tool | 🔵 Call it while a run is in flight (needs the in-cluster metrics-server, which kind / the `cicd/kind` spec do NOT install; add it separately). |
| Per-cluster metrics-server installer (enables the live stats above) | `scripts/install/install_metrics_server.sh`, `install_metrics_server.sh` command policy exec | 🔵 `probe_environment` reports `metrics_server.available` up front (pre-flight); on kind where it is false the agent OFFERS `run_shell("install_metrics_server.sh --kubelet-insecure-tls")` BEFORE the run (mutating → approval). Judgment in `knowledge/observability.md`; rule in `app/agent/prompt.py` HARD_RULES. |
| Grafana dashboard + Prometheus scrape config + **alert rules** | `deploy/observability/{grafana-dashboard.json,prometheus-scrape.yaml,alerts.rules.yaml}` | ⚪ Files render/import directly. |

---

## 8. Security & trust

| Feature | Where | How to see / verify |
|---|---|---|
| **Deny-by-default command policy**, argv-only (`shell=False`), policy-as-data | `security/command_policy.yaml`, `app/security/policy.py` | ⚪ `tests/platform/test_command_policy.py`; `/readyz` reports "15 policy-allowed executables". |
| Read-only probes auto-run; mutating commands require UI approval | `app/agent/loop.py`, `app/security/runner.py` | 🔵 Standup prompts; probes don't. |
| **Gated-model access guardrail**: once `check_capacity` reports a model `gated:true`+`authorized:false`, any `standup`/`run`/`smoketest` of it is REFUSED at the command chokepoint (both `execute_llmdbenchmark` and the ad-hoc `run_shell`) until a later `check_capacity` clears it; the refusal nudges `provision_hf_secret`. CLI matched by basename (no path bypass); `-m`/`--models`/`--model` parsed in space- and equals-form; HF token never leaves the backend | `app/tools/run/gated_access.py`, `app/tools/setup/capacity.py` (records the verdict), `command_exec.py`/`shell.py` (chokepoints), `app/agent/prompt.py` (HARD_RULE) | ⚪ `tests/tools/test_gated_guardrail.py`, `tests/orchestrator/test_capacity_gated.py`; 🟢 live flow `error-gated-model-access`. |
| **Skill-grounding gate** (owner: this row): a mutating `llmdbenchmark` op is REFUSED until its grounding doc was fetched THIS session (`consulted_skills` ledger written by `fetch_key_docs`). Spec-aware: the kind/CPU-sim path (`--spec cicd/kind*`) requires `fetch_key_docs(task="quickstart")` (the project runbook, a `kind: knowledge` `key_docs.yaml` entry); the GPU/guide path requires the op's `*_skill` (standup→deploy_skill, run/smoketest→benchmark_skill, teardown→teardown_skill, experiment→compare_skill). Wired at the command chokepoint (`command_exec.py`) + as an early deploy gate in `propose_session_plan` (`plan.py`); `run_shell` is intentionally NOT gated; WVA autoscaling is description-driven, not gated (no command chokepoint — the agent fetches `wva_skill` when the ask is about autoscaling) | `app/tools/run/skill_gate.py`, `command_exec.py`/`plan.py` (wiring), `knowledge/key_docs.yaml` (`kind: knowledge`), `app/tools/access/knowledge_access.py` (`fetch_key_docs`) | ⚪ `tests/tools/test_skill_gate.py` (unit) + the deterministic `scripts/eval/validate_flows.py` (42/42 flows pass with the gate live); the gated live-LLM check is `tests/eval/simulate/test_skill_usage_live.py` (6 scenarios × 3 runs, majority passes). |
| Secrets stay backend-only; child-process env scrubbed | `app/config.py:child_env` | ⚪ Read `child_env`; browser never receives keys. |
| **CommandPolicy governance**: per-command timeouts (P13) | `app/security/policy.py`, `security/command_policy.yaml` | ⚪ `tests/platform/test_governance.py`. |
| Optional CORS (`CORS_ALLOW_ORIGINS`); off = no CORS headers (today's default) | `app/config.py:cors_origins_list`, `app/main.py` | ⚪ Set the env var and inspect response headers. |

---

## 9. Operability & lifecycle (roadmap v2)

| Feature | Where | How to see / verify |
|---|---|---|
| **Structured JSON logging + correlation IDs** (P11) | `app/observability/logging.py` | 🟢 Server stdout is JSON (`{"timestamp":...,"level":"INFO","logger":"app.main","message":"startup",...}`). |
| Liveness `/healthz` | `app/main.py` | 🟢 `{"ok":true}`. |
| **Readiness `/readyz` + startup self-check** (P16/P18): workspace writable, provider coherent, repos resolvable, runner ok, auth coherent | `app/main.py`, `app/storage/retention.py` | 🟢 `curl /readyz` returns the full per-check report (all green here). |
| **Run lifecycle**: cancel a run in another chat, reattach, graceful shutdown (P16) | `app/tools/run/manage_runs.py` (`cancel_run`), `app/agent/lifecycle.py` | ⚪ `tests/platform/test_run_lifecycle.py`; 🔵 `cancel_run` frees a stuck run's concurrency slot. |
| Concurrency cap on simultaneous runs | `app/agent/*`, `tests/platform/test_concurrency.py` | ⚪ `tests/platform/test_concurrency.py`. |
| **WS protocol hardening + live event buffer** (P15) | `app/agent/ws_schemas.py`, `channel.py` | ⚪ `tests/agent/test_ws.py`. |
| **Workspace retention / GC + startup cleanup** (P18) | `app/storage/retention.py` | 🟢 Startup log: `{"message":"retention.gc","removed":0,"reclaimed_bytes":0}`. |
| **Simulate Mode** (`SIMULATE=1`): walk the whole flow; read-only commands run for real, mutations are approval-gated then no-op, synthetic report | `app/config.py`, `app/tools/command_exec.py` + `app/tools/run/shell.py` (caller-gate), `app/agent/loop.py` | 🔵 Set `SIMULATE=1`, run a benchmark in chat: read-only probes/greps return real output (genuine context), every mutating command still raises its Approve/Reject card (SIMULATE previews the mutation, it does not waive the guardrail) and is then a no-op, a synthetic report is produced, no cluster touched. ⚪ `tests/tools/test_simulate.py`. |

---

## 10. Deploy & packaging (Phase 8)

| Feature | Where | How to see / verify |
|---|---|---|
| Hardened non-root container image | `Dockerfile` | ⚪ `docker build .`. |
| **Helm chart** (Deployment, Service, SA, RBAC Role/Binding, Secret) | `deploy/helm/llm-d-benchmarking-agent/` | 🟢 `helm template deploy/helm/llm-d-benchmarking-agent` renders all 6 kinds. |
| Least-privilege RBAC | `deploy/helm/llm-d-benchmarking-agent/templates/rbac.yaml` | ⚪ Inspect the rendered Role rules. |
| Single source of truth for image/port/SA across artifacts | `app/packaging/assets.py` | ⚪ `tests/platform/test_packaging.py`. |
| **In-cluster service deploy**: run the agent ITSELF as a Kubernetes service (alongside the laptop install) via a self-contained full-bake image (bundles the `llmdbenchmark` CLI + 3 sibling repos + client toolchain) + Helm | `Dockerfile` (full-bake), `scripts/install/install_service.sh` (published image by default, `--build` for local), `deploy/helm/*`, `docs/guides/CLUSTER_SERVICE_DEPLOY.md` | 🟢 Keyless end-to-end on kind PASSED via `harnesses/cluster-service-sim/run.sh` (`/healthz`+`/readyz` green, `/api/provider`, in-Pod RBAC 403 = least-privilege holds); the live-chat step needs either a Claude subscription `CLAUDE_CODE_OAUTH_TOKEN` (the default `claude-agent-sdk` path; `claude` CLI baked in) or an `ANTHROPIC_API_KEY` fallback. |

---

## 11. Quality, validation & CI

| Feature | Where | How to see / verify |
|---|---|---|
| Pytest suite (unit + integration of mechanism) | `tests/` (40+ files) | ⚪ `make test` → green (a handful of env-gated tests skip by default). |
| **Quality gates: ruff + mypy + coverage** (P14) | `pyproject.toml`, `Makefile` | ⚪ `make quality` (= `lint` + `typecheck` + `coverage`). |
| **Flow-validation harness** (hermetic walk of the whole agent flow) | `tests/flows/`, `docs/reference/VALIDATION.md` | ⚪ `make flows` / `make validate`. |
| Catalog snapshot test (guards against repo drift) | `tests/flows/catalog_snapshot.py` | ⚪ `make snapshot-catalog`. |
| **llm-d-inference-sim integration tests** (opt-in, env-gated, skipped by default) (P26) | `tests/integration/` (+ non-gating CI job) | ⚪ Enable the env gate to run against the CPU mock; hermetic sim-shaped coverage always runs. |
| CI pipeline (GitHub Actions, hermetic flow + opt-in live eval) | repo-root `.github/workflows/agent-flow-validation.yml` | ⚪ Pushes to `origin` trigger it. |

---

## 12. Knowledge base (thick-agent: all judgment lives here)

The agent's decisions are data, not Python. Verify by reading `knowledge/`:
`usecase_to_profile.yaml`, `sweep_playbook.md`, `welllit_path_advisor.yaml` (P20),
`resource_management.md` (P23), `standard_metrics.yaml` (P25), `analysis.md`,
`results_interpretation.md`, `multi_harness.md`, `capacity.md`, `orchestrator.md`,
`observability.md`, `history.md`, `run_lifecycle.md`, `key_docs.yaml`. The system prompt
inlines the core guides; `read_knowledge('<topic>')` pulls in the rest on demand.

**Upstream skills library (3rd REQUIRED read-only repo, `llm-d-skills`):** the agent grounds its
deploy/teardown/benchmark/compare/autoscale procedures in the incubation skills' canonical
`SKILL.md`s, read live via `key_docs.yaml` → `fetch_key_docs(task='*_skill')` and ENFORCED by the
skill-grounding gate (§8). Paths + repo wiring (REQUIRED status, `repo_paths`/`readyz`, the
`knowledge/` adapters) → `docs/reference/UPSTREAM_REUSE_PATHS.md`. Verify:
`fetch_key_docs(task='teardown_skill')` returns the live SKILL.md; the clone command policy +
read-only guard are pinned in `tests/platform/test_command_policy.py` (`test_git_clone_skills_allowed`)
and the root `.claude/settings.json`; every golden operation-flow grounds in its grounding doc first
and all 5 operations are exemplified, enforced hermetically by
`tests/flows/test_flow_skill_grounding.py` / `test_flow_skill_correctness.py` /
`test_corpus_skill_coverage.py` + `tests/eval/test_no_orphan_operation.py`.

**Prompt-token efficiency:** fixed prompt overhead cut ~40% (`~20.4K → ~12.3K` tok) via stripped
schema titles (`registry.py:_strip_titles`), provider-agnostic prompt caching (`app/llm/*`),
tighter replay compaction (`context_mgmt.py`: recent-window 8, threshold 20K), dropping the CLI
log tail from a successful `execute_llmdbenchmark` result (`execute.py`; the BR-v0.2 report
supersedes it), trimmed tool descriptions (`registry.py:_DESCRIPTIONS`), and on-demand tool-schema
loading by GROUP (`registry.py:_TOOL_GROUPS` setup/run/analyze/advanced; only the lean
`STARTER_KIT` is resident until `load_tools(['<group>'])`). Effort/thinking bindable via the
provider-neutral `LLM_EFFORT`/`LLM_THINKING` aliases (`config.py`). Verify: the per-turn token line
in the UI (`· Y cached`) + `pytest tests/tools/test_phase_tiered_tools.py`.

---

## Evidence log

Reference shapes captured live against a fresh server (`uvicorn app.main:app`); these are the
expected responses, not the test runs.

**Plain instance:**
```
GET /healthz   → {"ok":true}
GET /readyz    → 200 {"ready":true,"self_check":{checks:[workspace_writable, provider_coherent,
                 repos_resolvable (llm-d, llm-d-benchmark), runner_ok (policy-allowed execs)]}}
GET /metrics   → Prometheus: llmdbench_agent_commands_total, _command_duration_seconds,
                 llmdbench_orchestrator_run_attempts_total, _run_faults_total, _runs_in_flight,
                 _runs_submitted_total, _runs_terminal_total
GET /api/sessions → persisted chats (id/title/message_count)
GET /api/history  → {"records":[...], "metrics":[ttft,tpot,itl,request_latency,output_token_rate,
                    total_token_rate,request_rate,success_rate_pct,kv_cache_hit_rate,
                    gpu_utilization,schedule_delay]}
GET /api/history/trend?metric=<unknown> → graceful 200 error + available_metrics
GET /api/history/trend?metric=ttft       → {"metric":"ttft","better":"lower","points":[...]}
artifact route → real chart PNG byte-identical (image/png); ../ / non-image / unknown-session → 404
startup log → JSON {"message":"startup",...} ; {"message":"retention.gc",...}
```

**Artifacts:**
```
helm template deploy/helm/llm-d-benchmarking-agent → ServiceAccount, Role, RoleBinding, Service, Deployment, Secret  (6 kinds)
```

## Findings / caveats

No open caveats. Five early findings (orphaned harness PNG charts behind `/static`; empty trend
sparkline until a result is stored; `/healthz`+`/readyz` wrongly auth-gated; `CLAUDE.md` tool-count
drift; ambiguous latency units) were all fixed on 2026-06-02 (`1515959`, merged `3363496`).

**Counts (current).** Verified against the running app: the agent tools enumerated in §4
(authoritative: `registry.py:build_registry`; `run_shell` is the agent's always-on ad-hoc command
tool), 11 trendable history metrics (incl. `kv_cache_hit_rate`, `gpu_utilization`,
`schedule_delay`), 15 policy-allowed executables, 7 `/metrics` families. All ROADMAP_V4 active
phases (27–66) are merged.
