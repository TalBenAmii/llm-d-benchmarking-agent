"""Enumerate the legal universe of choices straight from the llm-d-benchmark repo on
disk, so the agent (and the allowlist) can only ever name things that actually exist.

Nothing here is hardcoded knowledge — it is a live directory listing. If the repo is
absent, every list is empty (and the agent should clone it first).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_HARNESS_SUFFIXES = ("-llm-d-benchmark.sh", "-llm-d-benchmark.py")


def build_catalog(bench_repo: str | Path) -> dict[str, Any]:
    repo = Path(bench_repo)
    specs = _specs(repo)
    harnesses = _harnesses(repo)
    workloads_by_harness = _workloads(repo, harnesses)
    workloads_union = sorted({w for ws in workloads_by_harness.values() for w in ws})
    return {
        "present": repo.is_dir(),
        "repo_path": str(repo),
        "specs": specs,
        "harnesses": harnesses,
        "workloads": workloads_union,
        "workloads_by_harness": workloads_by_harness,
        "scenarios": _scenarios(repo),
    }


def _specs(repo: Path) -> list[str]:
    base = repo / "config" / "specification"
    if not base.is_dir():
        return []
    out = []
    for p in base.rglob("*.yaml.j2"):
        rel = p.relative_to(base)
        name = str(rel).removesuffix(".yaml.j2")
        out.append(name)
    return sorted(out)


def _harnesses(repo: Path) -> list[str]:
    # Authoritative source: the profile subdirectories (one per harness).
    prof = repo / "workload" / "profiles"
    if prof.is_dir():
        return sorted(d.name for d in prof.iterdir() if d.is_dir())
    # Fallback: derive from harness driver filenames.
    hdir = repo / "workload" / "harnesses"
    names: set[str] = set()
    if hdir.is_dir():
        for f in hdir.iterdir():
            for suf in _HARNESS_SUFFIXES:
                if f.name.endswith(suf):
                    names.add(f.name[: -len(suf)])
    return sorted(names)


def _workloads(repo: Path, harnesses: list[str]) -> dict[str, list[str]]:
    prof = repo / "workload" / "profiles"
    out: dict[str, list[str]] = {}
    if not prof.is_dir():
        return out
    for h in harnesses:
        hdir = prof / h
        if not hdir.is_dir():
            continue
        # Profile files are usually templates (`sanity_random.yaml.in`), but some ship as a
        # plain rendered `*.yaml` (no `.in`). The CLI takes the `*.yaml` name for BOTH: it
        # looks for `<name>` first and only falls back to `<name>.in` (upstream
        # step_05_render_profiles), so a plain `*.yaml` profile is a valid `-w` value too —
        # collect both and dedupe by their CLI name so neither kind is silently dropped.
        names = {f.name.removesuffix(".in") for f in hdir.glob("*.yaml.in")}
        names |= {f.name for f in hdir.glob("*.yaml")}
        if names:
            out[h] = sorted(names)
    return out


def _scenarios(repo: Path) -> list[str]:
    base = repo / "config" / "scenarios"
    if not base.is_dir():
        return []
    out = []
    for p in base.rglob("*.yaml"):
        rel = p.relative_to(base)
        out.append(str(rel).removesuffix(".yaml"))
    return sorted(out)


def catalog_for_allowlist(catalog: dict[str, Any]) -> dict[str, list[str]]:
    """Slice the catalog down to what the allowlist's ref_catalog checks need."""
    return {
        "specs": catalog.get("specs", []),
        "harnesses": catalog.get("harnesses", []),
        "workloads": catalog.get("workloads", []),
    }
