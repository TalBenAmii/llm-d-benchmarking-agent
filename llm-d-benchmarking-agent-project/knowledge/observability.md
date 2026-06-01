# Observability (Prometheus metrics + live run metrics)

This agent exposes two complementary observability surfaces. Use them to *explain what is
happening*, not to make decisions silently — surface the signal to the user and reason about
it out loud.

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
- `available: false` means the metrics-server is not installed/ready in the cluster (it is part
  of the `cicd/kind` spec, but a bare cluster may lack it). Relay that plainly — it is not an
  error you caused, and nothing was changed (the probe is read-only). Fall back to
  `probe_environment` / pod status if you need a coarse health signal.

Do not invent or estimate utilization numbers. If `observe_run_metrics` returns no rows, say
so; never fabricate a percentage.
