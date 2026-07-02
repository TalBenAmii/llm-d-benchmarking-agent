# INTERACTIVE TEST GUIDE — drive every feature & flow by hand (real LLM)

> A follow-along runbook for **manually** exercising every feature in this app with a **real
> LLM** driving the agent. Companion to [`FEATURES.md`](FEATURES.md) (the *inventory*); this is
> the *do-it-yourself* script. Check each box as you go.
>
> **What "real LLM" changes:** the agent's *judgment* (which tools to call, which spec/harness/
> factors to pick) is now the live model, not a scripted transcript. Command *execution* is a
> separate axis — you choose `SIMULATE=1` (mutating commands no-op, read-only run for real, no
> cluster) or real (kind cluster).

---

## Two tracks

Most features are reachable **without a cluster**. Only the actual deploy/benchmark/orchestrator
execution needs kind. So run the guide in two passes:

- **Track A — Real LLM + `SIMULATE=1` (no cluster).** Covers the entire agent flow, DOE
  generation + sweep wiring, all HTTP/ops/security/observability surfaces, the whole chat UI.
  The agent *drives* everything; commands are no-op'd and a synthetic report is produced.
  **Do this first — it exercises ~90% of the app.**
- **Track B — Real LLM + `SIMULATE=0` (live kind cluster).** Only for the things that must
  truly execute: real standup/run/teardown, real Benchmark Report numbers, the orchestrator
  Job lifecycle, live log streaming, `kubectl top`. Needs Docker + a kind cluster (the agent
  can bootstrap both, approval-gated).

Each step below is tagged **[A]**, **[B]**, or **[A/B]**.

---

## 0. One-time setup

```bash
cd llm-d-benchmarking-agent-project
cp .env.example .env          # if you don't already have one
```

Edit `.env` for a **real LLM** (pick ONE provider):

```ini
# Anthropic
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...          # your real key
ANTHROPIC_MODEL=claude-opus-4-8

# …or OpenAI-compatible
# LLM_PROVIDER=openai
# OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o

SIMULATE=1                            # Track A. Set 0 for Track B.
HOST=127.0.0.1
PORT=8000
```

> The LLM model here is the *agent's brain*. It is **separate** from the model being
> benchmarked (the quickstart benchmarks a CPU-sim engine). The key never leaves the backend.

Install + launch:

```bash
./run.sh                  # sets up venv, installs, starts uvicorn (reads HOST/PORT from .env)
# or manually:  pip install -e .  &&  uvicorn app.main:app --reload
```

- [ ] **[A/B]** Server is up — open **http://127.0.0.1:8000** and the chat UI loads.
- [ ] **[A/B]** Startup log on stdout is **JSON** (e.g. `{"timestamp":...,"message":"startup","provider":"anthropic"}`) and includes a `retention.gc` line. *(§9 structured logging + GC)*

Keep a second terminal open for the `curl` checks below.

---

## 1. Ops / HTTP surface smoke test (do this before chatting)

These prove the operability layer in seconds and confirm the LLM provider is wired.

```bash
curl -s localhost:8000/healthz                       # liveness
curl -s localhost:8000/readyz        | jq .          # readiness + self-check
curl -s localhost:8000/metrics       | head -20      # Prometheus exposition
curl -s localhost:8000/api/sessions  | jq '.[0]'     # persisted chats
curl -s localhost:8000/api/history   | jq .          # result history + trendable metrics
curl -s 'localhost:8000/api/history/trend?metric=ttft' | jq .
```

- [ ] **[A/B]** `/healthz` → `{"ok":true}`.
- [ ] **[A/B]** `/readyz` → `ready:true` with all self-checks green: `workspace_writable`,
  `provider_coherent` (shows your provider), `repos_resolvable` (llm-d, llm-d-benchmark),
  `runner_ok` (N allowlisted executables), `auth_coherent`. *(§9)*
- [ ] **[A/B]** `/metrics` exposes `llmdbench_agent_commands_total`, `_command_duration_seconds`,
  `llmdbench_orchestrator_run_attempts_total`, `_run_faults_total`, `_runs_in_flight`,
  `_runs_submitted_total`. *(§7)*
