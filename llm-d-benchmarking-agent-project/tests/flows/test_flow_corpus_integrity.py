"""Flow-corpus integrity: every scoring reference in ALL_FLOWS must be a REAL name.

test_flows.py already guards that required/forbidden TOOLS are real registry names; this
closes the parallel hole for subcommands and specs — a typo like "standp" or "guides/typo"
would silently never match in the live eval, scoring the flow as trivially passed. Hermetic,
sibling-independent (uses _SUBCOMMANDS + the frozen catalog snapshot).
"""
from __future__ import annotations

from app.tools.run.execute import _SUBCOMMANDS
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.flows import ALL_FLOWS

_SPECS = frozenset(frozen_catalog()["specs"])


def test_all_flow_subcommands_are_real():
    """Every required/forbidden subcommand across the corpus is a real llmdbenchmark subcommand."""
    bad = [
        (f.name, s)
        for f in ALL_FLOWS
        for s in list(f.required_subcommands) + list(f.forbidden_subcommands)
        if s not in _SUBCOMMANDS
    ]
    assert not bad, f"flows referencing unknown subcommands: {bad} (valid: {sorted(_SUBCOMMANDS)})"


def test_all_flow_required_specs_are_real():
    """Every flow.required_spec is a real spec in the frozen catalog."""
    bad = [
        (f.name, f.required_spec)
        for f in ALL_FLOWS
        if f.required_spec and f.required_spec not in _SPECS
    ]
    assert not bad, f"flows referencing unknown specs: {bad}"
