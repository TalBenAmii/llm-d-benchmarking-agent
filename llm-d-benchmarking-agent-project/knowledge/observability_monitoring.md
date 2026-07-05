# Benchmark monitoring — activating `results.observability` (DEFAULT ON)

Split out of `observability.md` (that file is the hub; this is the on-demand detail). Load this
when deciding whether to drive a benchmark with monitoring enabled, or when a report's
`results.observability` block came back empty.

The Benchmark Report v0.2 carries a `results.observability` block (KV-cache hit rate, schedule
delay / queue depth, GPU utilization, replica/startup/EPP-log snapshots). But that block is
**only populated when the metrics PRODUCER ran** — i.e. when the benchmark was driven with
monitoring enabled. Without it, the report ships with an EMPTY observability block and every
standard metric reads as `None`. So: **default to turning monitoring ON.**

## The mechanism (what the flag does)

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

## The decision (default ON, with a knowledge-driven opt-out)

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

## Reading the activated metrics

Once monitoring ran, the parsed metrics surface through `analyze_results` (per-run
`standard_metrics`) and `locate_and_parse_report` (`summary.standard_metrics`): **KV-cache hit
rate**, **schedule delay**, and **GPU utilization** — interpretation, the `None` diagnosis
(monitoring off / CRDs missing) and the never-fabricate rule are all in
`knowledge/results_interpretation.md`.

Upstream-grounded saturation thresholds to flag (Source:
`llm-d/docs/operations/observability/metrics.md`):

- **KV-cache utilization > 0.9** (`vllm:kv_cache_usage_perc`) = near-full → preemption/rejection
  risk, and **non-zero `vllm:num_requests_waiting`** = **saturated** pods — the primary
  autoscaling signal. Full wording, field names + the occupancy-vs-hit-rate distinction:
  `knowledge/standard_metrics.yaml` comments.
- *(optional)* **error rate > 5%** (`llm_d_epp_request_error_total`, per flow-id/priority) =
  backend failures worth alerting on.

This monitoring is about **metrics** (Prometheus time-series), *separate* from **distributed
tracing** (per-request OpenTelemetry spans; the benchmark only CONFIGURES it, never collects):
read_knowledge('observability_tracing').
