# SIMULATE mode: read-only commands are REAL; only mutating OUTCOMES are simulated (honesty rule)

> This applies to the **agent's `SIMULATE=1` dry-run**. It is separate from the opt-in
> `llm-d-inference-sim` integration tests below, which stand up a real mock server.

Under `SIMULATE=1` the split is by command **kind**, not all-or-nothing:

- **READ-ONLY commands run FOR REAL.** Environment probes (`docker info`, `kind get clusters`,
  `kubectl get` / `cluster-info`, …) and ad-hoc `grep`/`ls`/`cat` actually execute and return
  genuine output. So you are **not** blind under SIMULATE — gather the context you need and
  **trust** what comes back. (This is the fix for the old behaviour where every command no-opped to
  empty and the agent was flying blind.)
- **MUTATING actions are NOT executed.** Every `standup`/`deploy`/`run`/`teardown`, install script,
  and `kind`/`kubectl`/`helm`/`docker`/`git` write is announced (you see exactly what *would* run)
  and returns synthetic success — nothing is deployed or benchmarked.

Bind these honesty rules:

- **Read probe output honestly — it's real.** "Docker is up, kind is missing" is now a TRUE
  statement to make. Don't slap a "didn't actually run" caveat on a read-only probe. Still: don't
  claim host facts you didn't probe — no zero-tool-call "Cluster reachable ✅".
- **Never present the OUTCOME of a simulated mutation as real.** Anything that would RESULT from
  a no-op `standup`/`run` — a deployed stack, a serving endpoint, running pods, a
  benchmark report or its numbers — is SYNTHETIC. Never say "the stack is deployed", "the endpoint
  is serving", or present simulated results as measured. Attach
  "(simulated — nothing was actually deployed or benchmarked)" wherever such post-deploy state or
  results appear.
- This is the probe analogue of the results honesty floor in
  `knowledge/results_interpretation.md` — a SIMULATED mutation's result is never a green light, even
  though the read-only probe that observed the (empty) cluster afterward is itself real.

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
