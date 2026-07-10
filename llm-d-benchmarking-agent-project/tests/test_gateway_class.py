"""Phase 32 — gateway class / provider selection (--gateway-class).

Hermetic, no cluster / GPU / network. Covers the MECHANISM this phase adds (the
WHICH-provider JUDGMENT lives in knowledge/gateway_class.md, not in Python):

  * build_argv emits ``--gateway-class <provider>`` from ``flags["gateway_class"]``
    UNCONDITIONALLY across ALL SIX subcommands (plan/standup/smoketest/run/teardown/
    experiment) — upstream registers the flag on every one of them, so there is no
    subcommand guard and no if/elif on the value; absent/None/empty => nothing emitted
    (the spec's scenario gateway.className stands);
  * the allowlist permits ``--gateway-class`` (value-constrained to the gateway_class
    enum: epponly/istio/agentgateway/gke/data-science-gateway-class) on each of the six
    subcommands, the flag does NOT change a command's mode, and an out-of-enum /
    injection-laden value is refused;
  * the ExecuteInput schema accepts ``gateway_class`` inside ``flags``;
  * the knowledge guide + tool descriptions point the agent at the judgment.

Upstream acceptance + enum verified against
llm-d-benchmark/llmdbenchmark/interface/{plan,standup,smoketest,run,teardown,experiment}.py
and docs/standup.md.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.run.execute import build_argv
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

# The closed provider set the CLI advertises on every subcommand.
PROVIDERS = ["epponly", "istio", "agentgateway", "gke", "data-science-gateway-class"]

# build_argv accepts a free-form subcommand string; --gateway-class is upstream-valid on all six.
GATEWAY_SUBCOMMANDS = ["plan", "standup", "smoketest", "run", "teardown", "experiment"]

# Subcommands whose ALLOWLIST entry carries the --gateway-class flagspec + their base mode.
# `results` is read-only and does NOT take --gateway-class upstream, so it is excluded.
ALLOWLIST_SUBCOMMAND_MODE = {
    "plan": READ_ONLY,
    "standup": MUTATING,
    "smoketest": MUTATING,
    "run": MUTATING,
    "teardown": MUTATING,
    "experiment": MUTATING,
}


# ---------------------------------------------------------------------------
# build_argv — --gateway-class emission (PURE MECHANISM, all subcommands)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", GATEWAY_SUBCOMMANDS)
@pytest.mark.parametrize("provider", PROVIDERS)
def test_gateway_class_emits_on_every_subcommand(subcommand, provider):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"gateway_class": provider})
    assert "--gateway-class" in argv
    # --gateway-class is immediately followed by the exact provider (verbatim, no mutation).
    assert argv[argv.index("--gateway-class") + 1] == provider


@pytest.mark.parametrize("subcommand", GATEWAY_SUBCOMMANDS)
def test_gateway_class_unset_emits_nothing(subcommand):
    # No gateway_class key (or None/empty) => the spec's gateway.className stands; never inject it.
    for flags in ({}, {"gateway_class": None}, {"gateway_class": ""}):
        argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
        assert "--gateway-class" not in argv


def test_gateway_class_rides_alongside_other_flags():
    argv = build_argv(
        "standup", spec="guides/optimized-baseline", namespace="llmdbench",
        models="facebook/opt-125m",
        flags={"gateway_class": "istio", "monitoring": True, "stack": "qwen3-06b"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "guides/optimized-baseline"]
    assert argv[argv.index("--gateway-class") + 1] == "istio"
    # composes with — does not displace — the other modeled flags.
    assert "--monitoring" in argv
    assert "-m" in argv and "facebook/opt-125m" in argv
    assert argv[argv.index("--stack") + 1] == "qwen3-06b"
    assert "-p" in argv and "llmdbench" in argv


def test_gateway_class_emitted_exactly_once():
    argv = build_argv("standup", spec="cicd/kind", flags={"gateway_class": "agentgateway"})
    assert argv.count("--gateway-class") == 1
    assert argv.count("agentgateway") == 1


def test_execute_schema_accepts_gateway_class_flag():
    m = ExecuteInput(subcommand="standup", spec="cicd/kind", flags={"gateway_class": "agentgateway"})
    assert m.flags == {"gateway_class": "agentgateway"}


# ---------------------------------------------------------------------------
# allowlist — --gateway-class permitted, value-constrained (DATA)
# ---------------------------------------------------------------------------


def _run_args(subcommand):
    # Minimal valid positionals/flags so a subcommand validates before we probe --gateway-class.
    if subcommand == "run":
        return ["-l", "vllm-benchmark", "-w", "sanity_random.yaml"]
    if subcommand == "experiment":
        return ["-e", "exp.yaml"]
    return []


@pytest.mark.parametrize("subcommand", list(ALLOWLIST_SUBCOMMAND_MODE))
@pytest.mark.parametrize("provider", PROVIDERS)
def test_allowlist_permits_gateway_class_on_each_subcommand(
    allowlist, catalog, subcommand, provider
):
    d = allowlist.validate(
        _argv(subcommand, *_run_args(subcommand), "--gateway-class", provider), catalog=catalog
    )
    assert d.allowed, f"--gateway-class {provider} should be allowed on {subcommand}: {d.reason}"
    # The flag does NOT change the base mode of the subcommand.
    assert d.mode == ALLOWLIST_SUBCOMMAND_MODE[subcommand]


def test_allowlist_rejects_out_of_enum_gateway_class(allowlist, catalog):
    # A plausible-but-wrong provider (a typo) is refused by the value enum.
    d = allowlist.validate(_argv("standup", "--gateway-class", "isto"), catalog=catalog)
    assert not d.allowed


def test_allowlist_rejects_injection_laden_gateway_class(allowlist, catalog):
    # A metachar-laden value is rejected by the blanket screen (defense in depth); the enum
    # would also reject it.
    d = allowlist.validate(
        _argv("standup", "--gateway-class", "istio;rm -rf /"), catalog=catalog
    )
    assert not d.allowed


def test_gateway_class_keeps_read_only_preview(allowlist, catalog):
    # --dry-run still downgrades a gateway-class-bearing standup to a read-only PREVIEW
    # (the flag is orthogonal to the mode classification).
    d = allowlist.validate(
        _argv("standup", "--gateway-class", "agentgateway", "--dry-run"), catalog=catalog
    )
    assert d.allowed and d.mode == READ_ONLY


def test_gateway_class_on_plan_stays_read_only(allowlist, catalog):
    # plan is read-only; previewing a provider override must not flip it to mutating.
    d = allowlist.validate(_argv("plan", "--gateway-class", "epponly"), catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_gateway_class_knowledge_is_discoverable():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent / "knowledge" / "deploy/gateway_class.md"
    assert kfile.is_file(), "knowledge/gateway_class.md must exist (auto-indexed by prompt glob)"
    text = kfile.read_text()
    # First line must be a heading (the on-demand index uses it as the one-line purpose).
    assert text.splitlines()[0].startswith("#")
    lower = text.lower()
    # It must carry the WHICH-provider judgment, naming every provider in the enum.
    for provider in PROVIDERS:
        assert provider in lower, f"knowledge must document the {provider!r} provider"
    assert "--gateway-class" in text
    # It must explain that the override only matters on the modelservice deploy path.
    assert "modelservice" in lower


def test_gateway_class_knowledge_reachable_via_read_knowledge(tool_ctx):
    from app.tools.access.knowledge_access import read_knowledge

    res = read_knowledge(tool_ctx, name="gateway_class")
    assert "error" not in res, res
    assert res["topic"] == "gateway_class"
    assert "--gateway-class" in res["content"]


def test_execute_tool_description_points_at_gateway_class_knowledge():
    from app.tools.registry import _DESCRIPTIONS

    desc = _DESCRIPTIONS["execute_llmdbenchmark"]
    assert "gateway_class" in desc
    assert "--gateway-class" in desc


def test_execute_schema_description_documents_gateway_class():
    from app.tools.schemas import ExecuteInput

    desc = ExecuteInput.model_fields["flags"].description or ""
    assert "gateway_class" in desc
    assert "--gateway-class" in desc
