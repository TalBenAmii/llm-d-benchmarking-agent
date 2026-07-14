"""Guard for the committed WS event-stream baselines (tests/flows/baselines/).

The baselines pin the CURRENT agent loop's wire behavior for the engine-parity diff (see the
baselines README). This guard only keeps them from rotting silently: every baseline the capture
script declares must exist, parse, reference a real flow, and end with a ``done`` event — and no
orphan baseline file may linger after a corpus rename. The capture script itself is NOT run in
CI (re-capturing is a deliberate act); content equality is the parity phase's job, not this one.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from .flows import FLOWS_BY_NAME

_BASELINE_DIR = Path(__file__).parent / "baselines"
_CAPTURE_SCRIPT = (
    Path(__file__).parents[2] / "scripts" / "eval" / "capture_ws_baseline.py"
)


def _declared_baselines() -> dict:
    """The capture script's BASELINES spec — the single source of truth for what must exist."""
    spec = importlib.util.spec_from_file_location("capture_ws_baseline", _CAPTURE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BASELINES


def test_ws_baselines_exist_parse_and_end_done():
    declared = _declared_baselines()
    assert declared, "capture script declares no baselines"
    for name, spec in declared.items():
        path = _BASELINE_DIR / f"{name}.events.json"
        assert path.is_file(), (
            f"missing baseline {path.name} — recapture with scripts/eval/capture_ws_baseline.py"
        )
        doc = json.loads(path.read_text())
        assert doc["flow"] in FLOWS_BY_NAME, (
            f"{path.name}: flow {doc['flow']!r} no longer exists in the corpus"
        )
        assert doc["flow"] == spec["flow"]
        events = doc["events"]
        assert events, f"{path.name}: empty event stream"
        assert events[-1]["type"] == "done", f"{path.name}: stream does not end with `done`"


def test_no_orphan_baseline_files():
    declared = {f"{name}.events.json" for name in _declared_baselines()}
    on_disk = {p.name for p in _BASELINE_DIR.glob("*.events.json")}
    assert on_disk == declared, (
        f"baseline files out of sync with the capture script's BASELINES: "
        f"orphans={sorted(on_disk - declared)}, missing={sorted(declared - on_disk)}"
    )
