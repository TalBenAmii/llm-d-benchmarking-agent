# Observability (Prometheus metrics + live run metrics)

This agent exposes two complementary observability surfaces. Use them to *explain what is
happening*, not to make decisions silently — surface the signal to the user and reason about
it out loud.

## Operative summary (this file is long — read_knowledge may truncate the tail below; these are the load-bearing calls)
- **You CAN deploy the live-view stacks yourself** — both are approval-gated `run_shell` steps,
  not "advice only". The **Grafana** observability stack (richer: GPU/latency/throughput/history) and
  the **metrics-server** (§2, live CPU/mem sparklines) are each installable BY the agent; never refuse
  to stand up Grafana. The Grafana-vs-metrics-server choice + embedding a user's own Grafana →
  read_knowledge('observability_grafana').
- **Default benchmark monitoring ON** (§3) so the report's `results.observability` block is populated
  (KV-cache/GPU/queue). Opt out (`monitoring: false`) only on a truly Prometheus-CRD-less cluster.
  Full detail (the flag mechanism, the CRD-probe decision, reading the metrics) →
  read_knowledge('observability_monitoring').
- **Distributed tracing is CONFIG-ONLY** (§4): the agent configures the `tracing:` block so pods
  *emit* OTLP spans, but it CANNOT show traces — the user views them in their own Jaeger/Tempo.
  Full detail → read_knowledge('observability_tracing').
- **Live metric streaming + custom PromQL are UPSTREAM-UNIMPLEMENTED** (§5): don't promise a live
  metric chart; offer `observe_run_metrics` (kubectl top) + live pod-log streaming instead.
  Full gap statement + the exact answer to give → read_knowledge('observability_streaming').

## 1. The agent's own metrics — `/metrics` (Prometheus)

The backend exports its own counters/gauges in Prometheus text format at the HTTP endpoint
`/metrics`. This is for the *operator* (and the optional Grafana dashboard), not something you
call as a tool. Point a Prometheus scrape at it (see
`deploy/observability/prometheus-scrape.yaml`) and import
`deploy/observability/grafana-dashboard.json` to visualize. What is exported:

- `llmdbench_agent_commands_total{exe,mode,auto_run}` — every command the agent executed,
  by executable, read-only/mutating mode, and whether it auto-ran or was approval-gated.
- `llmdbench_agent_command_duration_seconds{exe,mode}` — a histogram of command durations.
- `llmdbench_orchestrator_runs_submitted_total` — benchmark Jobs submitted.
- `llmdbench_orchestrator_run_attempts_total{phase}` — Job attempts reaching a terminal phase.
- `llmdbench_orchestrator_runs_terminal_total{outcome}` — runs that finished, by
  `succeeded` / `dead_lettered`.
- `llmdbench_orchestrator_run_faults_total{kind}` — classified faults by kind
  (`oom` / `timeout` / `unschedulable` / `evicted` / `image_error` / `run_error` / `unknown`).
- `llmdbench_orchestrator_runs_in_flight` — runs currently being watched (a live gauge).

If a user asks "how many runs failed and why?" or "is anything running right now?", these
metrics — not log scraping — are the source of truth. They reset on a backend restart (they are
process-lifetime counters), so frame them as "since the agent started".

## 2. Live system metrics during a run — `observe_run_metrics`

`observe_run_metrics` reads the cluster's **live CPU/memory usage** via `kubectl top` (which
reads the in-cluster metrics-server). It is read-only and auto-runs. Use it **while a benchmark
is running** to watch resource pressure on the model server / harness pods.

- `scope="pods"` (default) + a `namespace`: usage for every pod in that namespace. Add
  `run_id` to narrow to one orchestrated run, or `containers=true` to break a pod down per
  container (so you can tell the model-server container from the load-generator).
- `scope="nodes"`: node-level usage — use to answer "is the whole node saturated?" (a common
  cause of `unschedulable` / `evicted`).

### Reading the numbers (this is your judgment — the tool only reports facts)

- **Memory near the pod's limit** (e.g. the model server sitting at ~90%+ of its `memory`
  request/limit) is the leading indicator of an imminent **OOM kill**. Call it out *before* it
  crashes: suggest a lighter workload/profile, a smaller model, or (if the user controls the
  spec) a larger memory limit. This ties directly to the `oom` fault in
  `knowledge/orchestrator.md` — observing it live lets you pre-empt that failure.
- **CPU pinned at the limit** on a CPU-only/sim run (the `cicd/kind` quickstart path) means the
  engine is throttled; throughput and latency numbers will reflect a CPU-bound run, not the
  model. Mention that when interpreting the Benchmark Report so the user doesn't over-read the
  results.
- **A node at/over ~100% CPU or memory** explains scheduling failures and evictions. If a run
  was `unschedulable` or `evicted`, check `scope="nodes"` to confirm node pressure.
- `available: false` means the metrics-server is not installed/ready in the cluster. It is NOT
  installed by kind or the `cicd/kind` spec — add it to the cluster separately (on kind, with
  `--kubelet-insecure-tls`). Relay that plainly — it is not an
  error you caused, and nothing was changed (the probe is read-only). Fall back to
  `probe_environment` / pod status if you need a coarse health signal.

