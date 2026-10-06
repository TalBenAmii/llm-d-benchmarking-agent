#!/usr/bin/env python3
"""Exercise the actual launcher, HTTP, WebSocket, and MCP stdio without LLM calls."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
import websockets
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[2]


async def protocols(base: str, port: int) -> None:
    async with websockets.connect(f'ws://127.0.0.1:{port}/ws') as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        assert first.get('type'), first
        print('WebSocket connected:', first['type'])
    params = StdioServerParameters(
        command=str(PROJECT / '.venv/bin/llm-d-bench-mcp'),
        cwd=str(PROJECT), env={**os.environ, 'REPOS_DIR': str(PROJECT.parent)},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        resources = await session.list_resources()
        prompts = await session.list_prompts()
        assert any(t.name == 'list_catalog' for t in tools.tools)
        result = await session.call_tool('list_catalog', {})
        assert not result.isError, result
        catalog = json.loads(result.content[0].text)
        assert catalog.get('present') and catalog.get('specs'), catalog
        await session.read_resource('doc://knowledge/analysis')
        print(f'MCP stdio: {len(tools.tools)} tools, {len(resources.resources)} resources, '
              f'{len(prompts.prompts)} prompts; catalog and knowledge read passed')


def main() -> None:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    kubeconfig = PROJECT / 'workspace-smoke-kubeconfig.yaml'
    kubeconfig.write_text('apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\ncurrent-context: \"\"\n')
    env = {**os.environ, 'LLM_PROVIDER': 'claude-agent-sdk', 'SIMULATE': '1',
           'KUBECONFIG': str(kubeconfig)}
    with (PROJECT / 'workspace-smoke.log').open('w') as log:
        server = subprocess.Popen(['bash', 'scripts/run.sh', '--no-reload', '--port', str(port)],
                                  cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        try:
            base = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 60
            with httpx.Client(base_url=base, timeout=5) as client:
                while True:
                    try:
                        client.get('/healthz').raise_for_status()
                        break
                    except httpx.HTTPError:
                        if server.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('Launcher failed; inspect workspace-smoke.log') from None
                        time.sleep(0.25)
                for path in ('/', '/healthz', '/readyz', '/metrics', '/api/provider',
                             '/api/history', '/static/preview.html'):
                    response = client.get(path)
                    response.raise_for_status()
                    print(path, response.status_code)
            asyncio.run(protocols(base, port))
        finally:
            os.killpg(server.pid, signal.SIGINT)
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # Bound cleanup even if a provider/background thread keeps Python alive.
                os.killpg(server.pid, signal.SIGKILL)
                server.wait(timeout=5)
                print('Smoke server required forced cleanup after 20 seconds')


if __name__ == '__main__':
    main()
