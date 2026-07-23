"""Phase 14 — quality gates wiring (ruff + mypy + coverage).

Hermetic config/wiring tests (like ``tests/platform/test_packaging.py`` / ``tests/platform/test_ops_docs.py``):
these do NOT re-run ruff/mypy/coverage (the integrator does that — the gates ARE the test).
Instead they assert the *config and wiring* the gates depend on actually exist and stay
coherent, so a future edit can't silently drop a gate:

  * ``pyproject.toml`` declares the dev tools (ruff / mypy / pytest-cov) and the
    ``[tool.ruff]`` / ``[tool.mypy]`` / ``[tool.coverage]`` config blocks.
  * The ``Makefile`` exposes ``lint`` / ``typecheck`` / ``coverage`` targets and the
    coverage target carries a ``--cov-fail-under`` gate.
  * The CI workflow runs ruff + mypy + the coverage-gated suite AND keeps the existing
    hermetic flow-validation job.
  * The coverage threshold is a single number shared by the Makefile and CI (no drift),
    and it sits a few points below 100 (a real gate, not a hardcoded 80).

No network, no cluster, no subprocess — pure file/TOML/YAML reads.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import yaml

from app.config import PROJECT_ROOT

PYPROJECT = Path(PROJECT_ROOT) / "pyproject.toml"
MAKEFILE = Path(PROJECT_ROOT) / "Makefile"
CI_WORKFLOW = Path(PROJECT_ROOT).parent / ".github" / "workflows" / "agent-flow-validation.yml"


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def _makefile_text() -> str:
    assert MAKEFILE.exists(), f"Makefile missing: {MAKEFILE}"
    return MAKEFILE.read_text()


def _coverage_threshold_from_makefile() -> int:
    """The single source of truth for the gate lives on the Makefile's COV_FAIL_UNDER."""
    m = re.search(r"^COV_FAIL_UNDER\s*:?=\s*(\d+)", _makefile_text(), re.MULTILINE)
    assert m, "Makefile must define COV_FAIL_UNDER"
    return int(m.group(1))


# ---- pyproject: dev tools present ------------------------------------------

def test_dev_extras_declare_quality_tools():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    names = {re.split(r"[<>=!~ \[]", d, maxsplit=1)[0].lower() for d in dev}
    for tool in ("ruff", "mypy", "pytest-cov"):
        assert tool in names, f"dev extras must include {tool!r}; got {sorted(names)}"


# ---- pyproject: tool config blocks present ---------------------------------

def test_ruff_config_present_and_sensible():
    tool = _pyproject()["tool"]
    assert "ruff" in tool, "[tool.ruff] config block must exist"
    select = tool["ruff"]["lint"]["select"]
    # A real ruleset, not just the implicit default — covers bugs (F/B) and imports (I).
    for code in ("F", "I", "B"):
        assert code in select, f"ruff lint.select should enable {code!r}; got {select}"
    # E501 is deliberately relaxed (existing style): keep that decision explicit.
    assert "E501" in tool["ruff"]["lint"]["ignore"]


def test_mypy_config_scoped_to_app_not_blanket_strict():
    mypy = _pyproject()["tool"]["mypy"]
    assert mypy["files"] == ["app"], "mypy should target app/"
    # Meaningful but achievable: NOT --strict over the whole tree.
    assert mypy.get("strict") is not True
    assert mypy.get("check_untyped_defs") is True
    # SDK-only engine: the old vendor-SDK provider carve-outs are gone — no relaxed modules.
    assert not mypy.get("overrides"), "no mypy overrides expected after the provider removal"


def test_coverage_config_present():
    cov = _pyproject()["tool"]["coverage"]
    assert cov["run"]["source"] == ["app"]


# ---- Makefile targets ------------------------------------------------------

def test_makefile_has_quality_targets():
    text = _makefile_text()
    for target in ("lint:", "typecheck:", "coverage:"):
        assert target in text, f"Makefile missing target {target!r}"
    assert "ruff check" in text
    assert "mypy app" in text
    # The coverage target must actually gate.
    assert "--cov-fail-under" in text


# ---- coverage threshold is a real, non-hardcoded gate ----------------------

def test_coverage_threshold_is_a_real_gate():
    n = _coverage_threshold_from_makefile()
    # A meaningful floor (set just under the ~89% measured baseline), not a token value
    # and not an unreachable 100.
    assert 70 <= n < 100, f"coverage gate {n} should be a sensible threshold below 100"


# ---- CI wires all three gates + keeps the hermetic job ---------------------

def test_ci_runs_all_three_gates_and_keeps_hermetic_job():
    assert CI_WORKFLOW.exists(), f"CI workflow missing: {CI_WORKFLOW}"
    data = yaml.safe_load(CI_WORKFLOW.read_text())
    jobs = data["jobs"]

    # The existing hermetic flow-validation job is preserved unchanged-in-spirit:
    # no submodules, runs the gating pytest suite.
    assert "flow-validation" in jobs, "must KEEP the hermetic flow-validation job"
    fv_runs = "\n".join(s.get("run", "") for s in jobs["flow-validation"]["steps"])
    assert "pytest" in fv_runs

    # A job runs ruff + mypy + the coverage-gated suite (all three in one job here).
    all_runs = "\n".join(
        s.get("run", "")
        for job in jobs.values()
        for s in job.get("steps", [])
    )
    assert "ruff check" in all_runs, "CI must run ruff"
    assert "mypy app" in all_runs, "CI must run mypy app"
    assert "--cov-fail-under" in all_runs, "CI must run the coverage-gated suite"

    # The CI coverage gate matches the Makefile's single source of truth (no drift).
    threshold = _coverage_threshold_from_makefile()
    assert f"--cov-fail-under={threshold}" in all_runs, (
        f"CI coverage gate must match Makefile COV_FAIL_UNDER={threshold}"
    )


# ---- Phase 26: the opt-in, non-gating sim-integration CI job ---------------

def test_ci_has_optin_nongating_sim_integration_job():
    """The llm-d-inference-sim integration job exists, is dispatch-only, and never blocks."""
    data = yaml.safe_load(CI_WORKFLOW.read_text())
    jobs = data["jobs"]
    assert "sim-integration" in jobs, "Phase 26 must add a sim-integration CI job"
    job = jobs["sim-integration"]

    # Non-gating: opt-in via manual dispatch + continue-on-error so it can't block the build.
    assert job.get("continue-on-error") is True, "sim-integration must never block the build"
    cond = str(job.get("if", ""))
    assert "workflow_dispatch" in cond and "run_sim_integration" in cond, (
        "sim-integration must only run on a manual dispatch with run_sim_integration"
    )

    # It actually runs the opt-in integration layer with the env flag set.
    steps = job.get("steps", [])
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "tests/integration/" in runs, "must run the tests/integration layer"
    envs = " ".join(
        f"{k}={v}" for s in steps for k, v in (s.get("env") or {}).items()
    )
    assert "LLMD_SIM_INTEGRATION" in envs, "must enable the opt-in integration flag"

    # The dispatch input that gates it is declared.
    inputs = data[True]["workflow_dispatch"]["inputs"]
    assert "run_sim_integration" in inputs


# ---- tomllib availability (the test's own dependency) ----------------------

def test_tomllib_is_available_on_the_pinned_python():
    # The project pins py>=3.11, where tomllib is stdlib — this test relies on it.
    assert sys.version_info >= (3, 11)