Do not invent or estimate utilization numbers. If `observe_run_metrics` returns no rows, say
so; never fabricate a percentage.

### Making live stats work — installing metrics-server (per-cluster, approval-gated)

The metrics-server is a **per-cluster** add-on, not a per-spec/per-guide one: install it ONCE
on a given cluster and *every* run on that cluster gets live stats (the `cicd/kind` quickstart,
`guides/optimized-baseline`, anything). It is NOT bundled with kind, the `cicd/kind` spec, or
any llm-d guide, so on a fresh kind cluster live stats are unavailable until you add it.

When the up-front `probe_environment` `metrics_server` fact (or `observe_run_metrics` / the live
sparklines mid-run) reports `available: false` AND the user wants live resource stats, OFFER to
install it as its OWN approval-gated step, and surface that offer BEFORE you offer to deploy/standup
or submit the `run` — never after, never as a mid-run action. It is a real, actionable offer the
user approves THEN: do not frame it as optional "I'll do it after" / "for future runs", do not do it
silently (it is mutating and approval-gated), and do not submit the deploy or the run in the same
turn — STOP and let the user decide on the install first. The vetted installer is a project script
run via `run_shell`:

- **kind / any self-signed-kubelet cluster** (the quickstart path):
  `run_shell("install_metrics_server.sh --kubelet-insecure-tls")`
  The `--kubelet-insecure-tls` flag is REQUIRED on kind — without it the metrics-server pod
  fails its TLS handshake to the kubelet and never becomes Ready.
- **a normal cluster with proper kubelet certs**:
  `run_shell("install_metrics_server.sh")` (pin a release with `--version vX.Y.Z`).

The script applies the pinned metrics-server manifest into `kube-system`, waits for the rollout,
and verifies `kubectl top` responds. It is idempotent (safe to re-run). Best timing: right after
the cluster is created — and in any case BEFORE you offer to deploy/standup or submit the first
`run`, so the run already has live stats — keyed off the `probe_environment`
`metrics_server.available == false` fact.

**When to SKIP installing it (judgment, probe first):** some clusters already serve the Metrics
API — managed Kubernetes like **GKE** ships metrics-server by default, and **OpenShift** has its
own monitoring stack (`oc adm top` / cluster monitoring) rather than the upstream metrics-server.
Probe with `observe_run_metrics` first: if it already returns `available: true`, do nothing. Only
offer the install when it is genuinely absent and the user actually wants the live view. (See
`knowledge/infra_providers` for per-provider differences.)

### Two live-view options to offer before a run — Grafana (richer) vs metrics-server (convenient)

> The full Grafana-vs-metrics-server live-view choice (both deployable by the agent via an
> approval-gated `run_shell`; the only user-owned piece is the `GRAFANA_DASHBOARD_URL` env var) +
> embedding a user's own Grafana in the live panel → read_knowledge('observability_grafana').
> Short version: offer BOTH as a pair before a run — Grafana is the richer view (GPU/latency/
> throughput/history) and metrics-server (`install_metrics_server.sh`, §2 above) is the zero-setup
> CPU/mem fallback. Never refuse to stand up Grafana.

## 3. Benchmark monitoring — activating `results.observability` (DEFAULT ON)

> Full detail moved → read_knowledge('observability_monitoring'). Short version: the Benchmark
> Report's `results.observability` block (KV-cache/GPU/queue) is populated ONLY when the run was
> driven with monitoring on, so **default monitoring ON** — set `flags.monitoring: true` on
> `execute_llmdbenchmark` to emit `--monitoring`. The ONE precondition is the Prometheus-operator
> CRDs; probe `probe_environment(checks=["prometheus_crds"])` before standup and opt out
> (`monitoring: false` → `--no-monitoring` on standup) ONLY on a cluster that has no CRDs and won't
> install them. The `cicd/kind` quickstart installs the CRDs itself, so keep monitoring ON there
> even when the probe shows them absent. That guide also covers reading the activated metrics
> (KV-cache hit rate, schedule delay, GPU util) + the upstream saturation thresholds.

## 4. Distributed tracing — the `tracing:` config block (CONFIG-ONLY)

> Full detail moved → read_knowledge('observability_tracing'). Short version: the benchmark
> **CONFIGURES** the `tracing:` block so pods *emit* OTLP spans, but it CANNOT collect or show
> traces — the user views them in their own Jaeger/Tempo. Author the dotted `tracing.*` overrides
> via `write_and_validate_config`, then GATE with `plan`/`--dry-run` before standup.

## 5. Real-time metric streaming / custom PromQL — UPSTREAM-UNIMPLEMENTED

> Full detail moved → read_knowledge('observability_streaming'). Short version: the benchmark
> itself CANNOT stream live metrics nor run user-defined Prometheus queries (both upstream
> "Not Yet Implemented"). Be honest about the gap, then offer the real substitutes: live
> CPU/mem via `observe_run_metrics` (§2) + live pod-log streaming via
> `orchestrate_benchmark_run`; arbitrary PromQL runs in the USER'S own Prometheus/Grafana (§3),
> not the benchmark CLI. Grafana embedding → read_knowledge('observability_grafana').
