"""The catalog must enumerate real on-disk repo contents (nothing hardcoded)."""
from __future__ import annotations

import pytest

from app.tools.setup.catalog import build_catalog, catalog_for_policy


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


def test_catalog_slice_for_policy(cat):
    sliced = catalog_for_policy(cat)
    assert set(sliced) == {"specs", "harnesses", "workloads"}
    assert "cicd/kind" in sliced["specs"]


def test_missing_repo_yields_empty(tmp_path):
    empty = build_catalog(tmp_path / "nope")
    assert empty["present"] is False
    assert empty["specs"] == [] and empty["harnesses"] == []


def _profile(tmp_path, harness, name):
    d = tmp_path / "workload" / "profiles" / harness
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("load: {}\n")


def test_plain_yaml_profile_is_catalogued(tmp_path):
    """A profile that ships as a plain ``*.yaml`` (no ``.in`` template) is still a valid
    ``-w`` value — upstream step_05_render_profiles resolves ``<name>`` before ``<name>.in`` —
    so the catalog must list it too, not only the ``*.yaml.in`` templates. Regression: the
    glob used to be ``*.yaml.in`` only, silently dropping plain-yaml profiles (e.g. the real
    repo's ``guide_predicted-latency-routing_1.yaml``) so the policy/plan rejected them."""
    _profile(tmp_path, "inference-perf", "sanity_random.yaml.in")  # template
    _profile(tmp_path, "inference-perf", "guide_plain_1.yaml")     # plain, no .in
    cat = build_catalog(tmp_path)
    wl = cat["workloads_by_harness"]["inference-perf"]
    assert "guide_plain_1.yaml" in wl       # the plain profile is NOT dropped
    assert "sanity_random.yaml" in wl       # the template still resolves to its CLI name
    assert "guide_plain_1.yaml" in cat["workloads"]
    # No `.yaml.in` ever leaks into a CLI name.
    assert not any(n.endswith(".yaml.in") for n in cat["workloads"])


def test_workload_present_in_both_forms_is_deduped(tmp_path):
    """If a profile exists as both ``foo.yaml`` and ``foo.yaml.in``, the catalog lists the
    single CLI name once (the two globs must not produce a duplicate)."""
    _profile(tmp_path, "inference-perf", "foo.yaml.in")
    _profile(tmp_path, "inference-perf", "foo.yaml")
    cat = build_catalog(tmp_path)
    assert cat["workloads_by_harness"]["inference-perf"].count("foo.yaml") == 1
