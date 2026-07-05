"""CaptureRunner direct unit test — its two dark contracts (timed_out + needle ordering).

The hermetic flow harness drives every command through CaptureRunner, which records argv
and replays canned output without a real subprocess. Its happy path is covered end-to-end,
but the timed_out canned path and the first-needle-wins ordering are exercised nowhere.
Sibling-independent (CaptureRunner never resolves real repo paths).
"""
from __future__ import annotations

from tests.flows.harness import CannedResult, CaptureRunner


async def test_canned_timed_out_and_exit_code_propagate():
    """A CannedResult(timed_out=True) surfaces on the RunResult with its exit code."""
    runner = CaptureRunner({}, canned={"run": CannedResult(timed_out=True, exit_code=124)})
    res = await runner.execute(["llmdbenchmark", "run"], None)
    assert res.timed_out is True
    assert res.exit_code == 124


async def test_canned_str_is_stdout_with_exit_zero():
    """A plain-str canned value becomes stdout with a clean exit."""
    runner = CaptureRunner({}, canned={"results": "OK-OUTPUT"})
    res = await runner.execute(["llmdbenchmark", "results"], None)
    assert res.output == "OK-OUTPUT"
    assert res.exit_code == 0
    assert res.timed_out is False


async def test_first_matching_needle_wins():
    """When two needles both match, the FIRST in insertion order wins."""
    runner = CaptureRunner({}, canned={"standup": "SPECIFIC", "llmdbenchmark": "GENERIC"})
    res = await runner.execute(["llmdbenchmark", "standup"], None)
    assert res.output == "SPECIFIC"


async def test_no_matching_needle_is_empty_success():
    """No canned needle matches -> empty output, clean exit."""
    runner = CaptureRunner({}, canned={"nomatch": "X"})
    res = await runner.execute(["llmdbenchmark", "results"], None)
    assert res.output == ""
    assert res.exit_code == 0


async def test_calls_are_recorded(tmp_path):
    """Every invocation is recorded with its argv and cwd."""
    runner = CaptureRunner({})
    await runner.execute(["llmdbenchmark", "run"], None, cwd=str(tmp_path))
    assert runner.calls[-1]["argv"] == ["llmdbenchmark", "run"]
    assert runner.calls[-1]["cwd"] == str(tmp_path)


async def test_clone_skeleton_skipped_for_non_llmd_repo(tmp_path):
    """git clone of a non-llm-d repo materializes no skeleton."""
    runner = CaptureRunner({})
    await runner.execute(["git", "clone", "https://example.com/other-repo"], None, cwd=str(tmp_path))
    assert not (tmp_path / "other-repo").exists()
