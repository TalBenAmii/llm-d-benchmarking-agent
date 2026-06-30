"""Expose every ``knowledge/`` file as an MCP resource under ``doc://knowledge/<stem>``.

Source of truth is the same glob the system-prompt builder uses (``app/agent/prompt.py``):
``knowledge/*.md|*.yaml|*.yml`` minus ``EXCLUDED_KNOWLEDGE_FILES``. The module-level functions are
pure and unit-testable; ``register_resources`` is the thin decorator wiring.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

from app.agent.prompt import _one_line_purpose
from app.tools.knowledge_access import EXCLUDED_KNOWLEDGE_FILES

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

_SCHEME = "doc"
_HOST = "knowledge"


def _knowledge_files(knowledge_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.md", "*.yaml", "*.yml"):
        files.extend(knowledge_dir.glob(pattern))
    files = [f for f in files if f.name not in EXCLUDED_KNOWLEDGE_FILES]
    return sorted(files, key=lambda p: p.stem)


def _mime(path: Path) -> str:
    return "text/markdown" if path.suffix == ".md" else "application/yaml"


def _stem_of_uri(uri: object) -> str:
    """Last path segment of a ``doc://knowledge/<stem>`` URI. String-parsed so it works whether the
    SDK hands us a ``pydantic.AnyUrl`` or a plain ``str``."""
    return str(uri).rstrip("/").rsplit("/", 1)[-1]


def list_resource_objects(knowledge_dir: Path) -> list[types.Resource]:
    return [
        types.Resource(
            uri=AnyUrl(f"{_SCHEME}://{_HOST}/{f.stem}"),
            name=f.stem,
            description=_one_line_purpose(f),
            mimeType=_mime(f),
        )
        for f in _knowledge_files(knowledge_dir)
    ]


def read_resource_contents(knowledge_dir: Path, uri: object) -> list[ReadResourceContents]:
    stem = _stem_of_uri(uri)
    index = {f.stem: f for f in _knowledge_files(knowledge_dir)}  # whitelist → no path traversal
    path = index.get(stem)
    if path is None:
        raise ValueError(f"unknown resource: {uri}")
    return [ReadResourceContents(content=path.read_text(encoding="utf-8"), mime_type=_mime(path))]


def register_resources(server: Server, knowledge_dir: Path) -> None:
    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return list_resource_objects(knowledge_dir)

    @server.read_resource()
    async def read_resource(uri: object) -> list[ReadResourceContents]:
        return read_resource_contents(knowledge_dir, uri)
