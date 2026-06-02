# FEATURES — what this project does and how to see / verify each piece

> A single, evidence-backed inventory of **every feature** built across the 122 commits on
> `main` (MVP → roadmap v1 phases 0–10 → v2 phases 11–18 → v3 phases 19–26 → token-tracking),
> with a concrete way to **see or verify** each one.
>
> **Read this first — why "the app looks unchanged":** most recent work is *backend / ops /
> trust / quality* plumbing that has **no chat-UI surface by design** (structured logging,
> auth, rate-limiting, allowlist governance, run lifecycle, workspace GC, CI quality gates).
> Only a handful of commits touched the visible chat. So "I can't see changes in the app" is
> expected — the changes are at the HTTP/WS, cluster, security, and CI surfaces, not in the
> chat bubbles. This file shows you where each one actually lives.

**Legend for the "How to see / verify" column**
- 🟢 **verified live in this session** — I ran it against the running app and observed the output (see the [Evidence log](#evidence-log) at the bottom).
- 🔵 **driveable in the browser/cluster** — observable by using the chat UI or a kind cluster (needs the LLM key, which is configured, and/or a cluster).
- ⚪ **artifact / config** — verify by rendering an artifact or reading a file; no live server needed.

---

## 0. The one-paragraph map

A local **chat assistant** (FastAPI backend + static chat UI over a WebSocket) that drives the
`llm-d-benchmark` CLI for non-experts: it interviews you, plans a run, checks preconditions,
deploys an llm-d stack, runs a benchmark, parses the report against the repo's schema, and
explains the numbers. Around that core sit a **Kubernetes-native orchestrator**, a **results
analyzer** (SLO/goodput/Pareto), **cross-session history + trends**, **Prometheus
observability**, a **deny-by-default security allowlist + per-action approval**, **optional
auth/rate-limit/CORS**, **run lifecycle** (cancel/reattach/readiness), **workspace GC**, a
**one-command Helm/Kustomize deploy**, and a **token-usage counter + prompt caching**. All
*judgment* lives in editable `knowledge/` files; Python is mechanism only.

---

## 1. How to launch it and see everything yourself

```bash
cd llm-d-benchmarking-agent-project
cp .env.example .env            # set ANTHROPIC_API_KEY or an OpenAI-compatible key (already configured here)
pip install -e .                # or: uv pip install -e .
uvicorn app.main:app --reload   # then open http://127.0.0.1:8000
```

The browser chat is where the **user-facing** features live. The HTTP endpoints
(`/healthz`, `/readyz`, `/metrics`, `/api/sessions`, `/api/history`) are where the
**operability** features live and are the easiest to verify with `curl`.

---

## 2. Core agent workflow (the MVP vertical)

| Feature | Where it lives | How to see / verify |
|---|---|---|
| End-to-end flow: probe → plan → standup → smoketest → run → parse report → summarize → teardown | `app/agent/loop.py`, `app/tools/*` | 🔵 In the chat: *"benchmark a small chat model on CPU"* and approve the plan (this is exactly the session you already ran). |
| **SessionPlan approval gate** — nothing mutating runs until you approve a structured plan | `app/validation/session_plan.py`, `propose_session_plan` tool | 🔵 Chat shows a plan card with Approve/Reject before any standup/run. |
| Use-case → `<spec, harness, workload>` triplet mapping (knowledge-driven, not hardcoded) | `knowledge/usecase_to_profile.yaml` | ⚪ Read the YAML; the LLM reasons over it (no `if/elif` in Python). |
| Concrete `llmdbenchmark run` argv + dry-run preview | `app/tools/execute.py` | 🔵 The "Executed commands" panel shows the exact argv; `--dry-run` is read-only. |
| Catalog grounding (specs/harnesses/workloads discovered from the repo, never invented) | `app/tools/probe.py:list_catalog` | 🔵 `list_catalog` runs read-only at session start. |

---

## 3. Chat UI features (what you actually see in the browser)

| Feature | Where | How to see / verify |
|---|---|---|
| llm-d brand theme (hexagon mark, Red Hat fonts), light/dark toggle | `ui/index.html`, `ui/styles.css` | 🔵 Open the app; click the theme toggle (top-right). Persists in `localStorage`. |
| **Recent chats sidebar + resume** (Claude-web style) | `ui/app.js`, `GET /api/sessions`, WS `?session=<id>` | 🟢 `GET /api/sessions` returns the stored chats (observed: 100+ sessions). Click one to replay its transcript. |
| **Stored Results sidebar + metric trend sparkline** | `ui/index.html` (`#history`, `#trend-view`), `GET /api/history`, `/api/history/trend` | 🟢 Endpoints live (see evidence). ⚠️ The sparkline only appears **after** a result is stored via the `result_history` tool — right now `/api/history` is empty, which is why you've never seen it. |
| **Token-usage counter** (real provider counts) — header chip `Σ N tokens`, live `↑up ↓down · N this turn (X calls · Y cached)` | `app/agent/events.py` (`usage` event), `ui/app.js` (`onUsage`/`appendTurnTokens`) | 🔵 Visible during/after any chat turn; the chip persists across reloads. |
| Animated "working" indicator (spinning llm-d hexagon + live status/tool name) | `ui/index.html` `#working`, `ui/app.js` | 🔵 Appears while the agent is thinking/running a tool. |
| Markdown rendering of assistant text | `ui/app.js` (renderer) | 🔵 Assistant replies render as formatted markdown. |
| Debug view (`>_`) — show only the commands the agent executed | `ui/index.html` `#debug-toggle` | 🔵 Toggle top-right; filters the transcript to executed commands. |
| Approval cards persist across chat switches | `ui/app.js` | 🔵 Switch chats with a pending approval; it's still there. |
| Per-command approval for mutating actions; read-only probes auto-run | `app/agent/loop.py`, `app/security/*` | 🔵 Standup/run/teardown prompt for approval; probes don't. |

---

## 4. The 22 agent tools (authoritative list — `app/tools/registry.py`)

> Note: `CLAUDE.md` still says "18 tools"; the registry now defines **22**. Verify by reading
> `app/tools/registry.py:build_registry`.

**Sensing / grounding (read-only, auto-run):** `probe_environment`, `list_catalog`,
`read_knowledge`, `read_repo_doc`, `fetch_key_docs`, `check_capacity`,
`check_endpoint_readiness`, `locate_and_parse_report`, `observe_run_metrics`.

**Planning / authoring:** `propose_session_plan`, `write_and_validate_config`,
`generate_doe_experiment`.

**Mutating (approval-gated):** `ensure_repos`, `run_setup`, `execute_llmdbenchmark`,
`run_command`, `orchestrate_benchmark_run`.

**Analysis / history (read-only):** `compare_reports`, `compare_harness_runs`,
`analyze_results`, `result_history`, `cancel_run`.

*How to verify each tool:* every tool has a focused test in `tests/` (e.g.
`tests/test_new_tools.py`, `tests/test_analyze.py`, `tests/test_capacity.py`,
`tests/test_history.py`). In the chat, each tool call renders as a card with its inputs and
result.

---

## 5. Benchmark Orchestrator (Kubernetes-native, §3.3)

| Feature | Where | How to see / verify |
|---|---|---|
| Job-per-run / per-DOE-treatment manifest generation | `app/orchestrator/job.py:build_job_manifest` | ⚪ `pytest tests/test_orchestrator.py`; or `orchestrate_benchmark_run` in chat (needs a cluster). |
| Fault classification (OOM / timeout / unschedulable / evicted / image / run error) | `app/orchestrator/faults.py` (6 kinds) | ⚪ `tests/test_orchestrator_faults.py`; surfaces as `llmdbench_orchestrator_run_faults_total` in `/metrics`. |
| Retry + dead-letter for transient faults; deterministic faults never retry | `controller.py:run_with_retries` | ⚪ `tests/test_orchestrator_retry.py`. |
| Parallel DOE sweeps with a concurrency cap | `controller.py:run_sweep` (`asyncio.Semaphore`) | ⚪ `tests/test_orchestrator_sweep.py`, `tests/test_sweep.py`. |
| **Stateless reconstruct** from Job/pod labels | `controller.py:reconstruct` | ⚪ `tests/test_orchestrator_controller.py`. |
| **Real-time pod log streaming** → live `output` events during a run (P21) | `controller.run_attempt` + `kube.stream_logs(follow=True)` | 🔵 Run `orchestrate_benchmark_run` on a cluster; logs stream into the console panel live, not just at the end. |
| **Checkpoint / resume** of long DOE sweeps via a per-sweep ConfigMap (P22) | `app/orchestrator/checkpoint.py` | ⚪ `tests/test_orchestrator*`; resume a sweep with the same `sweep_id` → completed treatments are skipped. |
| **Resource management** — nodeSelector / tolerations / affinity / GPU type + pod anti-affinity (P23) | `JobSpec`/`build_job_manifest` + `knowledge/resource_management.md` | ⚪ Pass `scheduling` to `orchestrate_benchmark_run`; inspect the rendered manifest in tests. |
| **Endpoint readiness gate** before submit (+ approval-gated standup suggestion) (P24) | `app/orchestrator/readiness.py`, `check_endpoint_readiness` tool | ⚪ `tests/test_readyz.py` + tool tests; reads `kubectl get endpoints`, refuses to submit against an unready endpoint. |
| Cleanup of terminal Jobs/ConfigMaps; results PVC preserved | `controller.py:cleanup` | ⚪ orchestrator tests. |

---

## 6. Results analysis, comparison & history (§3.4)

| Feature | Where | How to see / verify |
|---|---|---|
| Report parsing + plain-language summary (validated against repo BR-v0.2 schema) | `app/validation/report.py`, `locate_and_parse_report` | 🔵 The "Benchmark results" card you saw in chat. ⚪ `tests/test_report_validation.py`. |
| SLO-aware filtering + **goodput estimate** + Pareto/DoE frontier | `app/validation/analysis.py`, `analyze_results` | ⚪ `tests/test_analyze.py`. |
| A/B comparison of 2+ runs (per-metric deltas + per-metric winner) | `app/tools/compare.py` | ⚪ `tests/` (compare). |
| **Cross-harness** comparison (inference-perf vs guidellm on the same stack) | `app/tools/multiharness.py` | ⚪ `tests/test_multiharness.py`. |
| Metric extraction incl. **KV-cache hit rate, schedule delay, GPU utilization** (P25) | `report.py`/`analysis.py` + `knowledge/standard_metrics.yaml` | ⚪ tests; `None` when a harness doesn't emit them. |
| **Cross-session result history** (`store`/`list`/`get`/`delete`) | `app/storage/history.py`, `result_history` tool, `GET /api/history` | 🟢 `GET /api/history` returns `records` + the 8 trendable `metrics`. Store one in chat to populate it. |
| **Metric trends over time** (`trend`) + sidebar sparkline | `GET /api/history/trend?metric=<m>` | 🟢 Live (see evidence). Valid metrics: `ttft, tpot, itl, request_latency, output_token_rate, total_token_rate, request_rate, success_rate_pct`. |

> ⚠️ **The harness's own PNG charts are not surfaced.** `inference-perf` writes
> `latency_vs_qps.png`, `throughput_vs_latency.png`, `throughput_vs_qps.png` into the session
> `analysis/` folder, but nothing in `app/` or `ui/` references them and `/static` only serves
> `ui/`, so the browser can't reach them. The chat only shows the text summary + the trend
> sparkline. Surfacing these per-run charts is the obvious open enhancement.

---

## 7. Observability (§4, Phase 7 + 17)

| Feature | Where | How to see / verify |
|---|---|---|
| Prometheus metrics endpoint (agent's own counters/histograms/gauges) | `app/observability/metrics.py`, `GET /metrics` | 🟢 `curl /metrics` — exposes `llmdbench_agent_commands_total`, `_command_duration_seconds`, `llmdbench_orchestrator_run_attempts_total`, `_run_faults_total`, `_runs_in_flight`, `_runs_submitted_total`. |
| Live cluster resource usage during a run (`kubectl top`) | `app/tools/observe.py`, `observe_run_metrics` tool | 🔵 Call it while a run is in flight (needs metrics-server, present in `cicd/kind`). |
| Grafana dashboard + Prometheus scrape config + **alert rules** | `deploy/observability/{grafana-dashboard.json,prometheus-scrape.yaml,alerts.rules.yaml}` | ⚪ Files render/import directly. |

---

## 8. Security & trust

| Feature | Where | How to see / verify |
|---|---|---|
| **Deny-by-default allowlist**, argv-only (`shell=False`), policy-as-data | `security/allowlist.yaml`, `app/security/allowlist.py` | ⚪ `tests/test_allowlist.py`; `/readyz` reports "9 allowlisted executables". |
| Read-only probes auto-run; mutating commands require UI approval | `app/agent/loop.py`, `app/security/runner.py` | 🔵 Standup prompts; probes don't. |
| Secrets stay backend-only; child-process env scrubbed | `app/config.py:child_env` | ⚪ Read `child_env`; browser never receives keys. |
| **Allowlist governance** — per-command timeouts + usage quotas (P13) | `app/security/quota.py`, `security/allowlist.yaml` | ⚪ `tests/test_governance.py`. |
| **Optional Bearer-token auth** (`AUTH_ENABLED`/`AUTH_TOKEN`) → 401 on missing/bad (P12) | `app/security/auth.py` | 🟢 With auth on: no token → **401** + `www-authenticate: Bearer`; correct token → **200** (see evidence). |
| **Token-bucket rate limit** (`RATE_LIMIT_*`) → 429 when drained (P12) | `app/security/auth.py` (`rate_limit` dependency) | 🟢 With `RPS=1 BURST=2`: first request 200, rest **429** (see evidence). |
| Optional CORS (`CORS_ALLOW_ORIGINS`); off = no CORS headers (today's default) | `app/config.py:cors_origins_list`, `app/main.py` | ⚪ Set the env var and inspect response headers. |

---

## 9. Operability & lifecycle (roadmap v2)

| Feature | Where | How to see / verify |
|---|---|---|
| **Structured JSON logging + correlation IDs** (P11) | `app/observability/logging.py`, `logctx.py` | 🟢 Server stdout is JSON (`{"timestamp":...,"level":"INFO","logger":"app.main","message":"startup",...}`). |
| Liveness `/healthz` | `app/main.py` | 🟢 `{"ok":true}`. |
| **Readiness `/readyz` + startup self-check** (P16/P18) — workspace writable, provider coherent, repos resolvable, runner ok, auth coherent | `app/main.py`, `app/storage/retention.py` | 🟢 `curl /readyz` returns the full per-check report (all green here). |
| **Run lifecycle**: cancel a run in another chat, reattach, graceful shutdown (P16) | `app/tools/cancel.py` (`cancel_run`), `app/agent/lifecycle.py` | ⚪ `tests/test_run_lifecycle.py`; 🔵 `cancel_run` frees a stuck run's concurrency slot. |
| Concurrency cap on simultaneous runs | `app/agent/*`, `tests/test_concurrency.py` | ⚪ `tests/test_concurrency.py`. |
| **WS protocol hardening + live event buffer** (P15) | `app/agent/ws_schemas.py`, `channel.py` | ⚪ `tests/test_ws.py`. |
| **Workspace retention / GC + startup cleanup** (P18) | `app/storage/retention.py` | 🟢 Startup log: `{"message":"retention.gc","removed":0,"reclaimed_bytes":0}`. |
| **Simulate Mode** (`SIMULATE=1`) — walk the whole flow, execute nothing, synthetic report | `app/config.py`, tool handlers, `app/agent/loop.py` | 🔵 Set `SIMULATE=1`, run a benchmark in chat — every command is a no-op, a synthetic report is produced, no cluster touched. ⚪ `tests/test_simulate.py`. |

---

## 10. Deploy & packaging (Phase 8)

| Feature | Where | How to see / verify |
|---|---|---|
| Hardened non-root container image | `Dockerfile` | ⚪ `docker build .`. |
| **Helm chart** (Deployment, Service, SA, RBAC Role/Binding, Secret) | `deploy/helm/llm-d-benchmarking-agent/` | 🟢 `helm template deploy/helm/llm-d-benchmarking-agent` renders all 6 kinds. |
| **Kustomize base + overlay** | `deploy/kustomize/{base,overlays/example}/` | 🟢 `kubectl kustomize deploy/kustomize/base` renders SA, Role, RoleBinding, Service, Deployment. |
| Least-privilege RBAC | `deploy/*/rbac.yaml` | ⚪ Inspect the rendered Role rules. |
| Single source of truth for image/port/SA across artifacts | `app/packaging/assets.py` | ⚪ `tests/test_packaging.py`. |

---

## 11. Quality, validation & CI

| Feature | Where | How to see / verify |
|---|---|---|
| Pytest suite (unit + integration of mechanism) | `tests/` (40+ files) | ⚪ `make test` → **450 passed, 6 skipped** here. |
| **Quality gates: ruff + mypy + coverage** (P14) | `pyproject.toml`, `Makefile` | ⚪ `make quality` (= `lint` + `typecheck` + `coverage`). |
| **Flow-validation harness** (hermetic walk of the whole agent flow) | `tests/flows/`, `docs/VALIDATION.md` | ⚪ `make flows` / `make validate`. |
| Catalog snapshot test (guards against repo drift) | `tests/flows/catalog_snapshot.py` | ⚪ `make snapshot-catalog`. |
| **llm-d-inference-sim integration tests** (opt-in, env-gated, skipped by default) (P26) | `tests/integration/` (+ non-gating CI job) | ⚪ Enable the env gate to run against the CPU mock; hermetic sim-shaped coverage always runs. |
| CI pipeline (GitHub Actions, hermetic flow + opt-in live eval) | repo-root `.github/workflows/agent-flow-validation.yml` | ⚪ Pushes to `origin` trigger it. |

---

## 12. Knowledge base (thick-agent — all judgment lives here)

The agent's decisions are **data**, not Python. Verify by reading `knowledge/`:
`usecase_to_profile.yaml`, `sweep_playbook.md`, `welllit_path_advisor.yaml` (P20),
`resource_management.md` (P23), `standard_metrics.yaml` (P25), `analysis.md`,
`results_interpretation.md`, `multi_harness.md`, `capacity.md`, `orchestrator.md`,
`observability.md`, `history.md`, `run_lifecycle.md`, `key_docs.yaml`. The system prompt
inlines the core guides; `read_knowledge('<topic>')` pulls in the rest on demand.

**Prompt-token efficiency (token-tracking merge):** fixed prompt overhead was cut ~40%
(`~20.4K → ~12.3K`), schema `title`s are stripped (`registry.py:_strip_titles`), and
**provider-agnostic prompt caching** is wired in (`app/llm/*`). Verify via the per-turn token
line in the UI (`· Y cached`).

---

## Evidence log

Captured live against a fresh server (`uvicorn app.main:app`, this session). The runtime
observations below are the actual verification — not the test runs.

**Plain instance (port 8077):**
```
GET /healthz   → {"ok":true}
GET /readyz    → HTTP 200 {"ready":true,"self_check":{"ok":true,"checks":[
                 workspace_writable ✓, provider_coherent ✓ (openai),
                 repos_resolvable ✓ (llm-d, llm-d-benchmark),
                 runner_ok ✓ (9 allowlisted executables), auth_coherent ✓]}}
GET /metrics   → Prometheus exposition: llmdbench_agent_commands_total,
                 _command_duration_seconds, llmdbench_orchestrator_run_attempts_total,
                 _run_faults_total, _runs_in_flight, _runs_submitted_total
GET /api/sessions          → 100+ persisted chats (id/title/message_count)
GET /api/history           → {"records":[], "metrics":[ttft,tpot,itl,request_latency,
                              output_token_rate,total_token_rate,request_rate,success_rate_pct]}
GET /api/history/trend?metric=throughput → graceful 200 error: "unknown metric 'throughput'"
                              + available_metrics  (correct name is total_token_rate)
GET /api/history/trend?metric=ttft       → {"metric":"ttft","better":"lower","n":0,"points":[]}
startup log    → JSON: {"message":"startup","provider":"openai"} ; {"message":"retention.gc",...}
```

**Auth/rate-limit instance (port 8078, `AUTH_ENABLED=true AUTH_TOKEN=s3cret RATE_LIMIT_ENABLED=true RATE_LIMIT_RPS=1 RATE_LIMIT_BURST=2`):**
```
GET /api/sessions (no token)            → HTTP 401 {"detail":"missing or invalid bearer token"}
                                           header: www-authenticate: Bearer
GET /api/sessions (Bearer s3cret)       → HTTP 200
GET /healthz (no token)                 → HTTP 401   (health is gated too — see findings)
6× rapid authed GET /api/sessions       → 200, 429, 429, 429, 429, 429
```

**Artifacts:**
```
kubectl kustomize deploy/kustomize/base                 → ServiceAccount, Role, RoleBinding, Service, Deployment
helm template deploy/helm/llm-d-benchmarking-agent      → + Secret  (6 kinds total)
```

## Findings / caveats

- ⚠️ **Harness PNG charts are orphaned** (§6): real per-run charts are generated but never
  served or shown. This is the "results visualization" gap — the only visualization the app
  surfaces is the cross-session trend sparkline.
- ⚠️ **The trend sparkline is invisible until you store a result.** `/api/history` is empty,
  so the sidebar shows "No stored results yet." Use the `result_history` tool (`store`) after a
  run to populate it.
- ⚠️ **`/healthz` and `/readyz` are gated by auth** when `AUTH_ENABLED=true` (both returned 401
  without a token). Kubernetes liveness/readiness probes would need to carry the bearer token,
  or these routes should be exempted. Worth a decision.
- **Doc drift:** `CLAUDE.md` says "18 tools"; the registry has **22**.
- **Reported run units looked off:** the earlier chat narration showed "nanosecond" TPOT/ITL —
  a unit-labeling issue in the summary text, not a data problem. Separate from this inventory.
</content>
</invoke>
