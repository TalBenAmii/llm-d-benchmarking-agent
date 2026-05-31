# Flow validation — "does the agent run the *right commands*?"

This project ships a **flow-validation harness** that proves the agent drives the correct
command sequence for each end-to-end task a user might ask for — e.g. the kind quickstart
(benchmark repo) or the optimized-baseline guide (llm-d repo) — and that it gates and
refuses commands exactly as the security policy requires.

There are two layers, both built on the *same* flow fixtures:

| Layer | What it proves | Deterministic? | Needs | Gates CI? |
|-------|----------------|----------------|-------|-----------|
| **Golden transcript** (`tests/flows/test_flows.py`) | The *mechanism*: the allowlist accepts the flow, argv is built correctly, read-only vs mutating is classified right, and every mutation is approval-gated. | ✅ yes | nothing (no key/Docker/kind/repos) | ✅ **yes** |
| **Live eval** (`tests/flows/test_flows_live.py`) | The *judgment*: a real LLM, given natural-language input, actually *chooses* the right commands. | ❌ no | an API key | ❌ no (opt-in) |

## Quick start

```bash
make validate          # deterministic, hermetic — the headline check
make flows             # list known flows
make validate-live     # the real LLM drives each flow from mock input (needs a key in .env)
make validate-pytest   # the gating checks, as pytest
make test              # the whole suite
```

`make validate` prints, per flow, the exact commands the agent runs:

```
[ PASS ] kind-quickstart — kind quickstart (cicd/kind, simulated CPU engine)
        $ git clone https://github.com/llm-d/llm-d-benchmark  [mutating]
        $ install.sh --uv  [mutating]
        $ llmdbenchmark --spec cicd/kind standup -p llmd-quickstart --skip-smoketest  [mutating]
        $ llmdbenchmark --spec cicd/kind smoketest -p llmd-quickstart  [mutating]
        $ llmdbenchmark --spec cicd/kind run -p llmd-quickstart -l inference-perf -w sanity_random.yaml -r …/results  [mutating]
```

## How it works (and why it's hermetic)

Every command the agent runs funnels through one seam:
`ctx.run_command → allowlist.validate(...) → runner.execute(...)`. The harness
(`tests/flows/harness.py`) keeps the real allowlist and the real approval gating, and
swaps only two things:

1. **`CaptureRunner`** — a `CommandRunner` that *records* the logical argv instead of
   spawning a subprocess (and simulates a `git clone`'s side effect so downstream tools
   behave). Nothing touches your machine.
2. **A frozen catalog** (`tests/flows/catalog_snapshot.py`) — the allowlist's
   `ref_catalog` checks and the `SessionPlan` validator consult the live on-disk catalog;
   in CI the repos are empty gitlinks, so we seed a snapshot of the real
   `specs`/`harnesses`/`workloads`. `test_snapshot_matches_live` re-checks the snapshot
   against the real repo whenever it's present, so drift is caught.

For each flow the harness runs the **real agent loop** and asserts:

- the **significant commands** (`llmdbenchmark` / `install.sh` / `git` / `helm`) match the
  flow's expected ordered list (a `*` token matches the dynamic results-dir path);
- the **gating invariant**: every `mutating` command was approval-gated; every `read_only`
  command auto-ran; nothing denied reached the runner;
- per-flow extras: forbidden subcommands absent, read-only-only previews, refusals, the
  probe actually detecting a running stack, expected guidance in the agent's replies, etc.

## The flows today

| Flow | What it validates |
|------|-------------------|
| `kind-quickstart` | Fresh machine → clone → `install.sh --uv` → `standup`/`smoketest`/`run` on `cicd/kind`, then parse the report. |
| `optimized-baseline` | The llm-d optimized-baseline guide via `--spec guides/optimized-baseline` (same CLI, different spec). |
| `pd-disaggregation` | The prefill/decode disaggregation guide (`guides/pd-disaggregation`). |
| `precise-prefix-cache-routing` | The precise prefix-cache routing guide (`guides/precise-prefix-cache-routing`). |
| `tiered-prefix-cache` | The tiered prefix cache guide (`guides/tiered-prefix-cache`, shared-prefix workload). |
| `wide-ep-lws` | The wide expert-parallelism + LeaderWorkerSet guide (`guides/wide-ep-lws`). |
| `workload-autoscaling` | The workload autoscaling guide (`guides/workload-autoscaling`, guidellm harness). |
| `predicted-latency-routing` | The predicted-latency routing guide (`guides/predicted-latency-routing`, concurrent load). |
| `teardown` | `teardown` runs; deeper `kind delete cluster` is **offered**, never run silently. |
| `existing-stack-benchmark-only` | Probe detects a running stack → benchmark it directly, **no** `standup`/`smoketest`. |
| `dry-run-preview` | `plan` + `standup --dry-run` only — read-only, no approval prompt, nothing changed. |
| `safety-refusal` | Unknown spec / injected namespace / disallowed flag are **refused**; direct allowlist assertions that dangerous commands are denied and the legit ones are still allowed. |

The seven guide deploys share one factory (`_guide_deploy_flow`) — they're the same
command shape, differing only by `--spec` / harness / workload / namespace. The
GPU-requiring guides are `live_eval=False` (a careful agent would refuse to deploy them on
a GPU-less env, which would make a live score misleading); their command shape is still
validated deterministically.

## Adding a flow

Append one `Flow(...)` to `tests/flows/flows.py` — it's pure data. Give it:

- `mock_user_input` (what a person types),
- `turns` (the golden transcript: the ideal tool-call sequence),
- `expected` (the ordered significant commands), and
- optional invariants (`forbidden_subcommands`, `expect_all_readonly`, `assistant_text_contains`, …)
  and live-eval hints (`required_subcommands`, `required_spec`).

No harness or CI changes are needed — the tests and the CLI pick it up automatically.

> **More flows are cheap.** For another guide deploy, add one `_guide_deploy_flow(...)`
> line. Still unmodeled and available in the repos: `guides/agentic-tests`, the
> `examples/gpu` / `examples/cpu` / `examples/sim` specs, and the other CI clusters
> `cicd/ocp` / `cicd/gke` / `cicd/cks`. A sweeps/experiment (DoE) + A/B-compare flow is
> worth adding once the `feature/sweeps-ab-compare` capability lands on main.

## CI

`.github/workflows/agent-flow-validation.yml` (at the repo root, since GitHub Actions reads
workflows there) runs the hermetic gating job on every push/PR that touches the project.
A separate **opt-in** `live-eval` job runs the real-LLM eval only on manual dispatch with an
API-key secret, and is `continue-on-error` so it never blocks the build.