- [ ] **[A/B]** `/api/history` returns `records` + the 8 trendable `metrics`
  (`ttft, tpot, itl, request_latency, output_token_rate, total_token_rate, request_rate, success_rate_pct`). *(§6)*
- [ ] **[A/B]** `trend?metric=throughput` → graceful `200` error naming the valid metrics (the
  real name is `total_token_rate`) — confirms input validation, not a crash. *(§6)*

---

## 2. Chat UI features (visible in the browser)

Before running a flow, eyeball the static UI features. *(§3)*

- [ ] **[A/B]** **Theme toggle** (top-right) flips light/dark; reload → choice persists (`localStorage`).
- [ ] **[A/B]** **Recent chats sidebar** lists prior sessions; clicking one **replays its transcript**.
- [ ] **[A/B]** **Debug view** toggle (`>_`, top-right) filters the transcript to just executed commands.
- [ ] **[A/B]** **Context-window chip** under the chat input, right-aligned on the hint row (`⛶ N ctx`) is present (it updates once you chat — current prompt size sent to the model). *(§12)*

You'll confirm the dynamic ones (working spinner, markdown, approval cards, per-turn token line,
inline charts) during the flows below.

---

## 3. Core agent flow — the MVP vertical *(§2)*

In the chat, type a plain-language goal:

> **"Benchmark a small chat model on CPU using the kind quickstart."**

Then follow the agent. Watch the tool-call cards and the "Executed commands" panel.

- [ ] **[A/B]** Agent **interviews** you (use case, model size, concurrency) instead of guessing.
- [ ] **[A/B]** Read-only **sensing tools auto-run** (no approval): you see `probe_environment`,
  `list_catalog`, `read_knowledge`, `read_repo_doc` cards. *(§2 catalog grounding)*
- [ ] **[A/B]** Agent proposes a **SessionPlan card** with `<spec, harness, workload>` (expect
  `cicd/kind` + `inference-perf` + `sanity_random.yaml` for the quickstart) and **waits for
  Approve/Reject**. Nothing mutating runs before you approve. *(§2 determinism gate)*
- [ ] **[A/B]** **Working indicator** (spinning llm-d mark + live tool name) shows while it thinks.
- [ ] **[A/B]** **Per-turn token line** appears under the turn: `↑up ↓down · N this turn (X calls · Y cached)`. *(§12)*
- [ ] **[A]** Approve the plan. Each mutating step (ensure_repos / setup / standup / run /
  teardown) appears as an **approval card showing the exact argv**. Approve them. In SIMULATE,
  the command panel shows `[simulate] (no-op) would run: …` and `exit_code=0` — **no cluster touched**. *(§9 simulate)*
- [ ] **[A]** Flow completes with a **synthetic Benchmark results card** (parsed report summary). *(§6)*
- [ ] **[B]** With `SIMULATE=0`: the agent offers to install Docker + kind and `kind create
  cluster` (each approval-gated), then really stands up the stack and runs — the results card
  shows **real** BR v0.2 numbers, and **inline latency/throughput PNG charts** render under the
  summary. *(§6 charts)*

> **Verify the read-only/mutating boundary directly:** open Debug view — every command carries a
> read-only or **mutating** badge; only mutating ones were gated. *(§8)*

---

## 4. DOE / sweep feature — generate a matrix and drive a sweep *(§5, §6)*

This is the Design-of-Experiments path. Two shapes — try both. Read
[`knowledge/sweep_playbook.md`](knowledge/sweep_playbook.md) first if you want to predict the
agent's choices.

### 4a. Run-parameter sweep (preferred on kind — one standup, N runs)

> **"I want to see how latency scales with load. Sweep max-concurrency over 8, 16, and 32
> against one stack, then compare the results."**

- [ ] **[A/B]** Agent elicits **token characteristics** (input/output length, prefix reuse,
  concurrency) before designing the grid. *(§12 sweep_playbook)*
- [ ] **[A/B]** It reads repo truth (`read_knowledge("sweep_playbook")`, `read_repo_doc(...)`)
  to pick **real override keys** rather than inventing them.
