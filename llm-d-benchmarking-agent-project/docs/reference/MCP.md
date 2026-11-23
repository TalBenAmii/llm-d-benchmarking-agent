# Use the benchmarking tools from Codex CLI or Claude Code

The `llm-d-bench` MCP server lets either CLI use the agent's tools, knowledge resources,
and workflow prompts. The CLI starts the server locally over stdio and supplies the model
and login. The browser app's `LLM_PROVIDER` setting applies only to browser sessions.

## Install and verify

Choose the command for your client. The installer fetches the engine and its sibling
repositories and installs the server in the agent project's virtual environment.

**Codex CLI.** Install the `codex` executable first and sign in to Codex.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-bench-mcp/main/scripts/install.sh) --client codex
codex mcp get llm-d-bench
```

Start a new Codex session and open `/mcp` to check that `llm-d-bench` is connected.
Ask “Use llm-d-bench to list the available benchmark harnesses.” The catalog is read-only
and works without a cluster. A fresh registration configures Codex to prompt for each tool
call, including this one, and allows up to one hour for a benchmark tool to return.

**Claude Code (default).** Run the installer and select the Claude Code registration when
prompted. Use your Claude Code login and check the registration with `claude mcp list`.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-bench-mcp/main/scripts/install.sh)
```

## If you already installed the agent

The agent's `scripts/install/install_local.sh` installs the MCP server and attempts to
register it with Claude Code by default (`--no-mcp` skips this step). To add Codex to an
existing installation, register the same executable in Codex and apply the documented
approval and timeout settings. See [manual Codex setup](https://github.com/TalBenAmii/llm-d-bench-mcp#manual-config-codex-cli).

The server runs with your local kubeconfig. Each allowed tool call can execute several
commands, so review tool permissions in the CLI you use. For the tool list, workflow
prompts, and approval behavior, see the [MCP server README](https://github.com/TalBenAmii/llm-d-bench-mcp#what-your-agent-gets).
