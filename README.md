# llm-d Benchmarking Assistant

Benchmark [`llm-d`](https://github.com/llm-d/llm-d) by describing what you want in plain
English. No `llm-d-benchmark` expertise needed.

You say *"benchmark a chat app for ~500 concurrent users, p99 latency under 500 ms."* The
agent asks a couple of questions, checks your environment, shows you a plan, deploys an
`llm-d` stack if needed, runs the benchmark, and explains the results in plain words.
Nothing changes your system without your approval.

Licensed [Apache-2.0](LICENSE).

## Demo

https://github.com/user-attachments/assets/674b3eb5-6474-450e-b9dd-5bec2aa10c86

## Quick start

> **Proof of concept.** The tested setup runs the assistant on a local
> [kind](https://kind.sigs.k8s.io/) cluster. A real remote/GPU cluster uses the same deploy
> but hasn't been tested yet. See
> [CLUSTER_SERVICE_DEPLOY.md](llm-d-benchmarking-agent-project/docs/guides/CLUSTER_SERVICE_DEPLOY.md).

One command builds the image, deploys to a local kind cluster, and opens the chat UI:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/install.sh)
```

It auto-installs missing prerequisites (docker/kind/kubectl/helm, asks for `sudo`) and
helps you connect your Claude subscription. Useful flags: `--no-open`,
`--no-build`, `--cluster NAME` (`./install.sh --help` lists the rest). Tear down with
`kind delete cluster --name bench-agent`.

You can also run the app directly on your host:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/scripts/install/install_local.sh)
cd ~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project && ./scripts/run.sh --open
```

**Authentication.** The engine uses the Claude Agent SDK with your Claude
Pro/Max subscription. No API key is needed. Both installers help set up a `claude` CLI login
locally, or a `claude setup-token` token (`CLAUDE_CODE_OAUTH_TOKEN`) for the in-cluster
service. To try the whole workflow without touching a cluster, set `SIMULATE=1` in `.env`:
read-only commands still run, while approved changes return simulated results.

**Codex option for the local server.** You can also use your ChatGPT subscription through
the official Codex Python SDK. In the project directory, run `uv sync`, then
`./scripts/install/setup-codex-plan.sh` and `./scripts/run.sh`. The setup reuses your
ChatGPT login and selects Codex in `.env`. See the
[Codex setup and configuration](llm-d-benchmarking-agent-project/README.md#run-with-codex-and-your-chatgpt-subscription).

## Use it from Claude Code (MCP)

[`llm-d-bench-mcp`](https://github.com/TalBenAmii/llm-d-bench-mcp)
exposes the same tools and knowledge as an MCP server. One command installs and registers it
(the local installer above already does this by default):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-bench-mcp/main/scripts/install.sh)
```

It authenticates through your `claude` CLI login, without an API key.

## How a session goes

1. **Interview:** the agent asks two or three questions about your use case and SLOs.
2. **Probe:** read-only checks of your environment run automatically.
3. **Plan:** you review the proposed steps and capacity checks, then approve the plan.
4. **Run:** the agent deploys the stack, runs a smoke test, and starts the benchmark.
   Output streams live, and commands that change your system wait for approval.
5. **Explain:** the agent reads the validated report and explains the results, for example:
   *"Median TTFT was 180 ms and p99 was 320 ms, both under your 400 ms target."*
6. **Teardown:** it offers to remove the resources created for the run.

Beyond single runs it can compare runs and harnesses, sweep configs to find the best one for
your SLOs, track trends across sessions, check "will this model fit my GPU?", export
shareable HTML reports and reproducible provenance bundles, and orchestrate runs as
Kubernetes Jobs. See the feature list and verification steps in
[FEATURES.md](llm-d-benchmarking-agent-project/docs/reference/FEATURES.md).

## How it stays safe

- **Deny-by-default command policy:** the agent's command tools can only run an explicit
  allowlist ([command_policy.yaml](llm-d-benchmarking-agent-project/security/command_policy.yaml)).
- **Per-action approval:** read-only commands auto-run; every mutating or unknown command
  shows you the exact command and waits for Approve/Reject. Everything appears in the chat.
- **Secrets stay server-side:** keys live in the backend `.env`; the browser never sees
  them, and child-process env is scrubbed.
- **Validated results:** reported metrics come from the schema-validated Benchmark Report;
  if a report is missing or invalid, it says so.

## Under the hood

Python handles the chat UI, agent loop, tool execution, command policy, and schema validation.
The LLM makes benchmarking decisions using the Markdown and YAML files in
[`knowledge/`](llm-d-benchmarking-agent-project/knowledge/). Edit those files to change its
guidance without changing the code.

The workspace contains the project and the upstream repos it reads at runtime:

```
llm-d-benchmarking-agent/
├── llm-d/                            # deployment guides (read-only upstream)
├── llm-d-benchmark/                  # the llmdbenchmark CLI (read-only upstream)
├── llm-d-skills/                     # upstream skills library (read-only)
├── llm-d-bench-mcp/                  # the MCP server (owned sibling, its own git repo)
└── llm-d-benchmarking-agent-project/ # the project: app code, knowledge, docs, tests
```

Run the deterministic checks without an API key, cluster, or Docker:

```bash
cd llm-d-benchmarking-agent-project
make validate     # replay every flow through the real agent loop, executing nothing
pytest tests/     # the full suite
```

## Docs

| Doc | For |
|---|---|
| [USER_GUIDE.md](llm-d-benchmarking-agent-project/docs/guides/USER_GUIDE.md) | Installation, configuration, and running a benchmark |
| [GPU_CLUSTER_RUNBOOK.md](llm-d-benchmarking-agent-project/docs/guides/GPU_CLUSTER_RUNBOOK.md) | From CPU-sim to a real single-GPU cluster |
| [DEPLOYMENT.md](llm-d-benchmarking-agent-project/docs/guides/DEPLOYMENT.md) | Local and in-cluster deploy, config, secrets |
| [ARCHITECTURE.md](llm-d-benchmarking-agent-project/docs/reference/ARCHITECTURE.md) | Design, modules, interactions, and build tools |
| [FEATURES.md](llm-d-benchmarking-agent-project/docs/reference/FEATURES.md) | Features and how to verify them |

Full index: [docs/README.md](llm-d-benchmarking-agent-project/docs/README.md).

---

The llm-d name and logo belong to the [llm-d project](https://github.com/llm-d/llm-d)
(Apache-2.0); the logo appears here only to identify the tool this app drives. This is an
independent project, not affiliated with or endorsed by llm-d.
