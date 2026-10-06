# llm-d Benchmarking Assistant — User Manual

The assistant turns a natural-language benchmark request into a reviewed plan, approved
execution and an explanation of structured results. This guide starts from the submitted
ZIP; cloning the project or downloading another project archive is unnecessary.

## 1. Extract and install

Use Linux or WSL2 (Ubuntu recommended), Bash, Python 3.11+, `unzip`, `curl`, and `uv`.
Dependency installation requires Internet access. No Docker, GPU, cluster or paid account
is needed for the fixture demo and deterministic checks below. Live chat requires your own
Claude or ChatGPT subscription login; credentials are deliberately absent from the ZIP.

If `uv` is missing, install it with `python3 -m pip install --user uv` (or the official
installer `curl -LsSf https://astral.sh/uv/install.sh | sh`), then open a new terminal.
`uv` can download Python 3.11 if it is not installed. From the directory holding the ZIP:

```bash
unzip project_submission.zip -d submission
cd submission/src/llm-d-benchmarking-agent-project
uv sync --locked --extra dev --python 3.11
cp .env.example .env
```

All subsequent commands, unless stated otherwise, run from this project directory.
The ZIP includes the application, its MCP adapter, and the three upstream repositories at
Dockerfile compatibility pins. Keep these sibling directories together. Leave `REPOS_DIR`
and `WORKSPACE_DIR` empty in `.env`; their defaults resolve within this extraction.

## 2. Start and try the account-free demo

```bash
bash scripts/run.sh --no-reload
```

Open **http://127.0.0.1:8000**. The main UI loads without a login, but sending a live chat
message requires step 3. Open **http://127.0.0.1:8000/static/preview.html** to inspect the
real result-card renderer with fixture data: plans, command trail, charts and comparisons.
This page labels its data as fixtures; it does not deploy or measure anything.
A recorded workflow is included at `docs/demo/llm-d-demo-live.mp4` (about 72 seconds).
Stop the server with **Ctrl+C**. If shutdown waits on a background probe, press Ctrl+C
again to force exit. For a busy port use `--port 8001` and open port 8001.

Replay the documented workflows without an LLM, credentials or a cluster:

```bash
.venv/bin/python scripts/eval/validate_flows.py
```

Each flow should report PASS and the process should exit 0. The replay uses the application
mechanisms with fake model/command adapters; it does not prove real inference performance.
The developer guide gives the full suite and optional CLI/MCP checks.

## 3. Configure a live conversation

Choose one provider; run its setup script after installation:

```bash
# Option A — Claude subscription; installs/configures Claude CLI if needed:
bash scripts/install/setup-claude-plan.sh

# OR option B — ChatGPT subscription; uses the packaged SDK's Codex CLI:
bash scripts/install/setup-codex-plan.sh
# On a headless machine, append --device-auth to the Codex setup command.
```

Follow the browser login instructions using your own account. The setup script updates
`.env`. Do not copy a developer's credentials into the submission. Choose an available
model in the UI; model availability depends on the account. Codex is supported by the local
server path; the documented in-cluster deployment uses Claude authentication.

For the first conversation, change **`SIMULATE=0` to `SIMULATE=1`** in `.env`. Keep
`HOST=127.0.0.1` and Auto-approve off. Restart using `bash scripts/run.sh --no-reload`.
Send “What can you help me benchmark?” to verify the login, then:

> Benchmark a small chat model on my laptop. Show me the plan first.

1. Answer the workload, load and latency-target questions.
2. Let the assistant read the catalog and probe the environment.
3. Review and approve the SessionPlan.
4. Review each command card; approve, reject or type a correction.
5. Inspect the results, Debug command trail and explanation of TTFT/throughput/SLOs.
6. Ask to compare runs, save a baseline, export results or clean up.

`SIMULATE=1` still needs a provider login and executes real read-only probes. Approved
mutations return synthetic results. Missing Docker/kubectl/CLI may appear in probes;
install the full toolchain for actual execution. In normal mode dedicated commands use a
policy and shell commands use a classifier; Auto-approve can waive cards. Keep the service
local: it has no multi-user authentication.

## 4. Deploy and run real commands (optional, requires infrastructure)

For the full host toolchain, from this extracted project directory:

```bash
bash scripts/install/install_local.sh --dev --no-clone --no-mcp --no-llm-setup
```

This retains the packaged source and installs system client tools plus the benchmark CLI
in `../llm-d-benchmark/.venv`. It can request sudo. Add `--prereqs` if Docker/kind are also
needed. Start Docker, set `SIMULATE=0`, restart the app and ask for the local kind quickstart.
Approve creation of only the intended cluster/namespace. The kind quickstart runs actual
Kubernetes and harness commands against a **CPU inference simulator**, without model weights.
Ask for teardown at the end; do not delete a cluster shared with other work.

A real model benchmark additionally needs a suitable GPU cluster, model access and any
required `HF_TOKEN`. The repository's `docs/guides/GPU_CLUSTER_RUNBOOK.md` covers that path.
For deploying the assistant itself with Docker/Helm, use
`docs/guides/CLUSTER_SERVICE_DEPLOY.md`. Image builds download their declared dependencies;
the ZIP is a source submission, not an offline binary distribution.

## 5. Configuration and troubleshooting

| Symptom / setting | Action |
|---|---|
| Chat fails while the UI loads | Complete provider login, check model access, restart. `/healthz` alone does not verify a conversational turn. |
| `/readyz` is 503 | Read its JSON reasons; check sibling source paths, writable workspace and provider configuration/login. |
| `LLM_PROVIDER` | `claude-agent-sdk` or `codex-sdk`; select via the setup script. |
| `AGENT_SDK_MODEL` / `CODEX_MODEL` | A model available to your selected account. |
| `REPOS_DIR` | Leave empty in the extracted sibling layout; otherwise absolute path to extracted `src/`. |
| `HOST` / `PORT` | Defaults `127.0.0.1` / `8000`; restart after edits. |
| `Permission denied` after extraction | Invoke launchers with `bash`; for upstream executable scripts use an unzip tool that preserves Unix modes. |
| `uv sync --locked` fails | Check Internet access and Python 3.11 support; do not silently regenerate `uv.lock`. |
| Results look unusually fast | Confirm whether the session uses synthetic simulation, CPU inference simulation or actual GPU inference. |

Reopen saved conversations from the sidebar. Runtime chats/results live under `workspace/`.
Never submit `.env`, credentials, virtual environments or personal chat history. More details
are in the repository's `docs/guides/TROUBLESHOOTING.md`.
