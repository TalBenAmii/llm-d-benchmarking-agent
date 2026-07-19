"""Edge-case guards for the skill-usage detection helpers (_skill_index /
_operation_index / _run_passes) that both the live and scripted skill evals rely on.

Pure/synthetic tool-call lists — no engine, no LLM, instant.
"""
from __future__ import annotations

import pytest

from tests.eval.simulate.test_skill_usage_live import (
    SCENARIOS,
    _operation_index,
    _run_passes,
    _skill_index,
)

_BY_KEY = {s.key: s for s in SCENARIOS}


def _call(name, **inp):
    return {"name": name, "input": inp}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_fetch_all_does_not_count_as_grounding(scenario):
    """fetch_key_docs with no task (fetch-all) does NOT satisfy the SPECIFIC skill."""
    calls = [_call("fetch_key_docs"), _call("propose_session_plan")]
    assert _skill_index(calls, scenario) is None
    assert not _run_passes(calls, scenario)


def test_read_repo_doc_outside_skill_dir_not_detected():
    """read_repo_doc on an unrelated repo path is not skill grounding."""
    s = _BY_KEY["deploy_skill"]
    calls = [_call("read_repo_doc", path="llm-d-benchmark/docs/analysis.md")]
    assert _skill_index(calls, s) is None


def test_read_repo_doc_subfile_under_skill_dir_is_detected():
    """Any file under the skill dir (not just SKILL.md) counts as grounding."""
    s = _BY_KEY["deploy_skill"]
    calls = [_call("read_repo_doc", path=s.read_prefix + "references/troubleshooting.md")]
    assert _skill_index(calls, s) == 0


def test_skill_index_returns_first_occurrence():
    """When the skill is fetched twice, _skill_index returns the FIRST index."""
    s = _BY_KEY["benchmark_skill"]
    calls = [
        _call("probe_environment"),
        _call("fetch_key_docs", task="benchmark_skill"),
        _call("fetch_key_docs", task="benchmark_skill"),
    ]
    assert _skill_index(calls, s) == 1


def test_operation_index_returns_first_operation():
    """_operation_index points at the FIRST plan/execute call among several."""
    calls = [
        _call("probe_environment"),
        _call("propose_session_plan"),
        _call("execute_llmdbenchmark"),
    ]
    assert _operation_index(calls) == 1


def test_no_operation_returns_none():
    """A transcript with no plan/execute call has no operation index."""
    calls = [_call("probe_environment"), _call("fetch_key_docs", task="deploy_skill")]
    assert _operation_index(calls) is None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_skill_before_op_passes_after_op_fails(scenario):
    """skill before the operation passes; the same skill after the operation fails."""
    grounded = [_call("fetch_key_docs", task=scenario.key), _call("propose_session_plan")]
    assert _run_passes(grounded, scenario)
    late = [_call("propose_session_plan"), _call("fetch_key_docs", task=scenario.key)]
    assert not _run_passes(late, scenario)
