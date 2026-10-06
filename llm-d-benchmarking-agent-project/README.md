# llm-d Benchmarking Assistant Agent

This folder holds all of the project's code and docs. See the overview, installation steps, and security model in the
[**repository root**](../README.md).

Useful links:

- **Get started:** [Quick start](../README.md#quick-start)
- **Features and verification:** [`docs/reference/FEATURES.md`](docs/reference/FEATURES.md)
- **MCP server for Claude Code:** [`docs/reference/MCP.md`](docs/reference/MCP.md)
- **Documentation index:** [`docs/README.md`](docs/README.md)

## Run with Codex and your ChatGPT subscription

```bash
uv sync --extra dev
./scripts/install/setup-codex-plan.sh
./scripts/run.sh
```

The setup script uses the Codex CLI bundled with the official `openai-codex` Python SDK.
It reuses your local ChatGPT login, or opens login when needed. Pass `--device-auth` on
a headless host. It selects `LLM_PROVIDER=codex-sdk` in `.env`; no OpenAI API key is used.
Usage counts against your ChatGPT plan's Codex allowance.

`CODEX_MODEL` defaults to `gpt-6.1-sol`, and `CODEX_EFFORT` to `medium`. The browser's
model picker gets its model and effort choices from Codex at startup. A completed turn
verifies access to the selected model. `/readyz` also checks that Codex has a ChatGPT login.
After logging in or changing the provider, restart the server.

The SDK runs the model loop. The application's existing tools still enforce command
policy, schema validation, and browser approval. Codex's independent shell, plugins,
apps, and environment access are disabled. Conversation thread IDs and token totals
are persisted alongside the existing chat transcript. Stop cancels the SDK process and
any pending application tool call. Messages typed mid-turn are queued as follow-ups.

Claude remains available with `LLM_PROVIDER=claude-agent-sdk` and its existing
`AGENT_SDK_*` settings. Each provider has a separate saved conversation handle.
The Helm/service install scripts still configure Claude authentication; the Codex
setup above is for the local server, not an automatic migration of deployed services.

SDK documentation: [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) and
[subscription authentication](https://learn.chatgpt.com/docs/auth).

## Reproducible upstream versions

The local installer's fresh clones use the `LLMD_REF`, `BENCH_REF` and `SKILLS_REF`
revisions in `Dockerfile`, matching the container baseline. Existing checkouts are
preserved. Set `LLMD_REVISION`, `BENCH_REVISION` or `SKILLS_REVISION` only when deliberately
testing another version. Moving upstream HEAD has changed report paths, scenario structure
and workload identifiers; pulling upstream alone does not establish compatibility.

The current baseline is benchmark `a75d91e`, llm-d `51f59431`, and skills `d23b14c`.
It uses the `guides/wide-ep` name, nested scenario settings, GuideLLM `spec` profiles,
and Benchmark Report 0.2.1. Capacity preflight and profile inspection also retain support
for older flat formats. Validate a different checkout with the full suite; `/readyz`
checks repository availability and authentication, not upstream format compatibility.

`SIMULATE=1` still needs a working Claude login: read-only probes execute normally,
and approved mutations return synthetic results when using the Claude provider.
With Codex, it needs a working ChatGPT login instead. Simulation changes command
execution, not model authentication. Claude's `/readyz` check does not verify its login.
