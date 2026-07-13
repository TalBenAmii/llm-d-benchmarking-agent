"""Product-boundary guard — the debug-only ``harnesses/`` dir must never ship.

``harnesses/local-cluster/`` holds local mock-cluster scaffolding (multi-node kind, kwok
fake-GPU nodes, setup/teardown) used to exercise the agent's multi-GPU **orchestration /
scheduling** paths WITHOUT real hardware. It is debugging infrastructure, not product.

These tests turn "it never reaches the shipped artifact" into a *checked invariant*
rather than a hope:

1. the production image's build context (the Dockerfile ``COPY`` set) only pulls the product
   dirs (app — which includes the static UI at app/ui — security, knowledge, scripts) + the
   metadata files (pyproject.toml, README.md, NOTICE) — never ``harnesses/``;
2. ``.dockerignore`` excludes ``harnesses/`` (belt-and-suspenders on the COPY policy, and
   it makes the build context *physically unable* to include it);
3. no module under ``app/`` imports from ``harnesses/`` (the product can't depend on the mock).

If a future change wires the mock into the product, one of these fails loudly.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The ONLY build-context sources the production image is allowed to COPY. Anything else
# (notably ``harnesses/``) entering the image is a product-boundary leak. Keep in sync with
# the Dockerfile COPY lines: the runtime dirs (app — the static UI ships inside it at app/ui —
# security, knowledge, and scripts — the policy wires scripts/*.py to run via the bundled
# CLI venv), the two files pip needs in the builder (pyproject.toml, README.md), and the NOTICE
# attribution file. The sibling repos + toolchain are cloned/installed via RUN layers, never
# COPY'd from the context.
ALLOWED_COPY_SOURCES = {
    "pyproject.toml", "README.md", "app", "security", "knowledge", "scripts", "NOTICE",
}


def _context_copy_sources(dockerfile: str) -> list[str]:
    """Build-context sources of every ``COPY`` (skipping ``COPY --from=…`` stage copies)."""
    sources: list[str] = []
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY "):
            continue
        tokens = line.split()[1:]  # drop the "COPY" keyword
        if any(t.startswith("--from=") for t in tokens):
            continue  # copies from an earlier build stage, not from the context
        tokens = [t for t in tokens if not t.startswith("--")]  # drop flags (e.g. --chown=)
        # The last token is the destination; everything before it is a source path.
        sources.extend(src.rstrip("/") for src in tokens[:-1])
    return sources


def test_dockerfile_copies_only_product_dirs():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    leaked = sorted(set(_context_copy_sources(dockerfile)) - ALLOWED_COPY_SOURCES)
    assert not leaked, (
        f"Dockerfile COPYs non-product sources into the image: {leaked}. "
        "Debug-only scaffolding (harnesses/) must never be baked into the product image."
    )


def test_dockerignore_excludes_harnesses():
    patterns = {
        ln.strip().rstrip("/")
        for ln in (PROJECT_ROOT / ".dockerignore").read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    assert "harnesses" in patterns, (
        ".dockerignore must exclude `harnesses/` so the mock-cluster harness never enters "
        "the build context."
    )


def test_app_does_not_import_harnesses():
    pat = re.compile(r"^\s*(?:from|import)\s+harnesses\b")
    offenders = [
        f"{py.relative_to(PROJECT_ROOT)}:{i}"
        for py in (PROJECT_ROOT / "app").rglob("*.py")
        for i, line in enumerate(py.read_text().splitlines(), start=1)
        if pat.match(line)
    ]
    assert not offenders, (
        f"product code imports the debug-only mock-cluster harness: {offenders}. "
        "app/ must never depend on harnesses/."
    )
