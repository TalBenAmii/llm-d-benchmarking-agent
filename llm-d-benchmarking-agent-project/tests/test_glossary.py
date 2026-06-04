"""Tests for the plain-language glossary: the markdown parser and the read-only /api/glossary route.

The parser is pure (mechanism only); the endpoint exposes it for the UI's glossary dialog and the
results-card metric explainers. Like the rest of /api, the route is hermetic — a FastAPI TestClient
against the real app wiring, no cluster or LLM.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main
from app.agent.glossary import build_glossary, parse_glossary
from app.config import get_settings


def test_parse_one_term_per_bullet():
    text = (
        "# Glossary\n\n"
        "- **kind** — Kubernetes IN Docker; a local cluster.\n"
        "- **harness** — the load generator.\n"
    )
    terms = parse_glossary(text)
    assert [t["term"] for t in terms] == ["kind", "harness"]
    assert terms[0]["definition"] == "Kubernetes IN Docker; a local cluster."
    assert terms[1]["definition"] == "the load generator."


def test_parse_multiple_terms_in_one_bullet():
    """A single bullet may pack several inline ``**term** — def`` segments (the metric line)."""
    text = (
        "- **TTFT** — time to first token. **TPOT/ITL** — per-output-token latency.\n"
        "  **throughput** — tokens or requests per second. **goodput** — requests meeting an SLO.\n"
    )
    terms = {t["term"]: t["definition"] for t in parse_glossary(text)}
    assert set(terms) == {"TTFT", "TPOT/ITL", "throughput", "goodput"}
    assert terms["TTFT"] == "time to first token."
    assert terms["goodput"] == "requests meeting an SLO."
    # The definition of one inline term must not bleed into the next.
    assert "TPOT" not in terms["TTFT"]


def test_parse_strips_markdown_and_collapses_wrap():
    text = (
        "- **spec / scenario** — a cluster+model config the CLI can stand up\n"
        "  (e.g. `cicd/kind`).\n"
    )
    (entry,) = parse_glossary(text)
    assert entry["term"] == "spec / scenario"
    # Backticks dropped; the wrapped second line collapsed into one space-joined line.
    assert "`" not in entry["definition"]
    assert "\n" not in entry["definition"]
    assert entry["definition"] == "a cluster+model config the CLI can stand up (e.g. cicd/kind)."


def test_parse_ignores_non_term_text():
    assert parse_glossary("") == []
    assert parse_glossary("# Heading only\n\njust a paragraph, no terms.\n") == []
    # Bold WITHOUT the dash separator is not a definition.
    assert parse_glossary("- **bold** but no dash here\n") == []


def test_build_glossary_reads_the_real_knowledge_file():
    """The shipped knowledge/glossary.md parses and carries the metric terms the UI relies on."""
    terms = build_glossary(get_settings().knowledge_dir)
    by_term = {t["term"].lower(): t["definition"] for t in terms}
    # The four metric terms the results-card explainers map onto must be present and split apart.
    for needed in ("ttft", "tpot/itl", "throughput", "goodput", "harness"):
        assert needed in by_term, f"glossary.md is missing '{needed}'"
    assert "first token" in by_term["ttft"].lower()
    # Every entry is a non-empty {term, definition} pair.
    assert all(t["term"] and t["definition"] for t in terms)


def test_build_glossary_missing_file_returns_empty(tmp_path):
    assert build_glossary(tmp_path) == []


def test_api_glossary_endpoint():
    with TestClient(main.app) as client:
        r = client.get("/api/glossary")
        assert r.status_code == 200
        terms = r.json()["terms"]
        assert isinstance(terms, list) and terms
        present = {t["term"].lower() for t in terms}
        assert {"ttft", "goodput"} <= present
