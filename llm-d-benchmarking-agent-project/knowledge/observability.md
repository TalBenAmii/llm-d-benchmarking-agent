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

The Benchmark Report v0.2 carries a `results.observability` block (KV-cache hit rate, schedule
delay / queue depth, GPU utilization, replica/startup/EPP-log snapshots). The analyzer already
parses + surfaces these (see `knowledge/results_interpretation.md` and
`knowledge/standard_metrics.yaml`). But that block is **only populated when the metrics PRODUCER
ran** — i.e. when the benchmark was driven with monitoring enabled. Without it, the report ships
with an EMPTY observability block and every standard metric reads as `None`. So: **default to
turning monitoring ON.** This is your judgment, expressed as a flag the tools merely emit.

### The mechanism (what the flag does)

Set `flags.monitoring` on the `execute_llmdbenchmark` tool:

- `monitoring: true` → the tool emits **`--monitoring`** on the subcommand. On **standup** this
  renders + applies the **PodMonitor / ServiceMonitor** resources and bumps **EPP (inference
  scheduler) verbosity to level 4** (richer scheduling logs for post-run analysis). On **run** /
  **experiment** it enables **vLLM `/metrics` scraping + pod-log capture** during the run, which
  is what fills `results.observability`. `plan` accepts `--monitoring` too (preview the
  monitoring-enabled rendering without deploying).
- `monitoring: false` → the tool emits **`--no-monitoring`** **on `standup` only** (upstream,
  only standup's argument is a `BooleanOptionalAction`; run/experiment/plan are `store_true`).
  `--no-monitoring` disables PodMonitor **and** the GAIE ServiceMonitor creation — the clean
  escape for clusters lacking the Prometheus-operator CRDs. On run/experiment a `false`/opt-out
  simply omits the flag (no scraping); there is no `--no-monitoring` there.
- omit `monitoring` → upstream scenario defaults apply (`monitoring.podmonitor.enabled: true`,
  `metricsScrapeEnabled: false`).

Under the hood `--monitoring` sets `monitoring.podmonitor.enabled: true` +
`monitoring.metricsScrapeEnabled: true`; `--no-monitoring` sets `podmonitor.enabled: false` and
disables the GAIE Prometheus ServiceMonitor. The same effect can be reached scenario-side via
`monitoring.podmonitor.enabled` / `monitoring.installPrometheusCrds`.

The vLLM Prometheus `/metrics` endpoint is on port **8200** (modelservice) / **8000**
(standalone); the **EPP (inference-scheduler)** endpoint is a SEPARATE scrape on port **9090**
with **bearer-token auth** (Source: `llm-d-benchmark/docs/metrics_collection.md` → "EPP Prometheus
Metrics"). So two distinct PodMonitors/scrapes feed Prometheus — the vLLM serving metrics and the
EPP scheduling metrics — not one.

### The decision (default ON, with a knowledge-driven opt-out)

**Default: enable monitoring** so the report carries real KV-cache/GPU/queue metrics — these are
the most useful signals for interpreting an llm-d run and are the whole point of
`results.observability`. The ONE precondition: the cluster has the Prometheus-operator CRDs
(`podmonitors.monitoring.coreos.com`, `servicemonitors.monitoring.coreos.com`). When monitoring
is on and those CRDs are absent, standup tries to apply PodMonitors and that step is non-fatal
but noisy (logs a "PodMonitor CRD not found -- skipping" warning), and on a strict cluster the
pre-flight CRD validation can fail the standup.

So BEFORE a standup, **probe** `probe_environment(checks=["prometheus_crds"])`:

- `present: true` → keep monitoring ON (the default). Emit `--monitoring`.
- `present: false` → the cluster lacks the CRDs. Two correct paths, in order of preference:
  1. **If the scenario installs them** (the bundled `cicd/kind` scenario already sets
     `monitoring.installPrometheusCrds: true`, so standup installs the CRDs itself): keep
     monitoring **ON** — the CRDs will exist by the time PodMonitors apply. Absence at probe time
     is expected on a fresh Kind cluster and is **not** a reason to opt out on the quickstart path.
  2. **If the cluster won't install them** (a bare/vanilla cluster, no operator, scenario doesn't
     set `installPrometheusCrds`): **opt out** — set `monitoring: false` so standup emits
     `--no-monitoring`. The run still produces a valid report; it just won't carry the scrape-only
     observability metrics. Tell the user plainly why those metrics are absent (no
     Prometheus-operator CRDs / monitoring disabled), and offer to enable
     `monitoring.installPrometheusCrds` in the scenario as the alternative.

Never bake this on/off logic into Python — the tools only emit the boolean you set here. The CRD
probe reports facts; this file is where "default ON, opt out on a truly CRD-less cluster" lives.

### Reading the activated metrics

Once monitoring ran, the parsed metrics surface through `analyze_results` (per-run
`standard_metrics`) and `locate_and_parse_report` (`summary.standard_metrics`): **KV-cache hit
rate** (higher = more prefix reuse → lower TTFT), **schedule delay** (a labelled queue-depth
proxy; lower = less admission queueing), and **GPU utilization**. Interpret them per
`knowledge/results_interpretation.md`. If they are `None` after a run, the usual cause is that
monitoring was off / the CRDs were missing — say so; never fabricate a value the report lacks.

Upstream-grounded saturation thresholds to flag (Source:
`llm-d/docs/operations/observability/metrics.md`; keep the field names consistent with
`knowledge/standard_metrics.yaml`):

- **KV-cache utilization > 0.9 (~90%)** (`vllm:kv_cache_usage_perc`, 0.0–1.0) = near-full — GPU
  memory is nearly full and requests may be **preempted or rejected**. (This is cache
  *occupancy*, not the *hit rate* above.)
- **Non-zero waiting/queued requests** (`vllm:num_requests_waiting`) = pods are **saturated** —
  the primary autoscaling signal; a rising value means admission queueing.
- *(optional)* **error rate > 5%** (`llm_d_epp_request_error_total`, per flow-id/priority) =
  backend failures worth alerting on.

This monitoring is about **metrics** (Prometheus time-series). It is *separate* from
**distributed tracing** (per-request OpenTelemetry spans), which the next section covers — and
which the benchmark can only **configure**, never collect.

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