- [ ] **[A/B]** It calls **`generate_doe_experiment`** (auto-runs — only writes the workspace).
  In the result card verify: `generated:true`, `valid:true`, `n_run_treatments` matches your
  grid (3), `validated_against_examples` is **non-empty** (validated against the repo's real
  experiment YAMLs), and a workspace `path`.
- [ ] **[A/B]** It then calls `execute_llmdbenchmark(subcommand="run", flags={experiments:<path>, dry_run:true})`
  — the **read-only preview** of the actual CLI invocation with your generated file. *(§5 always-preview rule)*
- [ ] **[A]** It proposes the real (non-dry-run) sweep → **approval card** → approve → SIMULATE
  no-ops each treatment.
- [ ] **[A/B]** Finally it calls **`compare_reports`** / **`analyze_results`** on the output dir
  and reports per-metric deltas vs a baseline (synthetic numbers under SIMULATE). *(§6)*

**Watch for the harness/key gotcha:** `max-concurrency` is a **vllm-benchmark** field, but the
quickstart default harness is `inference-perf` (whose load knob is `rate`/QPS). A good agent
either sets `harness="vllm-benchmark"` or switches the key to `rate`. If it blindly emits
`max-concurrency` against inference-perf, that's a real finding worth noting.

### 4b. Full DoE (the deployment itself changes — standup+teardown per treatment)

> **"Find the best prefill/decode split. Sweep decode.replicas over 1 and 2 and prefill.replicas
> over 1 and 2, keeping the model and workload fixed."**

- [ ] **[A/B]** Agent uses **`setup_factors`** (not just run factors) → generated matrix is
  `setup × run` treatments (here 2×2 = 4), `subcommand="experiment"`.
- [ ] **[A/B]** It warns/keeps the matrix small (full DoE re-deploys per setup treatment; the
  playbook says prefer a run-parameter sweep on a single kind cluster).
- [ ] **[A/B]** **Open the generated file** (the `path` from the tool card, under
  `workspace/sessions/<id>/…`) and confirm the shape: top-level `experiment` / `design` /
  `setup` / `treatments`, one named treatment per cross-product cell, **no** top-level `run:` key.

> Quick non-UI sanity check of the same mechanism (hermetic, no LLM, no cluster):
> `.venv/bin/python -m pytest tests/test_doe.py tests/test_sweep.py -q` → covers the
> cross-product generator, the tool, and validation against the repo's real experiment YAMLs.

---

## 5. Analysis, comparison & history *(§6)*

After a run/sweep exists in the session:

- [ ] **[A/B]** Ask: **"Compare those runs and tell me the best config for low latency."** →
  `compare_reports` returns per-metric deltas + winner; the agent ties the recommendation to the
  goal (low TTFT/TPOT for interactive).
- [ ] **[A/B]** Ask: **"Which configs are Pareto-optimal? Use a TTFT SLO of 200ms."** →
  `analyze_results` with goodput / SLO filtering / Pareto frontier.
- [ ] **[A/B]** Ask: **"Store this run as my baseline."** → `result_history` stores it; then
  `curl /api/history` shows the record, and the **Stored Results sidebar + trend sparkline**
  populate in the UI. *(§6 — sparkline is empty until the first store, by design.)*
- [ ] **[A/B]** `curl 'localhost:8000/api/history/trend?metric=ttft'` now returns points.
- [ ] **[B]** (multi-harness) Run a second harness against the same stack
  (`inference-perf` + `guidellm`), then ask to **compare across harnesses** → `compare_harness_runs`. *(§6)*

---

## 6. Security & trust surfaces *(§8)*

The allowlist/approval behavior you already saw in §3. The optional trust controls need a
restart with env flags — easiest in a **separate instance** so your main one stays usable:

```bash
AUTH_ENABLED=true AUTH_TOKEN=s3cret RATE_LIMIT_ENABLED=true RATE_LIMIT_RPS=1 RATE_LIMIT_BURST=2 \
  PORT=8078 .venv/bin/uvicorn app.main:app
```

- [ ] **[A/B]** `curl -i localhost:8078/api/sessions` (no token) → **401** + `www-authenticate: Bearer`.
- [ ] **[A/B]** `curl -i -H 'Authorization: Bearer s3cret' localhost:8078/api/sessions` → **200**.
- [ ] **[A/B]** `curl -i localhost:8078/healthz` (no token) → **200** (liveness/readiness are
  **exempt** from auth so a kubelet can probe them). `/readyz` likewise. *(FEATURES Findings: fixed)*
