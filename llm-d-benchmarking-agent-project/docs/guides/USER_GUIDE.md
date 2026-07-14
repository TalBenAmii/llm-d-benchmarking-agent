# User Guide

This guide is for the person using the agent. You don't need to know the
`llm-d-benchmark` CLI, the `<spec, harness, workload>` model, or Kubernetes. You describe
what you want in plain language and the agent does the rest, asking for your approval before
anything changes your system.

For installing/running the agent, see [`DEPLOYMENT.md`](DEPLOYMENT.md). For what's under the
hood, see [`ARCHITECTURE.md`](../reference/ARCHITECTURE.md).

## The first run: the quickstart

The supported out-of-the-box path is the quickstart: a tiny llm-d stack on a local
[kind](https://kind.sigs.k8s.io/) cluster using a simulated inference engine (no GPU, no
model download). Open the UI (`http://127.0.0.1:8000`) and type something like:

> Benchmark a small chat model on my laptop.

The agent will walk through, roughly:

1. **Interview you** briefly to pin down the use case and any quality-of-service targets.
2. **Probe** your environment (read-only, automatic, no prompt): is Docker up? A cluster?
   A stack already running?
3. **Read the real procedure** from the benchmark repo's docs (so it doesn't rely on
   memory).
4. **Propose a SessionPlan**, e.g. `spec=cicd/kind`, `harness=inference-perf`,
   `workload=sanity_random.yaml`, namespace `llmd-quickstart`, with the steps it intends to
   run. You approve it before anything happens.
5. **Install prerequisites** if needed (Docker + the kind binary), **clone** the repo,
   **build** its tooling, **create** the kind cluster. Each one prompts you to Approve.
6. **Stand up** the stack, **smoketest** it, then **run** the benchmark (output streams
   live).
7. **Summarize** the Benchmark Report in plain words: TTFT, inter-token latency, throughput,
   percentiles — tied back to what you asked for.
8. **Offer teardown** when you're done.

## Approvals: what runs automatically vs what asks first

- **Read-only checks run automatically** (probing the environment, listing the catalog,
  reading a doc, parsing a report, previews via `--dry-run`/`plan`). You'll still see them
  in the command trail.
- **Anything that changes your system asks first.** Installing Docker/kind, cloning,
  building tooling, creating/deleting the cluster, standup, run, teardown: each shows you
  the exact command and waits for you to click Approve (or Reject). If you Reject,
  the agent acknowledges and replans.

Nothing runs off-screen: the one-click Debug view (`>_`) reveals the executed-command trail
inline in the chat — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#debug-mode-ui).

## Reading the results

The agent only ever reports numbers from a validated Benchmark Report; it never invents
or estimates metrics. If a report is missing or invalid, it says so plainly. Common metrics:

- **TTFT** (time to first token): responsiveness; how long until the model starts replying.
- **TPOT / ITL** (time per output token / inter-token latency): how fast text streams once
  it starts.
- **Throughput**: output tokens per second; how much work the stack sustains.
- **Percentiles (p50/p90/p95/p99)**: tail behavior; p99 latency is what your slowest users
  feel.

If you gave the agent SLO targets (e.g. "p99 TTFT under 500 ms, at least 1000 tok/s"), it
can tell you whether each run meets the SLOs and give an honest goodput estimate (the
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
the full command trail are replayed. If you navigate away mid-run, an already-approved
benchmark keeps running in the background and its result shows up when you return.

## Good practices

- **Let it probe first.** The agent always senses the environment before proposing
  anything; don't skip the plan.
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
| `install_prereqs.sh` can't install Docker | It needs root or passwordless sudo; on WSL the Docker daemon may not auto-start. The agent relays the exact message: start Docker yourself and re-probe. |
| Standup fails with OOM / won't load | Ask for a capacity pre-flight first ("will this fit?"); use a lighter model/spec. |
| "command denied by command policy" | The agent tried something outside the deny-by-default policy. That's the safety net working; widening it is a reviewed config change (see [`API.md`](../reference/API.md)). |
| A run seems stuck | Use `observe_run_metrics` to see live CPU/memory; orchestrated runs are wall-clock bounded and classify timeouts/OOM/eviction automatically. |

## Want to change how the agent reasons?

Edit the Markdown/YAML under [`knowledge/`](../../knowledge/) — that's where all the agent's
judgment lives; no code changes needed.
