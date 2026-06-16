# SIMULATE mode: probe/environment outcomes are simulated too (honesty rule)

> This applies to the **agent's `SIMULATE=1` dry-run** (every shell command no-ops and returns
> empty/synthetic success). It is separate from the opt-in `llm-d-inference-sim` integration
> tests below, which stand up a real mock server.

Under `SIMULATE=1`, **environment/precondition probes carry exactly the same "(simulated — no
command actually ran)" framing as simulated benchmark results.** The agent is usually careful to
disclaim simulated *benchmark numbers*, but has repeatedly narrated simulated *probe* output as
confirmed REAL host state — the same fabrication, different surface. Bind these rules:

- **No-op probe output is "unknown / not checked", NOT "ready".** When `docker info`,
  `kind get clusters`, `kubectl cluster-info`, etc. return empty under SIMULATE, that is the
  simulator no-opping the command — it is **not** evidence that Docker is up, kind is installed,
  the repos are cloned, or the cluster is reachable. Never convert empty/synthetic probe output
  into ✅ readiness ticks.
- **Never assert real host facts from a no-op probe.** Do not say "Docker is up, only kind is
  missing", "Your environment is ready: Docker ✅ kind ✅ venv ✅", or "Cluster reachable". If you
  describe environment state at all under SIMULATE, attach the same caveat results get —
  e.g. "(simulated — these probes didn't actually run; I can't confirm your real host state)".
- **Never volunteer unsolicited host-readiness claims** — especially not as a closer to soften a
  refusal, and **never with zero tool calls** this turn (a "Cluster reachable ✅" with no probe
  behind it is pure fabrication). If the user didn't ask about environment status, don't assert it.
- This is the probe analogue of the results honesty floor in
  `knowledge/results_interpretation.md` — empty/synthetic output is never a green light.

# llm-d-inference-sim integration tests (opt-in)

The proposal (§5.3 / §7) calls for **integration tests with `llm-d-inference-sim`** — the
CPU-only mock inference server (`ghcr.io/llm-d/llm-d-inference-sim`) that serves a tiny model
(`facebook/opt-125m`) over an OpenAI-compatible endpoint, so the whole stack can be exercised
without a GPU. Phase 26 adds an **opt-in** integration layer that exercises the
analyze/compare path against a report produced from a *real* mock run, while keeping the
default test suite fully hermetic.

## What it does

`tests/integration/` holds the layer:

- **Hermetic wiring coverage (always runs).** A sim-SHAPED Benchmark Report v0.2 fixture is
  built from the repo's own BR v0.2 example (read live from `llm-d-benchmark`, never vendored)
  and driven through the real `analyze_results` and `compare_reports` tools — SLO verdict,
  goodput estimate, §3.4 standard metrics (KV-cache hit rate), an A/B delta, and a Pareto
  sweep. This proves the analyze/compare logic genuinely parses a sim-shaped report **even
  when the sim binary is absent**.
- **The live integration test (opt-in, skipped by default).** When enabled, it stands up the
  real `llm-d-inference-sim`, issues real inference requests against it, builds a BR v0.2
  report from the measured latencies, and runs analyze/compare against THAT.

## How to run the live integration test

It runs only when **both** are true (otherwise it SKIPS cleanly — it never hangs reaching a
server that isn't there):

1. `LLMD_SIM_INTEGRATION=1` (explicit opt-in), **and**
2. `llm-d-inference-sim` is locatable, via either:
   - `LLMD_SIM_BINARY=<path-to-or-name-of>` an executable (the standalone build, or the
     image's `/app/llm-d-inference-sim`), or it being on `PATH` as `llm-d-inference-sim`; or
   - a pulled container image — `LLMD_SIM_IMAGE` (default `ghcr.io/llm-d/llm-d-inference-sim`)
     runnable via `docker`/`podman` (image-present check only; the tests never pull).

```bash
# Against a pulled image (docker/podman):
docker pull ghcr.io/llm-d/llm-d-inference-sim:latest
LLMD_SIM_INTEGRATION=1 REPOS_DIR=<dir-holding-llm-d-benchmark> \
  pytest tests/integration/ -v

# Against a local binary:
LLMD_SIM_INTEGRATION=1 LLMD_SIM_BINARY=/path/to/llm-d-inference-sim \
  REPOS_DIR=<dir-holding-llm-d-benchmark> pytest tests/integration/ -v
```

## CI

The non-gating CI job `sim-integration` in
`.github/workflows/agent-flow-validation.yml` mirrors the opt-in `live-eval` job: it runs
only on a **manual dispatch** with `run_sim_integration: true`, is `continue-on-error` (so it
can never block the build), pulls the sim image, and runs `tests/integration/` with the flag
set. The default gating suite is unaffected — those same tests skip there.
