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

When the user wants to *watch a run live*, there are two complementary surfaces. Offer BOTH as a
pair before the run (alongside the metrics-server offer above) and clarify what each is for — they
are not the same thing, and one is not a drop-in for the other:

- **Grafana — the richer, recommended view.** The llm-d observability dashboard (the upstream
  `--monitoring` Grafana, backed by Prometheus) shows the *full* picture: GPU utilization, vLLM/EPP
  latency (TTFT/ITL), throughput, queue depth, KV-cache, **and history across the whole run**. You
  CAN stand this stack up for the user — do NOT treat it as "their problem to solve" or claim you can
  "only advise". The upstream recipe is one approval-gated `run_shell` away, exactly like the
  metrics-server install and the kind-cluster create: read the exact commands from the upstream
  `install-prometheus-grafana.sh` / observability `setup.md` (`knowledge/useful_repo_docs.md` →
  `read_repo_doc`), then run them with `run_shell` (mutating → it raises the Approve card). Offer it
  the same way you offer the metrics-server install. The ONE piece that is genuinely the user's, not
  yours, is the backend env var `GRAFANA_DASHBOARD_URL` — you have no env/secret-write tool, so THEY
  set it (pointing at their Grafana). Once set, an **Open Grafana** button appears above the live
  metrics in the run panel and opens the dashboard in a modal. `probe_environment` reports
  `grafana_dashboard.configured` (true once the env var is set) — use it to tailor the message:
  configured → "it'll show up in the run panel"; not configured → "I can deploy the stack with you
  (one Approve); then set `GRAFANA_DASHBOARD_URL` and I'll embed it beside the run".
- **metrics-server — the convenient alternative.** Live **CPU/memory only** (no GPU, no latency, no
  history), but it is the zero-setup option **you can install for them** in one approval-gated step
  (`install_metrics_server.sh`, per §2.1) and it lights up the in-panel sparklines immediately. It is
  the right answer when the user just wants a quick "is anything melting?" view and has no Grafana
  stack.

Position Grafana as the fuller picture and metrics-server as the quick fallback. Be honest about the
real split: you can DEPLOY both for them (each an approval-gated `run_shell`) — the only thing you
can't do for Grafana is write their `GRAFANA_DASHBOARD_URL` backend env var (no env/secret tool), and
that controls only the in-panel embed, not whether the stack exists. So never refuse to stand up
Grafana; at most note the env var is the user's last step. The two live-views are independent — the
Grafana embed works even when metrics-server is absent, and vice-versa (see §5.2 for the embed).

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

## 4. Distributed tracing — configuring the `tracing:` block (CONFIG-ONLY)

This is an **advanced**, opt-in feature for users who already run an OpenTelemetry backend. Read
the limitation first, because it shapes everything you tell the user:

> The benchmark **CONFIGURES** OpenTelemetry tracing on the deployed modelservice pods — it
> renders a `tracing:` block (endpoint, sampling rate, service names) into the modelservice
> values so the vLLM decode/prefill pods and the routing proxy **export** OTLP spans. It does
> **NOT** deploy an OTel Collector or Jaeger/Tempo, and it does **NOT** collect, store, or
> analyze traces — there is no tracing data in the **Benchmark Report**. *Collection is the
> user's own external OTel backend.*
> (Source: `llm-d-benchmark/docs/observability.md` → "Distributed Tracing"; backend setup is
> `llm-d/docs/operations/observability/tracing.md` — OTel Collector + Jaeger.)

So be explicit with the user: **the agent cannot SHOW traces.** Authoring this block makes the
pods *emit* spans; the user views them in their **own** Jaeger/Tempo UI. Without a reachable
collector, enabling tracing is a **no-op** (the pods try to export to a dead endpoint — no error,
no spans collected). That reachable-collector precondition is on the **user**, not the agent.

### How to author it (the mechanism)

Use `write_and_validate_config(artifact_type='scenario', …)` exactly as for any other scenario
knob — the `tracing.*` family is a set of DOTTED overrides deep-merged onto the scenario item.
The keys (rendered by `config/templates/jinja/13_ms-values.yaml.j2`; nothing renders unless
`tracing.enabled` is set):

- `tracing.enabled` — **`true`** to turn tracing on (the gate for the whole block).
- `tracing.otlpEndpoint` — the **OTLP gRPC** endpoint of the user's collector, e.g.
  `http://otel-collector:4317`. Ask the user for this; it is *their* infrastructure.
- `tracing.sampling.sampler` — the sampler, e.g. `parentbased_traceidratio`.
- `tracing.sampling.samplerArg` — the sampling ratio as a **string**: `'1.0'` = 100% (trace
  every request), `'0.1'` = 10%. **Lower it to cut tracing overhead** on a load run — full
  sampling under high concurrency adds export cost; start at `'0.1'` or below for a real
  benchmark, `'1.0'` only when debugging a handful of requests.
- `tracing.serviceNames.vllmDecode` / `.vllmPrefill` / `.routingProxy` — the service-name labels
  the spans carry (so the user can tell decode/prefill/proxy apart in their backend).
- `tracing.vllm.collectDetailedTraces` — opt into vLLM's finer-grained internal spans.

Example overrides for one scenario item:

```
write_and_validate_config(artifact_type='scenario', target_filename='traced.yaml', content={
  "name": "traced-baseline",
  "tracing.enabled": True,
  "tracing.otlpEndpoint": "http://otel-collector:4317",
  "tracing.sampling.sampler": "parentbased_traceidratio",
  "tracing.sampling.samplerArg": "0.1",
  "tracing.serviceNames.vllmDecode": "vllm-decode",
  "tracing.serviceNames.vllmPrefill": "vllm-prefill",
  "tracing.serviceNames.routingProxy": "routing-proxy",
})
```

