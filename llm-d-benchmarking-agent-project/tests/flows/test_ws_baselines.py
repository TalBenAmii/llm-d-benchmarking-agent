"""Guard for the committed WS event-stream baselines (tests/flows/baselines/).

Two committed sets pin the wire behavior over the same six scripted conversations:
``<name>.events.json`` — the RETIRED pre-cutover agent loop's streams (Phase 0c), kept as
FROZEN pins that are never regenerated — and ``<name>.sdk-native.events.json`` — the engine's
streams, what ``scripts/eval/capture_ws_baseline.py`` regenerates. The structural checks keep
them from rotting silently (exist, parse, real flow, end with ``done``, no orphans); the parity
test diffs the engine's streams against the frozen pins — they must be BYTE-IDENTICAL modulo
the one adjudicated cutover difference (usage cadence). The capture script is NOT run in CI
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
# "" = the retired pre-cutover loop's frozen pins; ".sdk-native" = the engine's streams.
_ENGINE_SUFFIXES = ("", ".sdk-native")


def _load_capture_module():
    spec = importlib.util.spec_from_file_location("capture_ws_baseline", _CAPTURE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Loaded ONCE at module scope: the BASELINES spec is the single source of truth for what must
# exist, and the live-recapture test reuses the module's own capture_flow/normalization.
_CAPTURE = _load_capture_module()


def _declared_baselines() -> dict:
    return _CAPTURE.BASELINES


def test_ws_baselines_exist_parse_and_end_done():
    declared = _declared_baselines()
    assert declared, "capture script declares no baselines"
    for name, spec in declared.items():
        for suffix in _ENGINE_SUFFIXES:
            path = _BASELINE_DIR / f"{name}{suffix}.events.json"
            assert path.is_file(), (
                f"missing baseline {path.name} — "
                + ("recapture with scripts/eval/capture_ws_baseline.py" if suffix
                   else "the frozen pre-cutover pins are committed, never regenerated; restore "
                        "the file from git history")
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
    """The over-the-wire cutover parity diff: for every baseline conversation, the engine's
    stream equals the retired loop's FROZEN pin event-for-event — texts, tool calls/results,
    command argv, approval cards, cards, done — after dropping ``usage`` events from both
    sides. Usage cadence was the ONE adjudicated cutover difference: the old loop emitted one
    usage per LLM call (mid-turn), the engine one per SDK response (post-result). Both
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


def test_live_recapture_matches_committed_sdk_native_pin():
    """The engine's CURRENT wire stream still matches its committed pin — captured live, here,
    over the hermetic path (FakeTransport + in-process WS, zero quota). After cutover the
    frozen-vs-frozen parity test above can't catch an engine regression on its own; this one
    can, for a representative flow. One flow keeps the test fast; a full recapture stays the
    documented manual act (scripts/eval/capture_ws_baseline.py — see baselines/README.md).

    dry-run-preview is the chosen pin: read-only (no approval gates, no skill-gate surface, so
    pytest's autouse grounding fixture cannot perturb it) and the fastest of the six."""
    from tests.flows.flows import FLOWS_BY_NAME as flows

    captured = _CAPTURE.capture_flow(flows["dry-run-preview"])
    committed = json.loads(
        (_BASELINE_DIR / "dry-run-preview.sdk-native.events.json").read_text())["events"]
    assert captured == committed, (
        "the engine's live-captured dry-run-preview stream diverged from its committed "
        ".sdk-native pin — if the change is intended, recapture with "
        "scripts/eval/capture_ws_baseline.py and adjudicate the diff"
    )
