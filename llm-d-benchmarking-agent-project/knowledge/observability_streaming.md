# Real-time metric streaming / custom Prometheus queries — UPSTREAM-UNIMPLEMENTED

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
>    **fixed, predefined metric set** (the vLLM/EPP/DCGM/cAdvisor list in observability_monitoring.md /
>    the report's `results.observability`); you **cannot hand it an arbitrary PromQL query** to
>    collect.
> (Source: `llm-d-benchmark/docs/metrics_collection.md` → "Not Yet Implemented"; mirrored in
> `llm-d-benchmark/docs/observability.md`.)

So: the benchmark **cannot** stream live benchmark metrics in real time, and it **cannot** run
user-defined/custom Prometheus queries. Those are upstream gaps, not agent limitations to
apologize for — state them plainly and pivot to what IS available.

### The substitute for "live metrics during a run"

Even though the benchmark won't stream its own metrics, the agent CAN give the user a live view
of the run, two ways:

- **`observe_run_metrics` (kubectl top)** — live **CPU/memory** of the model-server / harness
  pods **WHILE the run is in flight** (read-only, auto-runs; see observability.md §2 for how to
  read the numbers and pre-empt an OOM/throttle). This is the closest thing to a live metric
  feed: poll it during the run to watch resource pressure in real time. Needs the in-cluster
  metrics-server (observability.md §2 covers the per-cluster install).
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
> → read_knowledge('observability_grafana'). Short version: the operator sets the backend env
> var `GRAFANA_DASHBOARD_URL`; when set, an **Open Grafana** button appears above the live metrics
> in the run panel (independent of metrics-server). See the paired offer in observability.md §2.

### The substitute for "custom Prometheus queries"

The benchmark collects only its fixed metric set, so arbitrary PromQL is **the user's own
Prometheus/Grafana**, not the benchmark. If the user has the upstream monitoring stack running
(see observability_monitoring.md, "The mechanism" — `--monitoring` creates the PodMonitors that feed their Prometheus),
point them at their own Prometheus/Grafana to run **any PromQL they like** against the scraped
vLLM/EPP series. The ready-to-use query catalog and dashboards are already linked from
observability_monitoring.md and from `knowledge/useful_repo_docs.md` (the upstream **Example PromQL
Queries** and **Grafana Dashboards**). So: custom queries are possible — just in the user's
Prometheus, not via the benchmark CLI.