`tracing` is a **soft-optional** knob: no upstream scenario *example* sets it, so the shape
validator special-cases it (`_SOFT_OPTIONAL_KNOBS`) to accept the block — but it IS a real,
deep-merged scenario key the modelservice jinja renders.

### GATE it, then hand off

Always run the authored scenario through the **determinism gate** before any standup: pass the
returned `spec_path` to `execute_llmdbenchmark(subcommand='plan', spec=<spec_path>,
flags={'dry_run': True})`. A clean plan/--dry-run is the acceptance check that the `tracing:`
block renders. Then make the boundary clear to the user: the deployed pods will export OTLP spans
to the endpoint you configured; **they** open their Jaeger/Tempo to view them — the agent has no
trace data to show and none appears in the Benchmark Report.

## 5. Real-time metric streaming / custom Prometheus queries — UPSTREAM-UNIMPLEMENTED

Two things users commonly ask for around metrics are **NOT implemented by the benchmark
itself**. Be honest about the gap, then offer the agent's best-available substitute — never
imply the benchmark can do something it cannot.

> The upstream metrics-collection feature explicitly lists both of these under **"Not Yet
> Implemented"**:
> 1. **Real-time Visualization** — *"Live metric streaming during benchmark execution."*
>    The benchmark currently generates **static PNG graphs only AFTER the run completes** (it
>    scrapes vLLM/EPP Prometheus endpoints every 15s into raw logs, then `visualize_metrics.py`
>    renders time-series PNGs once the run is done). There is **no live/streaming metric feed**
>    during a run.
> 2. **Custom Metric Queries** — *"User-defined Prometheus queries."* The benchmark collects a
>    **fixed, predefined metric set** (the vLLM/EPP/DCGM/cAdvisor list in §1 / the report's
>    `results.observability`); you **cannot hand it an arbitrary PromQL query** to collect.
> (Source: `llm-d-benchmark/docs/metrics_collection.md` → "Not Yet Implemented"; mirrored in
> `llm-d-benchmark/docs/observability.md`.)

So: the benchmark **cannot** stream live benchmark metrics in real time, and it **cannot** run
user-defined/custom Prometheus queries. Those are upstream gaps, not agent limitations to
apologize for — state them plainly and pivot to what IS available.

### The substitute for "live metrics during a run"

Even though the benchmark won't stream its own metrics, the agent CAN give the user a live view
of the run, two ways:

- **`observe_run_metrics` (kubectl top)** — live **CPU/memory** of the model-server / harness
  pods **WHILE the run is in flight** (read-only, auto-runs; see §2 for how to read the numbers
  and pre-empt an OOM/throttle). This is the closest thing to a live metric feed: poll it during
  the run to watch resource pressure in real time. Needs the in-cluster metrics-server (§2 covers
  the per-cluster install).
- **Phase-21 real-time pod-log streaming** — when you drive the run via
  `orchestrate_benchmark_run`, the benchmark pod's **log lines are forwarded live to the user as
  `output` events** (the same transport the runner uses for streamed command output). That gives
  a real-time, in-flight view of the harness's own progress/throughput log as it runs — without
  waiting for the post-run report.

### The answer the agent must give

When the user asks **"can you stream live benchmark metrics?"** (or "show me metrics in real
time as it runs"):

> Be HONEST first: *no — the benchmark itself does not stream live metrics. Upstream metric
> streaming / real-time visualization is "Not Yet Implemented"; the benchmark only produces
> static PNG graphs and the `results.observability` block AFTER the run finishes.* Then OFFER the
> substitute: *but I can watch the run live for you two ways — `observe_run_metrics` (kubectl top)
> to track CPU/memory on the model-server and harness pods in real time, and live pod-log
> streaming (via `orchestrate_benchmark_run`) so you see the harness's progress as it runs.*

Do not promise a streaming metric chart the benchmark can't produce; offer kubectl top + log
streaming as the real, available equivalent.

### Embedding the user's own Grafana in the live panel (optional)
If the operator has their own llm-d observability stack (the upstream `--monitoring` Grafana),
they can set the backend env var **`GRAFANA_DASHBOARD_URL`** to that dashboard's URL. When set, an
**Open Grafana** button appears above the live metrics in the run panel; clicking it opens the
dashboard in a large modal overlay (with an **open-in-new-tab** fallback for Grafana instances that
refuse iframe embedding via `X-Frame-Options` / `frame-ancestors`). The button shows even when no
metrics-server is present, since the external Grafana is independent of it. Unset (the default) → no
button, and the panel shows only the agent's own kubectl-top view. This is mechanism only — it
surfaces the operator's dashboard; it does not make the benchmark itself stream metrics. So when a
user asks for "live Grafana during the run," the honest answer is: point me at your Grafana via
`GRAFANA_DASHBOARD_URL` and I'll show it in the run panel (see the paired offer in §2).

### The substitute for "custom Prometheus queries"

The benchmark collects only its fixed metric set, so arbitrary PromQL is **the user's own
Prometheus/Grafana**, not the benchmark. If the user has the upstream monitoring stack running
(see §3 — `--monitoring` creates the PodMonitors that feed their Prometheus), point them at their
own Prometheus/Grafana to run **any PromQL they like** against the scraped vLLM/EPP series. The
ready-to-use query catalog and dashboards are already linked from §3 and from
`knowledge/useful_repo_docs.md` (the upstream **Example PromQL Queries** and **Grafana
Dashboards**). So: custom queries are possible — just in the user's Prometheus, not via the
benchmark CLI.
