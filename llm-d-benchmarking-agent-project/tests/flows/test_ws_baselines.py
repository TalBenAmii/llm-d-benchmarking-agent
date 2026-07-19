"""Guard for the committed WS event-stream baselines (tests/flows/baselines/).

Two committed sets pin the wire behavior of BOTH engines over the same six scripted
conversations: ``<name>.events.json`` (the old agent loop, Phase 0c) and
``<name>.sdk-native.events.json`` (the SDK-native engine, Phase 4). The structural checks keep
them from rotting silently (exist, parse, real flow, end with ``done``, no orphans); the parity
test is the over-the-wire engine diff itself — the two streams must be BYTE-IDENTICAL modulo
the one adjudicated difference (usage cadence). The capture script is NOT run in CI
(re-capturing is a deliberate act).
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
# One committed file set per engine: "" = the old loop, ".sdk-native" = the new engine.
_ENGINE_SUFFIXES = ("", ".sdk-native")


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
        for suffix in _ENGINE_SUFFIXES:
            path = _BASELINE_DIR / f"{name}{suffix}.events.json"
            assert path.is_file(), (
                f"missing baseline {path.name} — recapture with "
                f"scripts/eval/capture_ws_baseline.py"
                + (" --engine sdk-native" if suffix else "")
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
    declared = {f"{name}{suffix}.events.json"
                for name in _declared_baselines() for suffix in _ENGINE_SUFFIXES}
    on_disk = {p.name for p in _BASELINE_DIR.glob("*.events.json")}
    assert on_disk == declared, (
        f"baseline files out of sync with the capture script's BASELINES: "
        f"orphans={sorted(on_disk - declared)}, missing={sorted(declared - on_disk)}"
    )


def test_sdk_native_wire_stream_matches_old_engine_modulo_usage_cadence():
    """The over-the-wire engine parity diff: for every baseline conversation, the SDK-native
    stream equals the old loop's PINNED stream event-for-event — texts, tool calls/results,
    command argv, approval cards, cards, done — after dropping ``usage`` events from both
    sides. Usage cadence is the ONE adjudicated engine difference: the old loop emits one
    usage per LLM call (mid-turn), the new engine one per SDK response (post-result). Both
    files are already normalized by the capture (ids -> <tc-N>/<approval-N>, paths -> tokens,
    seq/durations/token-numbers stripped), so equality here is exact, not fuzzy. NO other
    difference is normalized — a new divergence must fail this test and be adjudicated."""
    for name in _declared_baselines():
        streams = []
        for suffix in _ENGINE_SUFFIXES:
            doc = json.loads((_BASELINE_DIR / f"{name}{suffix}.events.json").read_text())
            streams.append([e for e in doc["events"] if e["type"] != "usage"])
        old, new = streams
        assert new == old, (
            f"{name}: sdk-native wire stream diverged from the pinned old-engine baseline "
            "(compare the two .events.json files; adjudicate before normalizing anything)"
        )
