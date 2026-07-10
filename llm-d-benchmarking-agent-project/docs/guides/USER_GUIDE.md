# User Guide

This guide is for the **person using the agent** — you don't need to know the
`llm-d-benchmark` CLI, the `<spec, harness, workload>` model, or Kubernetes. You describe
what you want in plain language and the agent does the rest, asking for your approval before
anything changes your system.

For installing/running the agent, see [`DEPLOYMENT.md`](DEPLOYMENT.md). For what's under the
hood, see [`ARCHITECTURE.md`](../reference/ARCHITECTURE.md).

## What it does

You say something like *"benchmark a chat app with about 500 concurrent users"*. The agent:

1. **Interviews you** briefly to pin down the use case and any quality-of-service targets.
2. **Checks your environment** — is Docker up? Is there a cluster? Is a stack already
   running?
3. **Proposes a plan** (a *SessionPlan*) you approve before anything is deployed.
4. **Prepares** — installs missing prerequisites, clones the benchmark repo, builds its
   tooling, creates a local cluster — each step shown to you and approved.
5. **Deploys, validates, and runs** the benchmark.
6. **Explains the results** in plain language, tied back to what you asked for.

## The first run: the quickstart

The supported out-of-the-box path is the **quickstart** — a tiny llm-d stack on a local
[kind](https://kind.sigs.k8s.io/) cluster using a *simulated* inference engine (no GPU, no
model download). Open the UI (`http://127.0.0.1:8000`) and type something like:

> Benchmark a small chat model on my laptop.

The agent will walk through, roughly:

1. **Probe** your environment (read-only, automatic — no prompt).
2. **Read the real procedure** from the benchmark repo's docs (so it doesn't rely on
   memory).
3. **Propose a SessionPlan** — e.g. `spec=cicd/kind`, `harness=inference-perf`,
   `workload=sanity_random.yaml`, namespace `llmd-quickstart`, with the steps it intends to
   run. **You approve it** before anything happens.
4. **Install prerequisites** if needed (Docker + the kind binary), **clone** the repo,
   **build** its tooling, **create** the kind cluster — each one prompts you to Approve.
5. **Stand up** the stack, **smoketest** it, then **run** the benchmark (output streams
   live).
6. **Summarize** the Benchmark Report: TTFT, inter-token latency, throughput, percentiles —
   in plain words.
7. **Offer teardown** when you're done.

## Approvals: what runs automatically vs what asks first

- **Read-only checks run automatically** (probing the environment, listing the catalog,
  reading a doc, parsing a report, previews via `--dry-run`/`plan`). You'll still *see* them
  in the command trail.
- **Anything that changes your system asks first.** Installing Docker/kind, cloning,
  building tooling, creating/deleting the cluster, standup, run, teardown — each shows you
  the **exact command** and waits for you to click **Approve** (or Reject). If you Reject,
  the agent acknowledges and replans.

Nothing runs off-screen: the UI shows every command, and the one-click **Debug view** (`>_`)
reveals the executed-command trail *inline in the chat* — each command appears in place,
between the messages, in the order it ran, with read-only/mutating badges. Toggle it off to
hide the commands again.

## Reading the results

The agent only ever reports numbers from a **validated Benchmark Report** — it never invents
or estimates metrics. If a report is missing or invalid, it says so plainly. Common metrics:

- **TTFT** (time to first token) — responsiveness; how long until the model starts replying.
- **TPOT / ITL** (time per output token / inter-token latency) — how fast text streams once
  it starts.
- **Throughput** — output tokens per second; how much work the stack sustains.
- **Percentiles (p50/p90/p95/p99)** — tail behavior; p99 latency is what your slowest users
  feel.

If you gave the agent **SLO targets** (e.g. "p99 TTFT under 500 ms, at least 1000 tok/s"), it
can tell you whether each run **meets the SLOs** and give an honest **goodput estimate** (the
fraction of requests that would meet your targets).

## Things you can ask for

| You want… | What to say | What the agent uses |
|---|---|---|
| A basic benchmark | "benchmark a small chat model" | the quickstart flow |
| To benchmark a stack that's already up | "I already have a stack running, just benchmark it" | it detects the running stack and skips redeploy |
| Just a preview, no changes | "show me what you'd run, but don't deploy" | read-only `plan` / `--dry-run` (no approval prompt) |
| To compare two configurations | "compare these two runs" / "which config won?" | `compare_reports` (per-metric deltas + winner) |
| The best config from a sweep | "which configuration is best for my SLOs?" | `analyze_results` (Pareto frontier + goodput) |
| Two harnesses contrasted | "run inference-perf and guidellm and compare" | `compare_harness_runs` (cross-validated, no false winner) |
| "Has performance regressed?" | "how has TTFT trended over time?" | `result_history` trends |
| "Will this even fit?" | "can I run an 8B model on one GPU?" | `check_capacity` pre-flight |
| Live resource usage during a run | "is the server near its memory limit?" | `observe_run_metrics` (`kubectl top`) |

## Picking up where you left off

Chats are saved. The recent-chats sidebar lets you reopen a conversation; the transcript and
the full command trail are replayed. If you navigate away **mid-run**, an already-approved
benchmark keeps running in the background and its result shows up when you return.

## Good practices

- **Let it probe first.** The agent always senses the environment before proposing
  anything — don't skip the plan.
- **Tell it your SLOs up front.** "p99 TTFT under 500 ms" turns vague results into a
  pass/fail verdict and a goodput number.
- **Approve deliberately.** The command on each approval card is exactly what will run. If a
  command surprises you, Reject and ask why.
- **Don't expect it to invent numbers.** If it can't find a valid report, it will say so
  rather than guess.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "LLM provider not configured" | No API key in `.env`. Add `ANTHROPIC_API_KEY` (or OpenAI-compatible creds) and restart. |
| `install_prereqs.sh` can't install Docker | It needs root or passwordless sudo; on WSL the Docker daemon may not auto-start. The agent relays the exact message — start Docker yourself and re-probe. |
| Standup fails with OOM / won't load | Ask for a capacity pre-flight first ("will this fit?"); use a lighter model/spec. |
| "command denied by allowlist" | The agent tried something outside the deny-by-default policy. That's the safety net working; widening it is a reviewed config change (see [`API.md`](../reference/API.md)). |
| A run seems stuck | Use `observe_run_metrics` to see live CPU/memory; orchestrated runs are wall-clock bounded and classify timeouts/OOM/eviction automatically. |

## Want to change *how* the agent reasons?

The agent's judgment lives in editable Markdown/YAML under [`knowledge/`](../../knowledge/) —
playbooks, heuristics, and interpretation guides. Editing those changes the agent's behavior
**without touching code**. The mechanism (security, validation, the tools) is fixed; the
brain is yours to tune.
