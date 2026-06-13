"""Start-of-chat suggestion chips (W1): load_suggestions returns the data file's chips,
filtered to well-formed {label, prompt} entries, and degrades to [] on a missing/garbled file.
"""
from __future__ import annotations

from app.agent import suggestions
from app.config import Settings


def test_load_suggestions_returns_the_chips():
    chips = suggestions.load_suggestions(Settings(_env_file=None))
    assert len(chips) == 5
    labels = [c["label"] for c in chips]
    assert "What can you do?" in labels
    # The start-of-chat entry point for co-authoring a spec + workload (author_spec_workload.md).
    assert any("spec" in lbl.lower() and "workload" in lbl.lower() for lbl in labels)
    for c in chips:
        assert set(c) == {"label", "prompt"}
        assert isinstance(c["label"], str) and c["label"]
        assert isinstance(c["prompt"], str) and c["prompt"]


def test_load_suggestions_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(suggestions, "_SUGGESTIONS_PATH", tmp_path / "does_not_exist.yaml")
    assert suggestions.load_suggestions(Settings(_env_file=None)) == []


def test_load_suggestions_garbled_file_returns_empty(monkeypatch, tmp_path):
    bad = tmp_path / "suggestions.yaml"
    bad.write_text("chips: [this is : not : valid : yaml")
    monkeypatch.setattr(suggestions, "_SUGGESTIONS_PATH", bad)
    assert suggestions.load_suggestions(Settings(_env_file=None)) == []


def test_load_suggestions_wrong_shape_and_partial_entries(monkeypatch, tmp_path):
    # Top-level is a list, not a dict with `chips` -> [].
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text("- just\n- a\n- list\n")
    monkeypatch.setattr(suggestions, "_SUGGESTIONS_PATH", wrong)
    assert suggestions.load_suggestions(Settings(_env_file=None)) == []

    # `chips` present but entries are malformed: only the complete one survives, coerced to str.
    partial = tmp_path / "partial.yaml"
    partial.write_text(
        "chips:\n"
        "  - {label: ok, prompt: do it}\n"
        "  - {label: no-prompt}\n"
        "  - {prompt: no-label}\n"
        "  - not-a-dict\n"
    )
    monkeypatch.setattr(suggestions, "_SUGGESTIONS_PATH", partial)
    out = suggestions.load_suggestions(Settings(_env_file=None))
    assert out == [{"label": "ok", "prompt": "do it"}]
