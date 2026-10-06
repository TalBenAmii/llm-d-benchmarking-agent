"""Codex subscription configuration and safe, account-free model metadata."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from openai_codex import CodexConfig
from openai_codex.client import CodexClient
from pydantic import BaseModel

from app.config import Settings

# All executable capabilities belong to our registry, where approvals and policy
# are enforced. Codex gets no independent environment, plugins, apps, or shell.
ISOLATION_CONFIG: dict[str, Any] = {
    "features.shell_tool": False,
    "features.unified_exec": False,
    "features.apps": False,
    "features.plugins": False,
    "features.remote_plugin": False,
    "features.multi_agent": False,
    "features.browser_use": False,
    "features.computer_use": False,
    "features.image_generation": False,
    "features.goals": False,
    "features.skip_host_skill_discovery": True,
    "web_search": "disabled",
    "project_doc_max_bytes": 0,
}


def codex_config(settings: Settings) -> CodexConfig:
    return CodexConfig(
        codex_bin=settings.codex_cli_path or None,
        client_name="llmd_benchmarking_agent",
        client_title="llm-d Benchmarking Assistant",
        config_overrides=('forced_login_method="chatgpt"', 'model_provider="openai"',
                          *(f"{key}={json.dumps(value)}" for key, value in ISOLATION_CONFIG.items())),
        env={"OPENAI_API_KEY": "", "CODEX_API_KEY": "", "OPENAI_BASE_URL": ""},
    )


class _ConfigResponse(BaseModel):
    config: dict[str, Any]


def isolated_thread_config(client: CodexClient) -> dict[str, Any]:
    # Empty tables merge with user settings. Explicitly disable every inherited
    # MCP server so it cannot bypass the application's approval boundary.
    inherited = client.request(
        "config/read", {"includeLayers": False}, response_model=_ConfigResponse,
    ).config
    config = dict(ISOLATION_CONFIG)
    for name in inherited.get("mcp_servers", {}):
        config[f'mcp_servers.{name}.enabled'] = False
    return config


def require_chatgpt(client: CodexClient) -> None:
    account = client.account_read().model_dump(mode="json", by_alias=True).get("account")
    if not account or account.get("type") != "chatgpt":
        raise RuntimeError("Codex needs a ChatGPT subscription login. Run `codex login` first.")


async def discover_codex(settings: Settings) -> dict[str, Any]:
    """No inference: check login and list model/effort choices from the bundled CLI."""
    client = CodexClient(codex_config(settings))

    def probe() -> dict[str, Any]:
        client.start()
        client.initialize()
        require_chatgpt(client)
        data = client.model_list().model_dump(mode="json", by_alias=True)
        return {"authenticated": True, "models": [
            {"id": m["model"], "label": m["displayName"],
             "efforts": [e["reasoningEffort"] for e in m["supportedReasoningEfforts"]
                         if e["reasoningEffort"] != "ultra"]}
            for m in data["data"]
        ]}

    try:
        return await asyncio.wait_for(asyncio.to_thread(probe), timeout=20)
    except Exception:
        # Neither raw transport errors nor account identity belong in an HTTP response.
        return {"authenticated": False, "models": []}
    finally:
        await asyncio.to_thread(client.close)
