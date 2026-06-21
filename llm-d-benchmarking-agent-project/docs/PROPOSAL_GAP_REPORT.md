# Proposal Gap Report — what the proposal asks for vs. what's built

> **Date:** 2026-06-20 · **Source of truth:** [`llm-d-benchmarking-agent-proposal.md`](../llm-d-benchmarking-agent-proposal.md)
> (the "north star") cross-referenced against [`FEATURES.md`](../FEATURES.md), [`plan.md`](../plan.md),
> [`ROADMAP_V4.md`](../ROADMAP_V4.md), and direct code inspection (`app/`, `knowledge/`, `security/`).
>
> **Bottom line:** the proposal is **substantially implemented** — every MVP item and almost
> every stretch goal ships. What remains is **7 genuine gaps/divergences**, most of them either
> *intentional design choices* that conflict with the proposal's wording or *environment-gated*
> items that need hardware the dev box doesn't have. There is **no missing core capability** —
> the gaps are at the API-surface, infra-binding, and "course-deliverable" edges.
>
> **Addendum 2026-06-20 (HEAD `773337b`):** §7 below adds an *internal* reachability audit — code we **started but never finished wiring to the user** (distinct from §2's proposal-vs-built lens). Net change to §2: **G7 is now CLOSED** (the `orchestrate_sweep` tool merged into `main` mid-audit), and **G6 + G1 already have partial implementation sitting in the tree** — §7.1 maps exactly what exists so the remaining work is *continued, not duplicated*.
>
> **Addendum 2026-06-20 (HEAD `6626a77`) — three more gaps closed:** the §6-recommended, in-scope items are now shipped to `main`. **G6** (Grafana live integration) — a `GRAFANA_DASHBOARD_URL` config value is fed into the `resource_stats` payload, so the already-built UI panel embeds the user's own llm-d Grafana during a run. **G1** (orchestrator REST API) — the cheap read-only slice is done: `GET /api/jobs` mirrors run state for non-chat clients (the *mutating* submit/stop API stays intentionally chat-only). **T3** (§7.2) — a new `manage_orchestrated_runs` tool lists/stops/reaps cluster Jobs; `stop` actually deletes a running Job (fixing the bug where `cancel_run` left it running), and the phantom `list_orchestrated_runs` reference is gone. Tool count **36→37**. Still open: **G2/G3** (divergent-by-design), **G4** (optional viz polish), **G5** (external course deliverable), §7.2 **T1** (`graph_query`, still unmerged — deliberately deferred), and the §7.3 dead-code cleanups.

---

## 1. Verdict legend

- ✅ **IMPLEMENTED** — present and verifiable.
- 🟡 **PARTIAL** — the capability exists but not in the form/extent the proposal describes.
- 🔀 **DIVERGENT-BY-DESIGN** — deliberately built differently from the proposal (usually to satisfy a project rule the proposal didn't anticipate); the *intent* is met, the *mechanism* differs.
- ⬜ **MISSING** — not present.

---

## 2. The actual gaps (the answer to "what's missing")

| # | Proposal item | Where in proposal | Verdict | What's missing / different |
|---|---|---|---|---|
| **G1** | **Orchestrator REST API** — a programmatic HTTP API to submit / monitor / manage benchmark jobs | §3.3, §7 ("FastAPI for the orchestrator REST/WebSocket API"), §4 (API design) | 🟡 **READ-MIRROR SHIPPED 2026-06-20 (HEAD `6626a77`)** | **UPDATE:** the cheap, harmless slice from §6 is done — a thin read-only **`GET /api/jobs?namespace=…&session_id=…&sweep_id=…`** (`main.py::list_orchestrated_jobs`) mirrors run state for non-chat clients, reusing `BenchmarkOrchestrator.reconstruct()`; it never mutates and degrades softly when no cluster is reachable, so a programmatic client CAN now poll run state without driving the LLM. **Intentionally still NOT built** (agent-first thesis, §4): a *mutating* `POST /api/jobs` / per-id submit-control API — submitting and stopping stay approval-gated through the chat (`orchestrate_*` + `manage_orchestrated_runs`). _Original gap (historical):_ FastAPI served the **chat UI + session/history/artifact** endpoints only. There is **no** `POST /api/jobs`, `GET /api/jobs/{id}`, etc. Orchestration is reachable **only through the chat WebSocket** as an LLM tool-call (`orchestrate_benchmark_run`). A non-chat client cannot drive the orchestrator without going through LLM reasoning. **UPDATE 2026-06-20 (§7.1):** partial groundwork exists — `BenchmarkOrchestrator.reconstruct()` (`controller.py:255`) already rebuilds run state from cluster labels (today library-only: no tool, no route). A thin read-only `GET /api/jobs` mirror could **reuse `reconstruct()`** rather than build state-tracking from scratch; `orchestrate.py:158` already references a not-yet-existing `list_orchestrated_runs` read tool. |
| **G2** | **K8s Watch API for event-driven job monitoring** | §3.3 ("Watch API for event-driven updates"), §5.1, §4 | 🔀 **DIVERGENT** | The orchestrator **polls** (`kubectl get jobs -l run-id=<id> -o json` in an `asyncio.sleep` loop, `app/orchestrator/controller.py:watch`), it does **not** use a K8s Watch stream. Deliberate (comment: "simpler and more robust… trivially testable against a fake"). Event-driven *feel* exists at the UI layer (status callbacks → WebSocket), but the underlying cluster monitoring is poll-based. |
| **G3** | **Kubernetes Python client (official `kubernetes` or `kr8s` for async Watch)** | §7 (tech stack) | 🔀 **DIVERGENT** | No `kubernetes` / `kr8s` / `kubernetes_asyncio` import anywhere. The orchestrator shells out to **allowlisted `kubectl`** (`app/orchestrator/kube.py`). Deliberate — a Python client would bypass the deny-by-default allowlist, the approval gate, and the env scrub (project rule #5). |
| **G4** | **Configuration Explorer integration** — reuse its Pareto visualization / cost-optimal config ranking | §3.4, §5.2 | 🟡 **PARTIAL** | The **Capacity Planner** *is* reused (`app/tools/capacity.py` → `scripts/capacity_check.py` shells into the upstream `llmdbenchmark.utilities.capacity_validator.run_capacity_planner`). But the **Configuration Explorer's Pareto visualization** is **not** integrated — Pareto/DoE analysis is agent-authored (`app/validation/analysis.py`, `app/tools/compare.py`, UI scatter), not the upstream explorer's output. Intent met (capacity pre-flight + Pareto exist); the *specific upstream-viz integration* is absent. |
| **G5** | **Upstream contribution + final live GPU demo** | §5.3, §10, §6 (weeks 10–14) | ⬜ / 🟡 **NOT DONE (mostly out-of-scope-for-code)** | (a) **No upstream PR** — the agent is a standalone project in its own repo (`origin = github.com/TalBenAmii/llm-d-benchmarking-agent`); it *wraps* the upstream CLI but is not a module inside `llm-d-benchmark/`. Proposal frames this as conditional ("if quality is sufficient" / "if applicable"). (b) **GPU well-lit-path execution is advisory-only / unexercised** — 8 GPU-only paths are catalogued in `knowledge/welllit_path_advisor.yaml` and *would* submit to a real GPU cluster if configured, but only the `cicd/kind` CPU-sim path is actually exercised. The "final presentation + live demo on a lab GPU cluster" is a course deliverable, not code. |
| **G6** | **Grafana integration with llm-d's observability stack for live monitoring during runs** | §5.2 (stretch), §4 ("Integration with Prometheus/Grafana for live GPU and system metrics"), §7 ("optional Grafana dashboard integration") | ✅ **CLOSED 2026-06-20 (HEAD `6626a77`)** | **UPDATE:** the live wiring is done — a `GRAFANA_DASHBOARD_URL` config value (`config.py`, `Settings.metrics_dashboard_url`) is fed as `dashboard_url` into the `resource_stats` payload (`resource_poller.py`), so the already-built panel (`ui/app.js:1187`) embeds the user's own llm-d Grafana beside the run. It rides EVERY tick (including the metrics-server-absent one), so the dashboard shows independent of metrics-server, and covers both the orchestrate and local-CLI run paths. Knowledge (`observability.md`) + `.env.example` now document it. _Original state (historical):_ ✅ The **dashboard artifact** ships (`deploy/observability/grafana-dashboard.json` + `prometheus-scrape.yaml` + `alerts.rules.yaml`), visualizing the agent's own `/metrics` — this satisfies §7's "optional Grafana dashboard." But there is **no live wiring into llm-d's own Grafana** for during-run monitoring: the UI has a Grafana/Prometheus dashboard *slot* that only appears if the backend hands it a URL (`ui/app.js:1153`, nothing supplies one by default), and `knowledge/observability.md` only **advises** the user to point at *their own* Prometheus/Grafana if they already run the upstream `--monitoring` stack. Live during-run metrics ARE delivered — but through the agent's own UI sparklines / `observe_run_metrics` (kubectl-top), **not** Grafana. **UPDATE 2026-06-20 (§7.1):** the panel is already half-built — `ui/app.js:1187` renders the Grafana/Prometheus `<iframe>` + "Open dashboard ↗" link, but ONLY when the backend supplies `data.dashboard_url`, and nothing in `app/` ever does (the `resource_stats` payload from `resource_poller.py:82` has no such field). **Finish G6 by feeding a `dashboard_url` from config into that payload — do NOT rebuild the panel.** |
| **G7** | **Parallel DOE-treatment job scheduling with configurable concurrency** — the proposal's flagship distributed-systems feature | §3.3, §4 ("parallel job scheduling with configurable concurrency limits across a multi-node cluster"), §5.2 (stretch: "orchestrator executes treatments in parallel with configurable concurrency") | ✅ **CLOSED 2026-06-20 (HEAD 773337b)** — see §7.1 | **UPDATE 2026-06-20:** now SHIPPED as the `orchestrate_sweep` tool, merged to `main` (`registry.py:634`; `OrchestrateSweepInput` `schemas.py:468`; handler `orchestrate.py:226`) — thin wiring over `run_sweep` with `max_parallel`, checkpoint/resume via `sweep_id`, and per-treatment dead-letter, so a chat user CAN now launch a real parallel DoE sweep (requires `ORCHESTRATOR_IMAGE`). Residual sub-work (§7.2 T3) is now **CLOSED 2026-06-20 (HEAD `6626a77`)**: the `manage_orchestrated_runs` tool exposes `reconstruct`/`cleanup` and a `stop` (delete) action, `cancel_run`'s cluster-Job gap is documented + covered by `manage_orchestrated_runs(action='stop')`, and the phantom `list_orchestrated_runs` reference is fixed. _Original gap (now historical):_ The full machinery exists and is tested — `BenchmarkOrchestrator.run_sweep(max_parallel=…)` (semaphore-bounded), per-treatment Jobs, `checkpoint.py` checkpoint/resume, cross-treatment dead-letter, `reconstruct_sweep` — BUT the **only live caller is `app/tools/resilience.py:226`** (the chaos/resilience drill), which runs against a **fake in-process cluster** and is double-gated behind `CHAOS_ENABLED`. The production tool `orchestrate_benchmark_run` is **single-run only** (`OrchestrateBenchmarkInput` has `max_attempts`, no `max_parallel`/sweep fields). Real multi-treatment sweeps go through `execute_llmdbenchmark(subcommand='experiment')` → the **upstream CLI's sequential** DoE. So parallel treatment scheduling — the headline §4 distributed-systems capability — is not reachable as a real benchmark run. *(Found by the Haiku re-audit; verified: `grep run_sweep app/tools/` → only `resilience.py`.)* Related: the controller's `cleanup` and stateless `reconstruct` are real + tested but **not exposed as agent tools** either. |

---

## 3. What IS implemented (so the gaps stay in proportion)

Everything below is present and evidence-backed in `FEATURES.md` — listed compactly so the
5 gaps above aren't read as "the project is incomplete." It is not.

### MVP (§5.1) — ✅ all four
- ✅ Conversational agent, **≥3 archetypes** (chat / RAG / batch and more) → valid `<spec, harness, workload>` triplet (`knowledge/usecase_to_profile.yaml`, knowledge-driven, no `if/elif`).
- ✅ K8s orchestrator submits Jobs wrapping the CLI, monitors completion (poll, not Watch — see G2), collects the universal Benchmark Report.
- ✅ Results parser → TTFT / TBT(TPOT) / throughput / latency percentiles + **A/B comparison**.
- ✅ Driveable demo with inference-perf against a stack on Kind (CPU).

### Conversational Agent (§3.2) — ✅
- ✅ OpenAI-compatible / Claude LLM backend; structured interview (use-case, scale, token shape, QoS, infra, harness selection).
- ✅ Two outputs: concrete `llmdbenchmark run` argv **and** DoE experiment file (`generate_doe_experiment`).
- ✅ Knowledge as editable config (`knowledge/*.yaml|*.md`), not hard-coded logic (project rule #3).

### Orchestrator (§3.3) — ✅ except G2/G3, G7
- ✅ Job-per-run manifest generation, dependency/readiness gate, log streaming, result collection, single-run **retry + dead-letter**. ✅ Per-treatment manifests, **checkpoint/resume**, cross-treatment dead-letter via `orchestrate_sweep` (G7); `cleanup` + **stateless reconstruct** now exposed through the `manage_orchestrated_runs` tool and the read-only `GET /api/jobs` route (T3/G1, HEAD `6626a77`).

### Results Analyzer (§3.4) — ✅ except G4's viz
- ✅ Metric extraction incl. **KV-cache hit rate, schedule delay, GPU utilization**; **goodput** w/ SLO filtering; A/B + cross-harness compare; **Pareto/DoE** frontier; BR-v0.2-validated report + human summary.

### Stretch goals (§5.2) — ✅ (G6 closed; only G4's explorer-viz remains partial)
- ✅ DoE *generation* · ✅ parallel sweep *execution* (`orchestrate_sweep`, G7) · ✅ multi-harness · ✅ Capacity Planner pre-flight (G4: planner yes, explorer-viz no) · ✅ goodput/SLO · ✅ history + trend sparklines · ✅ Prometheus · ✅ **Grafana** live-embed during runs (G6, HEAD `6626a77`) · ✅ **well-lit-path advisor**.

### Distributed-systems concepts (§4) — ✅ except Watch (G2), G7
- ✅ Single-run job scheduling, resource management (quotas + nodeSelector/tolerations/affinity/GPU + anti-starvation pod anti-affinity — the strongest §4 area), fault tolerance, stateless *design*, clean API seams, observability (Prometheus + log streaming). 🟡 **Parallel-treatment scheduling + distributed coordination** (the semaphore/ordering/checkpoint-lock/consistency machinery) is real but library/drill-only (G7).

### Final deliverables (§5.3) — ✅ except upstream PR (G5)
- ✅ Public repo + **CI** · ✅ **Helm chart + Kustomize** · ✅ technical docs (`docs/`: ARCHITECTURE, API, DEPLOYMENT, USER_GUIDE, …) · ⬜ upstream PR · ⬜ live GPU demo (course deliverables).

### Tech stack (§7) — ✅ except K8s-client choice (G3)
- ✅ Python 3.11+ · ✅ OpenAI-compatible LLM · 🔀 kubectl-subprocess instead of k8s Python client (G3) · ✅ FastAPI · ✅ BR-v0.2 parsing · ✅ Docker + Helm · ✅ pytest + Kind + llm-d-inference-sim · ✅ Prometheus · ✅ Grafana dashboard artifact + live during-run embed via `GRAFANA_DASHBOARD_URL` (G6).

---

## 4. Why the divergences exist (not accidental omissions)

- **G2 (poll vs Watch) & G3 (kubectl vs Python client)** both trace to **project rule #5
  (deny-by-default allowlist, argv-only, `shell=False`, per-action approval)**. A long-lived
  Watch stream / Python k8s client would sit *outside* the allowlist + approval + env-scrub
  path the whole security model depends on. So these are conscious trade-offs of the
  proposal's mechanism wording against this project's stricter security posture — the
  *function* (monitor job lifecycle, react to OOM/timeout/eviction) is delivered.
- **G1 (no orchestrator REST API)** follows from the **"thin code, thick agent"** thesis
  (rule #3): the product is a *chat assistant*, so the public surface is the chat WebSocket,
  not a job REST API. The proposal envisioned a more service-like orchestrator; the build
  chose an agent-first one.
- **G4 (explorer viz)** — the upstream Configuration Explorer's *visualization* was never
  wired in because the agent renders its own Pareto/SLO cards in the browser; only the
  Capacity Planner verdict was worth shelling out for.
- **G5** — upstream PR is explicitly conditional in the proposal; the live GPU demo needs a
  GPU cluster (the dev box is WSL2 + a single 8 GB Blackwell laptop GPU — see
  `docs/GPU_CLUSTER_RUNBOOK.md`), so it stays advisory until that lands.

---

## 5. Adjacent note — the 7 deferred ROADMAP_V4 phases

These are **not** proposal line-items (they come from mining the benchmark CLI's full surface),
but they're the other "tracked-but-not-built" set, all environment-gated:
**34** WVA (OpenShift-only), **43** `--non-admin` (shared-cluster), **44** telemetry push (opt-in),
**47** cloud-upload helpers (GCS/S3), **52** multi-turn trace replay (experimental upstream),
**57/58** empty upstream placeholder docs. See `ROADMAP_V4.md` §"Remaining work".

---

## 6. Recommendation (if closing gaps is wanted)

| Gap | Effort | Worth it? |
|---|---|---|
| **G1** orchestrator REST API | Medium | ✅ **DONE (read mirror, `6626a77`)** — `GET /api/jobs` ships. The mutating submit/stop API stays intentionally chat-only (agent-first thesis). |
| **G2** Watch API | Medium | Low value — polling meets the functional need and is more testable. Document the deliberate choice (already in code comments) and move on. |
| **G3** k8s Python client | High + risky | **Don't** — it breaks the security model. Keep kubectl. |
| **G4** Config Explorer viz | Low–Medium | Optional polish; our own Pareto cards already cover the user need. |
| **G5** upstream PR / GPU demo | External | Gated on a GPU lab cluster + a maintainer review cycle; out of code-only scope. |
| **G6** Grafana live integration | Low | ✅ **DONE (`6626a77`)** — `GRAFANA_DASHBOARD_URL` feeds the dashboard slot; the during-run panel now embeds the user's own Grafana beside the agent's sparklines. |
| **G7** parallel DOE sweep execution | — | ✅ **DONE (`orchestrate_sweep`, `773337b`)** — real treatments run through the parallel/checkpoint/dead-letter controller; `manage_orchestrated_runs` (`6626a77`) adds list/stop/cleanup of the resulting Jobs. |

## 7. Internal started-but-unfinished / unreachable code (orphaned-features audit)

> **Date:** 2026-06-20 · **HEAD:** `773337b` · **Method:** 4 parallel reachability audits (tool-layer, orchestrator, UI, backend-wiring) + direct git inspection.
>
> **How this differs from §2:** §2 is *proposal vs. built*. §7 is *built vs. reachable* — code that exists in the repo but a chat user **cannot actually use** (orphaned, gated, half-wired, or committed-but-unmerged). The point of §7 is to let us **continue existing work instead of duplicating it.**

### 7.1 Cross-reference — proposal gaps (§2) that ALREADY have implementation started

This is the "common ground" between §2 and the audit. Where a §2 gap already has code in the tree, continue *that*; do not rebuild.

| §2 gap | New status from this audit | What already exists (continue, don't rebuild) |
|---|---|---|
| **G7** Parallel DOE sweep | ✅ **CLOSED** (merged mid-audit) | `orchestrate_sweep` tool is on `main` — `registry.py:634`, `OrchestrateSweepInput` `schemas.py:468`, handler `orchestrate.py:226`. Thin wiring over `run_sweep`: `max_parallel`, checkpoint/resume via `sweep_id`, per-treatment dead-letter. Needs `ORCHESTRATOR_IMAGE`. Residual sub-work → 7.2 **T3**. |
| **G6** Grafana live integration | ✅ **CLOSED (`6626a77`)** | Backend now feeds `dashboard_url` from `GRAFANA_DASHBOARD_URL` (`config.py`) into the `resource_stats` payload (`resource_poller.py`); the panel (`ui/app.js:1187`) was reused as-is, not re-authored. |
| **G1** Orchestrator REST API | 🟡 **READ MIRROR CLOSED (`6626a77`)** | `GET /api/jobs` (`main.py::list_orchestrated_jobs`) now reuses `reconstruct()` as a read-only mirror; the `manage_orchestrated_runs` tool supplies the in-chat list/stop/cleanup. The phantom `list_orchestrated_runs` reference in `orchestrate.py` is fixed. A *mutating* REST submit/stop API remains intentionally unbuilt. |

(G2 poll-vs-Watch and G3 kubectl-vs-client remain divergent-by-design; G4 explorer-viz and G5 upstream-PR/GPU-demo show no in-tree started work.)

### 7.2 Tier 1 — features a user genuinely cannot reach today

**T1. `graph_query` — a complete graphify-backed code-nav tool: built + tested, never merged.**
- Lives on branch `worktree-graphify-runtime-tool` (commit `6e8321b`); **absent from `main`**. Adds `app/tools/graph.py`, `GraphQueryInput` (`schemas.py`), a registry entry, `config.py` `graph_index_path`, an allowlisted `graphify` executable, `knowledge/graph_query.md`, and **15 tests**.
- ⚠️ The branch is based on a **stale commit** (`823ad8d`, Jun 7) far behind current `main` — it needs a **rebase** before it can merge cleanly.
- This is the partial answer to the todo question *"do we have LSP integration in python"*: not LSP, but a structured code-graph retrieval tool **was** built; it just never shipped. **Continue: rebase that branch onto `main` and merge — do not re-author the tool.**

**T2. Grafana/Prometheus dashboard panel — ✅ CLOSED (`6626a77`).** (Same as G6.) `resource_poller.py` now adds `dashboard_url` (from `GRAFANA_DASHBOARD_URL`) to the `resource_stats` payload, so the existing `ui/app.js:1187` panel embeds the user's own Grafana; absent the config it still degrades to the `kubectl top` table.

**T3. Orchestrated cluster Jobs can now be listed, stopped, and reaped — ✅ CLOSED (`6626a77`).**
- New `manage_orchestrated_runs` tool (`app/tools/manage_runs.py`): `action='list'` (read-only, via `reconstruct`), `action='stop'` (delete still-running Jobs via `delete_job`, approval-gated), `action='cleanup'` (reap terminal Jobs via `cleanup`).
- The `cancel_run`-only-stops-the-watch gap is now covered: `manage_orchestrated_runs(action='stop')` deletes the actual K8s Job (documented in `knowledge/run_lifecycle.md`).
- `cleanup()` + `reconstruct()` now have real `app/` callers (the tool + the `GET /api/jobs` route).
- The phantom `list_orchestrated_runs` reference in `orchestrate.py` is replaced with the real tool name.
- 13 new tests cover the tool, the route, and a `manage-orchestrated-runs` live-eval flow.

### 7.3 Tier 2 — dead / orphaned code (low user impact, but started-and-abandoned)

- **Dead tool input fields** (the LLM can set them; nothing happens): `export_run_bundle.session_id` (`schemas.py:828` — `build_bundle()` has no such parameter, so the promised provenance never lands) · `advise_accelerators.namespace` (`schemas.py:43` — its own description says "unused… reserved for future").
- **Dead route:** `GET /api/sessions/{sid}/bundle/{bundle_id}` raw-JSON (`main.py:569`) — the UI only ever fetches the `.html` sibling, contrary to the route's docstring.
- **Dead module:** `app/packaging/assets.py:58-77` (`required_rbac_rules`, `deploy_dir`, `helm_chart_dir`, `kustomize_base_dir`) — only its own unit test imports it; no route/tool/prompt reaches it.
- **Dead event constant:** `SESSION_PLAN = "session_plan"` (`app/agent/events.py:98`) — never emitted; the SessionPlan rides the `approval_request` event's `kind` field instead.
- **Orphan knowledge (loadable via generic index, never deliberately cued):** `knowledge/sim_integration.md` (a SIMULATE-mode honesty rule that should be cued from the injected `SIMULATE_NOTE`, `prompt.py:150`, but isn't — so it's never surfaced exactly when it matters) · `knowledge/benchmark_feature_coverage.md` (nothing routes capability questions to it).
- **Orphan dev file:** `ui/preview.html` — served at `/static/preview.html` but **linked from nowhere** (an intentional card-layout fixture driven by `window.__LLMD_PREVIEW__`; not a user feature, but unreachable from the app).

### 7.4 Tier 3 — gated off by default (intentional, operator-tunable — NOT bugs, listed for completeness)

- `run_resilience_drill` — registered but hard-refuses unless `CHAOS_ENABLED=true` (default off); even then it runs only against an in-process **fake** cluster (`_DrillKubeClient`). The full restart-durability machinery (`restart.py`, `prove_restart_recovery`) is reachable only through this gated drill.
- `run_shell` — registered only when `UNRESTRICTED_TOOLS=true` (default off): a deliberate, allowlist-bypassing power-user escape hatch.
- `orchestrate_benchmark_run` / `orchestrate_sweep` — refuse on empty `ORCHESTRATOR_IMAGE` (the `Dockerfile` + Helm/Kustomize manifests supply it in a real deploy); correct fail-loud behavior for local dev, not abandonment.

### 7.5 Ruled out (investigated, NOT findings)

`retention.py:18` "/readyz not yet present" is a **stale comment** — the route is wired (`main.py:225`). `provider.py:106 NotImplementedError` is a benign abstract base (always overridden via `open_provider_turn`). `welcome.py` / `results_card.py` "B2 / TODO #3" markers are fully rendered. Share-chat, suggestion chips, all 14 card renderers, the WS send/receive contract, and `suggest_next_steps` are all verified wired end-to-end.