- [ ] **[A/B]** Fire 6 rapid authed requests → first `200`, rest **429** (token bucket drained). *(§8)*
- [ ] **[A/B]** **Secrets never reach the browser:** open DevTools → Network/WS; confirm no API
  key appears in any frame (the WS only carries chat text, events, approvals). *(§8)*
- [ ] **[A/B]** (CORS) Restart with `CORS_ALLOW_ORIGINS=https://app.example.com` and confirm the
  response carries `access-control-allow-origin`; empty (default) → no CORS headers. *(§8)*

Stop this instance when done (`Ctrl-C`).

---

## 7. Observability & lifecycle *(§7, §9)*

- [ ] **[A/B]** After running flows, `curl /metrics` again — `llmdbench_agent_commands_total`
  has **incremented**; histograms have observations. *(§7)*
- [ ] **[A/B]** Server stdout stays structured JSON throughout; each request/turn carries a
  **correlation id**. *(§9 logging)*
- [ ] **[A/B]** **Reattach/resume:** start a turn, reload the browser mid-turn (or open the
  session in a new tab) — the running turn (and any pending approval card) **survives**, routed
  to the new socket. *(§9 WS hardening, §3 approval persistence)*
- [ ] **[B]** **Cancel a run:** while a real run is in flight, open another chat and ask to
  **cancel it** (or use the cancel control) → `cancel_run` frees the concurrency slot and reaps
  the subprocess/Job. *(§9 run lifecycle)*
- [ ] **[B]** **`observe_run_metrics`:** ask **"show live cluster resource usage"** during a run
  → `kubectl top` output (needs the in-cluster metrics-server, which kind / `cicd/kind` do NOT
  install — add it separately). *(§7)*
- [ ] **[A/B]** **Workspace GC:** the startup `retention.gc` log line proves the pass ran;
  caps are honored and an active session is never pruned. *(§9)*

---

## 8. Orchestrator (Kubernetes-native) — Track B *(§5)*

These need a real cluster. Ask the agent to use the orchestrator path:

> **"Run this benchmark as a Kubernetes Job via the orchestrator, and stream the logs."**

- [ ] **[B]** `orchestrate_benchmark_run` submits a **Job** (per-run / per-DOE-treatment manifest).
- [ ] **[B]** **Live pod log streaming** — logs appear in the console panel **during** the run,
  not just at the end. *(§5 P21)*
- [ ] **[B]** **Endpoint readiness gate** — if the target endpoint isn't ready, `check_endpoint_readiness`
  refuses to submit and suggests an (approval-gated) standup. *(§5 P24)*
- [ ] **[B]** **Resource management** — pass `scheduling` (nodeSelector/tolerations/affinity/GPU
  type) and confirm it lands in the rendered manifest. *(§5 P23)*
- [ ] **[B]** **Checkpoint/resume** — start a multi-treatment sweep, interrupt it, resume with the
  same `sweep_id` → completed treatments are **skipped** (per-sweep ConfigMap). *(§5 P22)*
- [ ] **[B]** **Fault classification + retry** — a transient fault (e.g. unschedulable) retries;
  a deterministic one (e.g. image error) goes straight to dead-letter. Surfaces as
  `llmdbench_orchestrator_run_faults_total` in `/metrics`. *(§5)*

> No cluster handy? The orchestrator mechanism is fully covered hermetically:
> `.venv/bin/python -m pytest tests/test_orchestrator*.py tests/test_sweep.py -q`.

---

## 9. Deploy & packaging artifacts (no server needed) *(§10)*

```bash
helm template deploy/helm/llm-d-benchmarking-agent | grep '^kind:' | sort | uniq -c
docker build -t llmd-agent:test .                  # optional: hardened non-root image
```

- [ ] **⚪** Helm renders **6 kinds**: Deployment, Service, ServiceAccount, Role, RoleBinding, Secret.
- [ ] **⚪** RBAC is **least-privilege** (inspect the Role rules). *(§10)*

