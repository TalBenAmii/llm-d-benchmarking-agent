# Glossary (plain-language)

- **llm-d** — a Kubernetes-native distributed LLM inference platform (prefix-aware routing,
  KV-cache disaggregation, workload-aware scheduling).
- **llm-d-benchmark** — its companion benchmarking toolchain; provides the `llmdbenchmark`
  CLI used here.
- **kind** — "Kubernetes IN Docker"; a local single-machine cluster for development.
- **spec / scenario** — a cluster+model configuration the CLI can stand up (e.g. `cicd/kind`).
- **harness** — the load generator: `inference-perf` (staged K8s load), `guidellm`
  (throughput sweeps), `vllm-benchmark`, `inferencemax`, `nop` (load-time only).
- **workload / profile** — the request pattern: token lengths, rates, prefix sharing
  (e.g. `sanity_random.yaml`, `chatbot_synthetic.yaml`).
- **inference-sim** — a simulated inference engine that mimics token timing without a GPU
  or a real model; used by the kind quickstart.
- **standup / smoketest / run / teardown** — the CLI lifecycle: deploy, verify, benchmark,
  clean up.
- **EPP / router** — the Endpoint Picker that routes requests to model-server pods using
  prefix/load-aware scoring.
- **TTFT** — time to first token. **TPOT/ITL** — per-output-token / inter-token latency.
  **throughput** — tokens or requests per second. **goodput** — requests meeting an SLO.
- **Benchmark Report (v0.2)** — the standardized result format the CLI emits; the agent
  reads metrics from it (never from raw logs).
