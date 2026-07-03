# Distributed tracing — the `tracing:` config block (CONFIG-ONLY)

> Extracted from `knowledge/observability.md` §4 — read this when the user asks about OpenTelemetry
> tracing. The entry file keeps a pointer (`read_knowledge('observability_tracing')`).

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