---

## 10. The `llm-d-inference-sim` integration (optional) *(§11, P26)*

A real lightweight CPU inference server (distinct from `SIMULATE`). Opt-in, skipped by default:

```bash
LLMD_SIM_INTEGRATION=1 .venv/bin/python -m pytest tests/integration/ -v
```

- [ ] **⚪** With the sim binary/image present, the integration tests run end-to-end against it;
  otherwise they **skip cleanly** (never hang).

---

## 11. Belt-and-suspenders: the hermetic suite & live eval

Independent of manual driving, you can confirm nothing regressed:

```bash
make test            # full hermetic suite (no LLM/cluster) — expect ~all pass, a few skips
make quality         # ruff + mypy + coverage
make flows           # hermetic walk of the whole agent flow (scripted provider)
```

- [ ] **⚪** `make test` green.
- [ ] **[A/B] Live agent eval (real LLM, no cluster):** does the model *choose* the right
  commands from natural language, across the canonical flows?
  ```bash
  LLM_EVAL_LIVE=1 .venv/bin/python -m pytest tests/flows/test_flows_live.py -v
  # or: make validate-live
  ```
  Treat failures as **signal** (a prompt/knowledge gap or a genuinely wrong choice), not a hard
  build break — a live model is nondeterministic.

> **What the live eval now covers:** beyond the deploy/benchmark vertical (§3), the flow set in
> `tests/flows/flows.py` includes **tool-choice flows** for the rest of the surfaces, each scored
> on the tool the model picks from natural language (a `required_tools` hint, the analogue of
> `required_subcommands` for non-`llmdbenchmark` tools):
> - **§4 DoE/sweep** — `doe-run-sweep` (run-parameter sweep) and `doe-full-experiment` (full DoE
>   with setup factors) → `generate_doe_experiment`.
> - **§5/§6 analysis & history** — `analyze-slo-pareto` → `analyze_results`; `compare-ab-runs` →
>   `compare_reports`; `result-history-baseline` → `result_history`; `multi-harness-compare` →
>   `compare_harness_runs`; `capacity-preflight` → `check_capacity`.
> - **§7/§8 orchestrator & lifecycle** — `orchestrate-k8s-job` → `orchestrate_benchmark_run`;
>   `endpoint-readiness-gate` → `check_endpoint_readiness`; `observe-live-usage` →
>   `observe_run_metrics`; `cancel-stuck-run` → `cancel_run`.
>
> These run in the SAME hermetic sandbox as §3 (no cluster/repos), so they score the model's
> *choice*, not real numbers — the manual click-throughs above still own end-to-end execution.
> Each is also replayed deterministically (a golden transcript) by `make test`, so the loop +
> approval gating are CI-gated even though the live scoring is opt-in.

---

## Coverage map (every FEATURES.md section → where it's exercised here)

| FEATURES.md § | Exercised in |
|---|---|
| §2 Core agent workflow | §3 |
| §3 Chat UI | §2, §3 |
| §4 The agent tools | §3–§5, §8 (each renders as a card) |
| §5 Orchestrator | §8 (Track B) / hermetic fallback |
| §6 Analysis & history | §4, §5 |
| §7 Observability | §1, §7 |
| §8 Security & trust | §3, §6 |
| §9 Operability & lifecycle | §1, §7 |
| §10 Deploy & packaging | §9 |
| §11 Quality & CI | §11 |
| §12 Knowledge base / tokens | §2, §3 (token line), read `knowledge/` |

---

### Quick reference — what needs what

| Capability | Real LLM | SIMULATE=1 ok | Needs cluster |
|---|:--:|:--:|:--:|
| Interview → plan → approval gate | ✅ | ✅ | ❌ |
| DOE generation + sweep wiring (preview) | ✅ | ✅ | ❌ |
| Real benchmark numbers + inline charts | ✅ | ❌ | ✅ |
| Orchestrator Job lifecycle / log stream | ✅ | ❌ | ✅ |
| HTTP ops / auth / rate-limit / metrics | — | ✅ | ❌ |
| Deploy artifacts (helm/docker) | — | — | ❌ |
| Hermetic suite / flows | — | — | ❌ |
