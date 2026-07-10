"""Phase 33 — multi-stack scenarios: --stack subset + --parallel cap.

Hermetic, no cluster / GPU / network. Covers the MECHANISM this phase adds (the
WHICH-stack / HOW-MANY-at-once JUDGMENT lives in knowledge/multi_stack.md, not in Python):

  * build_argv emits the SUBCOMMAND-AWARE multi-stack flags:
      - a set ``flags["stack"]``  => ``--stack <names>``  on standup/smoketest/run/teardown ONLY
        (plan/experiment reject it upstream); absent/None/empty => nothing (every stack operated on);
      - ``flags["parallel"]``     => ``--parallel <int>``  on standup/smoketest/experiment ONLY
        (run uses the SEPARATE --parallelism/-j harness-pod count; teardown/plan have neither);
        an explicit 0 IS honored (``is not None`` guard);
  * --stack and --parallel are DISTINCT from --parallelism/-j (parallel harness PODS) — the
    pre-existing -j emission is NOT regressed and the two never collide;
  * the allowlist permits --stack (value-constrained to a stack_list) on standup/smoketest/run/
    teardown and --parallel (positive_int) on standup/smoketest (experiment already had it), the
    flags do NOT change the mutating classification, and the metachar screen rejects injection;
  * the ExecuteInput schema accepts ``stack``/``parallel`` inside ``flags``;
  * the knowledge guide + tool descriptions point the agent at the judgment.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.execute import build_argv
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

# Real stack names from the upstream multi-model-wva scenario (config/scenarios/examples).
STACK_ONE = "qwen3-06b"
STACK_TWO = "llama-31-8b"
SUBSET = f"{STACK_ONE},{STACK_TWO}"

# Upstream acceptance, verified against llm-d-benchmark/llmdbenchmark/interface/*.py:
STACK_SUBCOMMANDS = ["standup", "smoketest", "run", "teardown"]
STACK_REJECTED = ["plan", "experiment"]
PARALLEL_SUBCOMMANDS = ["standup", "smoketest", "experiment"]
PARALLEL_REJECTED = ["run", "teardown", "plan"]


# ---------------------------------------------------------------------------
# build_argv — --stack subset emission (PURE MECHANISM, subcommand-aware)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", STACK_SUBCOMMANDS)
def test_stack_emits_on_supported_subcommands(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"stack": STACK_ONE})
    assert "--stack" in argv
    # --stack is immediately followed by the exact names the agent chose (verbatim, no mutation).
    assert argv[argv.index("--stack") + 1] == STACK_ONE


@pytest.mark.parametrize("subcommand", STACK_SUBCOMMANDS)
def test_stack_emits_comma_separated_subset_verbatim(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"stack": SUBSET})
    assert argv[argv.index("--stack") + 1] == SUBSET


@pytest.mark.parametrize("subcommand", STACK_REJECTED)
def test_stack_omitted_on_unsupported_subcommands(subcommand):
    # Upstream --stack exists ONLY on standup/smoketest/run/teardown; never emit it elsewhere,
    # even if the agent mistakenly set it (every stack of the scenario stands).
    argv = build_argv(subcommand, spec="cicd/kind", flags={"stack": STACK_ONE})
    assert "--stack" not in argv
    assert STACK_ONE not in argv


@pytest.mark.parametrize("subcommand", STACK_SUBCOMMANDS + STACK_REJECTED)
def test_stack_unset_emits_nothing(subcommand):
    # No stack key (or None/empty) => operate on every stack; never inject --stack.
    for flags in ({}, {"stack": None}, {"stack": ""}):
        argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
        assert "--stack" not in argv


# ---------------------------------------------------------------------------
# build_argv — --parallel cap emission (PURE MECHANISM, subcommand-aware)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", PARALLEL_SUBCOMMANDS)
def test_parallel_emits_on_supported_subcommands(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"parallel": 1})
    assert "--parallel" in argv
    assert argv[argv.index("--parallel") + 1] == "1"


@pytest.mark.parametrize("subcommand", PARALLEL_REJECTED)
def test_parallel_omitted_on_unsupported_subcommands(subcommand):
    # Upstream --parallel is on standup/smoketest/experiment ONLY; run uses --parallelism/-j.
    argv = build_argv(subcommand, spec="cicd/kind", flags={"parallel": 2})
    assert "--parallel" not in argv


@pytest.mark.parametrize("subcommand", PARALLEL_SUBCOMMANDS)
def test_parallel_explicit_zero_is_honored(subcommand):
    # `is not None` guard => an explicit 0 is emitted (mirrors the parallelism/-j guard), not
    # silently dropped as a falsy value would be under a bare truthiness check.
    argv = build_argv(subcommand, spec="cicd/kind", flags={"parallel": 0})
    assert "--parallel" in argv
    assert argv[argv.index("--parallel") + 1] == "0"


@pytest.mark.parametrize("subcommand", PARALLEL_SUBCOMMANDS + PARALLEL_REJECTED)
def test_parallel_unset_emits_nothing(subcommand):
    for flags in ({}, {"parallel": None}):
        argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
        assert "--parallel" not in argv


# ---------------------------------------------------------------------------
# --parallel (stacks) is DISTINCT from --parallelism/-j (harness pods) — no regression
# ---------------------------------------------------------------------------


def test_parallel_and_parallelism_are_distinct_flags_on_experiment():
    # experiment is the one subcommand that takes BOTH; they must both appear, distinctly, with
    # their own values — proving the pre-existing -j emission is NOT regressed and not conflated.
    argv = build_argv("experiment", spec="cicd/kind", flags={"parallel": 2, "parallelism": 8})
    assert "--parallel" in argv and argv[argv.index("--parallel") + 1] == "2"
    assert "-j" in argv and argv[argv.index("-j") + 1] == "8"


def test_run_keeps_parallelism_j_and_never_gets_parallel():
    # On a run the agent's harness-pod count (-j) is untouched, and --parallel (a STACK knob,
    # not a run flag upstream) is never emitted even if mistakenly set.
    argv = build_argv("run", spec="cicd/kind", flags={"parallelism": 4, "parallel": 2})
    assert "-j" in argv and argv[argv.index("-j") + 1] == "4"
    assert "--parallel" not in argv


def test_stack_and_parallel_ride_alongside_other_flags():
    argv = build_argv(
        "standup", spec="examples/gpu", namespace="llmdbench",
        flags={"stack": STACK_ONE, "parallel": 1, "monitoring": True},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "examples/gpu"]
    assert argv[argv.index("--stack") + 1] == STACK_ONE
    assert argv[argv.index("--parallel") + 1] == "1"
    assert "--monitoring" in argv
    assert "-p" in argv and "llmdbench" in argv


def test_execute_schema_accepts_stack_and_parallel_flags():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"stack": SUBSET, "parallel": 1})
    assert m.flags == {"stack": SUBSET, "parallel": 1}


# ---------------------------------------------------------------------------
# allowlist — --stack / --parallel permitted, value-constrained (DATA)
# ---------------------------------------------------------------------------


def _run_args(subcommand):
    # Minimal valid positionals/flags so a subcommand validates before we probe --stack/--parallel.
    if subcommand == "run":
        return ["-l", "vllm-benchmark", "-w", "sanity_random.yaml"]
    if subcommand == "experiment":
        return ["-e", "exp.yaml"]
    return []


@pytest.mark.parametrize("subcommand", STACK_SUBCOMMANDS)
def test_allowlist_permits_stack_on_supported_subcommands(allowlist, catalog, subcommand):
    d = allowlist.validate(
        _argv(subcommand, *_run_args(subcommand), "--stack", SUBSET), catalog=catalog
    )
    assert d.allowed, f"--stack should be allowed on {subcommand}: {d.reason}"
    assert d.mode == MUTATING  # still a real cluster mutation (approval-gated)


@pytest.mark.parametrize("subcommand", ["standup", "smoketest"])
def test_allowlist_permits_parallel_on_standup_smoketest(allowlist, catalog, subcommand):
    d = allowlist.validate(_argv(subcommand, "--parallel", "1"), catalog=catalog)
    assert d.allowed, f"--parallel should be allowed on {subcommand}: {d.reason}"
    assert d.mode == MUTATING


def test_allowlist_still_permits_parallel_on_experiment(allowlist, catalog):
    # experiment already carried --parallel pre-Phase-33; assert we did not regress it.
    d = allowlist.validate(_argv("experiment", "-e", "exp.yaml", "--parallel", "2"), catalog=catalog)
    assert d.allowed, f"--parallel must remain allowed on experiment: {d.reason}"
    assert d.mode == MUTATING


def test_allowlist_stack_value_constraint_accepts_one_and_many(allowlist, catalog):
    for names in (STACK_ONE, SUBSET, "a,b,c-d,e0"):
        d = allowlist.validate(
            _argv("run", "-l", "vllm-benchmark", "-w", "sanity_random.yaml", "--stack", names),
            catalog=catalog,
        )
        assert d.allowed, f"stack list {names!r} should pass the value constraint: {d.reason}"


def test_allowlist_rejects_injection_laden_stack_value(allowlist, catalog):
    # A metachar-laden stack value is rejected by the blanket screen (defense in depth);
    # the stack_list regex would also reject it.
    d = allowlist.validate(
        _argv("standup", "--stack", "qwen3-06b;rm -rf /"), catalog=catalog
    )
    assert not d.allowed


def test_allowlist_rejects_non_int_parallel_value(allowlist, catalog):
    # --parallel is value-pinned to positive_int; a non-int is refused.
    d = allowlist.validate(_argv("standup", "--parallel", "lots"), catalog=catalog)
    assert not d.allowed


def test_stack_and_parallel_keep_read_only_preview(allowlist, catalog):
    # --dry-run still downgrades a stack/parallel-bearing standup to a read-only preview
    # (these flags are orthogonal to the mode classification).
    d = allowlist.validate(
        _argv("standup", "--stack", STACK_ONE, "--parallel", "1", "--dry-run"), catalog=catalog
    )
    assert d.allowed and d.mode == READ_ONLY


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_multi_stack_knowledge_is_discoverable():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent / "knowledge" / "deploy/multi_stack.md"
    assert kfile.is_file(), "knowledge/multi_stack.md must exist (auto-indexed by prompt glob)"
    text = kfile.read_text()
    # First line must be a heading (the on-demand index uses it as the one-line purpose).
    assert text.splitlines()[0].startswith("#")
    # It must carry the WHICH-stack + WHEN-to-cap judgment, not just name the flags.
    lower = text.lower()
    assert "--stack" in text and "--parallel" in text
    assert "subset" in lower
    assert "multi-model-wva" in lower  # the canonical multi-stack example


def test_execute_tool_description_points_at_multi_stack_knowledge():
    from app.tools.registry import _DESCRIPTIONS

    desc = _DESCRIPTIONS["execute_llmdbenchmark"]
    assert "multi_stack" in desc
    assert "--stack" in desc
