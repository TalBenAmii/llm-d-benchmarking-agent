"""Phase 41 — dataset replay URL (-x/--dataset).

Hermetic, no cluster / GPU / network. Covers the MECHANISM this phase adds (the
dataset-vs-synthetic JUDGMENT lives in knowledge/dataset_replay.md, not in Python):

  * build_argv emits the SUBCOMMAND-AWARE dataset flag: a set ``flags["dataset"]`` => ``-x <url>``
    on run/experiment ONLY (standup/plan/smoketest/teardown reject it upstream); absent/None/empty
    => nothing (the synthetic workload profile still drives the load);
  * the allowlist permits ``-x``/``--dataset`` (value-constrained to a dataset URL/path) on run and
    experiment, and the flag does NOT change the command's mutating classification, while the
    metachar screen still rejects an injection-laden dataset value;
  * the ExecuteInput schema accepts ``dataset`` inside ``flags``;
  * the knowledge guide + tool descriptions point the agent at the judgment.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.execute import build_argv
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

# A real trace URL from the upstream README's `-x DATASET` example family.
DATASET_URL = (
    "https://github.com/alibaba-edu/qwen-bailian-usagetraces-anon/"
    "raw/refs/heads/main/qwen_traceA_blksz_16.jsonl"
)

# ---------------------------------------------------------------------------
# build_argv — subcommand-aware dataset emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", ["run", "experiment"])
def test_dataset_emits_x_on_run_and_experiment(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"dataset": DATASET_URL})
    assert "-x" in argv
    # -x is immediately followed by the exact URL the agent chose (verbatim, no mutation).
    assert argv[argv.index("-x") + 1] == DATASET_URL


@pytest.mark.parametrize("subcommand", ["standup", "plan", "smoketest", "teardown"])
def test_dataset_omitted_on_non_run_experiment(subcommand):
    # Upstream -x/--dataset is accepted ONLY on run/experiment; we never emit it elsewhere,
    # even if the agent mistakenly set it — the synthetic profile (or no-op) stands.
    argv = build_argv(subcommand, spec="cicd/kind", flags={"dataset": DATASET_URL})
    assert "-x" not in argv
    assert DATASET_URL not in argv


@pytest.mark.parametrize("subcommand", ["run", "experiment", "standup", "plan"])
def test_dataset_unset_emits_nothing(subcommand):
    # No dataset key (or None/empty) => synthetic profiles still drive; never inject -x.
    for flags in ({}, {"dataset": None}, {"dataset": ""}):
        argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
        assert "-x" not in argv


def test_dataset_does_not_disturb_other_flags():
    argv = build_argv(
        "run", spec="cicd/kind", harness="vllm-benchmark", workload="sanity_random.yaml",
        flags={"dataset": DATASET_URL, "output": "local", "monitoring": True},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    # the dataset rides ALONGSIDE the existing flags, not in place of them
    assert argv[argv.index("-x") + 1] == DATASET_URL
    assert "-r" in argv and "local" in argv
    assert "--monitoring" in argv
    assert "-l" in argv and "vllm-benchmark" in argv


def test_execute_schema_accepts_dataset_flag():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"dataset": DATASET_URL})
    assert m.flags == {"dataset": DATASET_URL}


# ---------------------------------------------------------------------------
# allowlist — -x/--dataset permitted (value-constrained) on run + experiment (DATA)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["-x", "--dataset"])
def test_allowlist_permits_dataset_on_run(allowlist, catalog, flag):
    d = allowlist.validate(
        _argv("run", "-l", "vllm-benchmark", "-w", "sanity_random.yaml", flag, DATASET_URL),
        catalog=catalog,
    )
    assert d.allowed, f"{flag} should be allowed on run: {d.reason}"
    assert d.mode == MUTATING  # a real run stays mutating (approval-gated)


@pytest.mark.parametrize("flag", ["-x", "--dataset"])
def test_allowlist_permits_dataset_on_experiment(allowlist, catalog, flag):
    d = allowlist.validate(_argv("experiment", "-e", "exp.yaml", flag, DATASET_URL), catalog=catalog)
    assert d.allowed, f"{flag} should be allowed on experiment: {d.reason}"
    assert d.mode == MUTATING


def test_allowlist_dataset_value_constraint_accepts_schemes(allowlist, catalog):
    for url in (
        DATASET_URL,
        "hf://datasets/anon/shared-prefix",
        "gs://my-bucket/traces/run1.jsonl",
        "s3://my-bucket/traces/run1.jsonl",
        "workspace/datasets/local_trace.jsonl",
        "https://example.com/data/",  # trailing slash => directory replay
    ):
        d = allowlist.validate(
            _argv("run", "-l", "vllm-benchmark", "-w", "sanity_random.yaml", "-x", url),
            catalog=catalog,
        )
        assert d.allowed, f"dataset url {url!r} should pass the value constraint: {d.reason}"


def test_allowlist_rejects_injection_laden_dataset_value(allowlist, catalog):
    # A metachar-laden dataset value is rejected by the blanket screen (defense in depth),
    # even though the constraint regex would also reject it.
    d = allowlist.validate(
        _argv("run", "-l", "vllm-benchmark", "-w", "sanity_random.yaml",
              "-x", "https://evil/$(rm -rf /)"),
        catalog=catalog,
    )
    assert not d.allowed


def test_dataset_flag_keeps_read_only_preview(allowlist, catalog):
    # --dry-run still downgrades a dataset-bearing run to a read-only preview (the dataset flag
    # is orthogonal to the mode classification).
    d = allowlist.validate(
        _argv("run", "-l", "vllm-benchmark", "-w", "sanity_random.yaml",
              "-x", DATASET_URL, "--dry-run"),
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_dataset_replay_knowledge_is_discoverable():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent / "knowledge" / "dataset_replay.md"
    assert kfile.is_file(), "knowledge/dataset_replay.md must exist (auto-indexed by prompt glob)"
    text = kfile.read_text()
    # It must actually carry the dataset-vs-synthetic judgment, not just mention the flag.
    assert "synthetic" in text.lower()
    assert "-x" in text


def test_execute_tool_description_points_at_dataset_knowledge():
    from app.tools.registry import _DESCRIPTIONS

    desc = _DESCRIPTIONS["execute_llmdbenchmark"]
    assert "dataset" in desc.lower()
    assert "dataset_replay" in desc
