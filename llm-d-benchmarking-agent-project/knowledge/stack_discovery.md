# Stack discovery — capture the live stack as BR-v0.2 components (when to use it)

`discover_stack` runs the **standalone stack-discovery tool**, `llm-d-discover <url> -f
benchmark-report`, against an OpenAI-compatible endpoint URL. It traces the **live** llm-d
stack behind that URL and returns BR-v0.2 `scenario.stack[]` components — model, role
(prefill/decode/replica), replica count, parallelism (tp/pp/dp/ep/workers), accelerator, and
the request-router (EPP). This is **OPTIONAL, richer ENVIRONMENT capture**: it tells you what
is *actually deployed*, in more structured detail than the agent's own endpoint probing can
infer.

It is **read-only and auto-runs**: the upstream tool connects with its **own read-only
Kubernetes RBAC** and applies **env-var redaction** to the configs it reads, so a discovery
trace only READS the live stack and never mutates the cluster.

## When to use it (vs endpoint probing)

Endpoint probing — `probe_environment` and `check_endpoint_readiness` — is the
**unconditional default** for sensing the environment. It answers "is a stack present in this
namespace, and is its endpoint actually serving?" That is what you run first, every time.
`discover_stack` does **not** replace it; it **complements** it. Reach for `discover_stack`
when you want a precise, structured picture of *what* is deployed:

- The user points you at a **pre-existing or remote stack** they did NOT stand up (so you have
  no SessionPlan / scenario describing it), and you need to know its model, prefill/decode
  split, replica counts and parallelism before benchmarking or comparing.
- You want to **confirm what really got deployed** matches what was intended (e.g. after a
  standup, or to debug a "why is throughput low" question — is decode TP what you expected?).
- You are **documenting / capturing** the environment for a report or a hand-off.

Do **not** use it as a readiness gate (use `check_endpoint_readiness`), and do not block a
benchmark on it — if discovery can't run, fall back to endpoint probing and proceed.

## Preconditions

`llm_d_stack_discovery` is a **self-contained subpackage** of llm-d-benchmark with its OWN
`setup.py`/`requirements.txt`. **`install.sh` does NOT install it.** The `llm-d-discover`
console script must be installed into the benchmark venv first:

```
pip install -e llm-d-benchmark/llm_d_stack_discovery   # into the benchmark venv
```

If it isn't installed, `discover_stack` returns `ran: False` with this hint — relay it and fall
back to endpoint probing.

## Inputs

- `endpoint_url` (REQUIRED) — the OpenAI-compatible endpoint to trace (e.g.
  `https://model.example.com/v1`, or an in-cluster service URL). Same value the benchmark `run`
  takes via `-U/--endpoint-url`.
- `kubeconfig` (optional) — a NON-SECRET kubeconfig FILE path to target a remote cluster. The
  secret cluster-by-URL+TOKEN route is **not** exposed here; it stays backend-only.
- `context` (optional) — a kube context name.
- `filter_type` (optional) — narrow the discovered components by type (e.g. `Pod`, `Service`,
  `vllm`).

## Reading the result

On success you get `stack` with FACTS only — `component_count`, `inference_engine_count`,
`models`, `roles`, `tools`, and per-engine `inference_engines` (label / model / role / replicas
/ accelerator + nested parallelism). The full discovery JSON is written to
`discovery_output_path`, and a `{scenario: {stack: [...]}}` capture to
`scenario_capture_path`, both in the session workspace (the read-only repos are never written).

**Note the scope:** `--output-format benchmark-report` emits the **stack only** — it is the
`scenario.stack[]` slice of a BR-v0.2 report, with **no `run`/`results` block**. So it captures
the ENVIRONMENT, not benchmark results. To get actual metrics you still run a benchmark and
parse its report with `locate_and_parse_report` / `analyze_results`. The facts here are
descriptive; whether a given shape (e.g. decode TP=1 on one replica) is right for the user's
goal is **your** judgment to narrate, not a verdict this tool returns.
