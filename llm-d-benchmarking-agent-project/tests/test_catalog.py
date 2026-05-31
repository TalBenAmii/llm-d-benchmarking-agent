"""The catalog must enumerate real on-disk repo contents (nothing hardcoded)."""
from __future__ import annotations

import pytest

from app.tools.catalog import build_catalog, catalog_for_allowlist


@pytest.fixture(scope="module")
def cat(bench_repo):
    if not bench_repo.is_dir():
        pytest.skip("llm-d-benchmark repo not present")
    return build_catalog(bench_repo)


def test_catalog_present(cat):
    assert cat["present"] is True


def test_specs_include_kind_and_guides(cat):
    assert "cicd/kind" in cat["specs"]
    assert "guides/optimized-baseline" in cat["specs"]


def test_harnesses_enumerated(cat):
    assert "inference-perf" in cat["harnesses"]
    assert "nop" in cat["harnesses"]


def test_workloads_include_sanity_random(cat):
    assert "sanity_random.yaml" in cat["workloads"]
    assert "sanity_random.yaml" in cat["workloads_by_harness"]["inference-perf"]


def test_catalog_slice_for_allowlist(cat):
    sliced = catalog_for_allowlist(cat)
    assert set(sliced) == {"specs", "harnesses", "workloads"}
    assert "cicd/kind" in sliced["specs"]


def test_missing_repo_yields_empty(tmp_path):
    empty = build_catalog(tmp_path / "nope")
    assert empty["present"] is False
    assert empty["specs"] == [] and empty["harnesses"] == []
