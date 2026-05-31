# llm-d Benchmarking Assistant Agent

A **local, chat-based assistant** that helps people who don't know the `llm-d-benchmark`
API run benchmarks anyway. You describe a use case in plain language
(*"benchmark a chat app with ~500 concurrent users"*); the agent interviews you, checks
your environment, deploys an `llm-d` stack if one isn't already running, runs the
benchmark, and explains the results.

It does this by driving the real `llmdbenchmark` CLI on your behalf — inside a strict
security sandbox, asking for your approval before anything that changes your system.

> **Status: MVP implemented & verified (2026-05-31).** The end-to-end vertical works —
> chat UI → agent loop → schema-validated, approval-gated tools → real `llmdbenchmark`
> execution → validated Benchmark Report summary. **44 tests pass.** The supported path is
> the `llm-d-benchmark` *quickstart* (local [kind](https://kind.sigs.k8s.io/) cluster,
> CPU-only simulated engine). A live LLM session needs an API key in `.env`; GPU / `llm-d`
> guide deploys, DoE sweeps, and multi-harness A/B come later. See
> [`plan.md`](plan.md#implementation-status) for the full status.

## Design in one line
**Thin code, thick agent.** The Python here is only *mechanism* — a chat UI, an agent
loop, a handful of tools, a command allowlist, and schema validation. All the *judgment*
(which spec/harness/workload, what flags, how to read results) lives in the LLM plus
editable knowledge files under [`knowledge/`](knowledge/). Reliability comes from
**schema-validated handoffs** at every boundary, not from hard-coded scripts.

## How it stays safe
- **Deny-by-default allowlist** ([`security/allowlist.yaml`](security/allowlist.yaml)): the
  agent can only run a small, explicit set of commands (`llmdbenchmark`, plus read-only
  `kubectl`/`kind`/`docker` probes, `git clone` of the llm-d repos, and `install.sh`).
- **No shell, ever.** Commands run as argv lists with `shell=False` — command injection is
  structurally impossible.
- **Per-action approval.** Read-only probes run automatically; every *mutating* command
  (standup, run, teardown) shows you the exact command and waits for you to click Approve.
- **Secrets stay server-side.** Your LLM API key never reaches the browser.

## The four determinism gates
1. The LLM can only act through **schema-validated tool calls**.
2. Before any deployment it proposes a **SessionPlan** you approve.
3. Any generated config is validated via the CLI's own `--dry-run` / `plan`.
4. Results are parsed from the repo's validated **Benchmark Report v0.2**, never from logs.

## Run it
```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY (or OpenAI-compatible creds)
pip install -e .              # or: uv pip install -e .
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Run the tests:
```bash
pip install -e '.[dev]'
pytest tests/
```

## Validate the agent runs the *right commands*
A **flow-validation harness** proves the agent drives the correct command sequence for
each end-to-end flow (the kind quickstart, the optimized-baseline guide, teardown,
benchmarking an already-running stack, dry-run previews, and out-of-policy refusals):

```bash
make validate        # deterministic & hermetic — no API key, Docker, kind, or repos needed
make flows           # list the known flows
make validate-live   # the real LLM drives each flow from natural-language input (needs a key)
```

It replays each flow through the **real** agent loop + allowlist + approval gating,
capturing every command without executing anything, and asserts the right commands run
with correct read-only/mutating gating. This is what
[`.github/workflows/agent-flow-validation.yml`](../.github/workflows/agent-flow-validation.yml)
runs on every push/PR. See [`docs/VALIDATION.md`](docs/VALIDATION.md) for the full design
and how to add a flow.

## Layout
| Path | What |
|---|---|
| `app/` | FastAPI backend: agent loop, tools, security, validation (mechanism only) |
| `security/allowlist.yaml` | The deny-by-default command policy (data) |
| `knowledge/` | The agent's editable brain — playbooks & heuristics (no code) |
| `ui/` | Static chat UI (HTML/JS/CSS) |
| `workspace/` | Gitignored runtime scratch (sessions, configs, logs) |
| `tests/` | pytest |

See [`CLAUDE.md`](CLAUDE.md) for the full set of working rules and
[`plan.md`](plan.md) for the implementation plan.

## Relationship to the repos
This project sits alongside two **read-only** repos and never modifies them:
```
kind-quickstart-guide/
├── llm-d/                            # deployment guides (read-only context)
├── llm-d-benchmark/                 # provides the llmdbenchmark CLI (read-only)
└── llm-d-benchmarking-agent-project/ # this project
```
