"""Tests for app.dig.find_last_json.

Covers the contract (last balanced JSON value off a noisy stream) and pins the raw_decode
rewrite to be result-identical to the original naive backward-scan via an oracle.
"""
import json

import pytest

from app.dig import find_last_json, parse_bridge_dict


def _naive_find_last_json(text, opener):
    """The original O(braces × length) implementation, kept here as an equivalence oracle."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.rfind(opener)
    while start != -1:
        try:
            return json.loads(text[start:])
        except ValueError:
            start = text.rfind(opener, 0, start)
    return None


def test_clean_object():
    assert find_last_json('{"a": 1, "b": 2}', "{") == {"a": 1, "b": 2}


def test_leading_log_noise_before_object():
    text = "INFO bootstrapping\nWARN slow disk\n{\"ok\": true, \"n\": 3}"
    assert find_last_json(text, "{") == {"ok": True, "n": 3}


def test_array_opener():
    text = "loading components...\n[{\"kind\": \"Deployment\"}, {\"kind\": \"Service\"}]"
    assert find_last_json(text, "[") == [{"kind": "Deployment"}, {"kind": "Service"}]


def test_trailing_whitespace_after_value():
    assert find_last_json('  {"x": 1}  \n\n', "{") == {"x": 1}


def test_returns_last_value_when_several_present():
    # Two objects on the stream — the contract is the LAST balanced value.
    assert find_last_json('{"first": 1}\n{"second": 2}', "{") == {"second": 2}


def test_nested_braces_returns_outer_object():
    # Noise then a deeply nested object: inner braces are nearer the end but their candidates
    # leave trailing text, so the scan must walk back to the outermost opener.
    text = 'log line\n{"outer": {"inner": {"deep": [1, 2, 3]}}}'
    assert find_last_json(text, "{") == {"outer": {"inner": {"deep": [1, 2, 3]}}}


def test_no_json_returns_none():
    assert find_last_json("just logs, no json here", "{") is None


def test_empty_and_blank_return_none():
    assert find_last_json("", "{") is None
    assert find_last_json("   \n  ", "{") is None
    assert find_last_json(None, "{") is None


def test_trailing_partial_disqualifies_the_whole_stream():
    # Contract: the value must extend to the END of the stream. A complete object followed by a
    # dangling partial leaves no opener-to-end substring that parses, so the result is None —
    # identical to the original implementation (verified by the oracle test below).
    text = '{"complete": true}\n{"partial":'
    assert find_last_json(text, "{") is None
    assert find_last_json(text, "{") == _naive_find_last_json(text, "{")


@pytest.mark.parametrize(
    "text,opener",
    [
        ('{"a": 1}', "{"),
        ('noise {"a": {"b": 2}}', "{"),
        ('x\ny\n[1, 2, [3, 4]]', "["),
        ('{"first": 1} trailing {"second": 2}', "{"),
        ('{"dangling":', "{"),
        ('no json', "{"),
        ('', "{"),
        ('{"unicode": "café ☕"}', "{"),
        ('logs {"a":1}\nmore logs\n{"b":2}', "{"),
        ('[{"k": 1}] then [{"k": 2}]', "["),
    ],
)
def test_matches_naive_oracle(text, opener):
    assert find_last_json(text, opener) == _naive_find_last_json(text, opener)


def test_large_nested_blob_after_noise_is_handled():
    # A big object with many nested braces preceded by log noise — the case the rewrite
    # protects against. The per-test timeout (pyproject) doubles as a no-hang guard.
    payload = {"runs": [{"id": i, "meta": {"k": {"deep": i}}} for i in range(2_000)]}
    text = "INFO starting\nWARN retry\n" + json.dumps(payload)
    assert find_last_json(text, "{") == payload


# ---- parse_bridge_dict: the shared bridge-stdout wrapper ----------------------
# This is the shared helper app/tools/setup/capacity.py and app/tools/analyze/aggregate_runs.py both
# call directly. It locks the unified empty/no-json/malformed error policy + label text.


def test_parse_bridge_dict_clean_object():
    out = json.dumps({"ok": True, "diagnostics": ["a", "b"]})
    assert parse_bridge_dict(out, "capacity") == {"ok": True, "diagnostics": ["a", "b"]}


def test_parse_bridge_dict_tolerates_noise_before_and_after():
    # Log chatter on both sides of the single JSON object — the object must still come back.
    out = 'WARNING: hub chatter\n{"ok": true, "n": 1}\n'
    assert parse_bridge_dict(out, "aggregation") == {"ok": True, "n": 1}


def test_parse_bridge_dict_empty_is_not_ok_and_labels_the_bridge():
    res = parse_bridge_dict("", "capacity")
    assert res["ok"] is False
    assert res["error"] == "capacity bridge produced no output"
    # whitespace-only is treated the same as empty
    assert parse_bridge_dict("   \n  ", "aggregation")["error"] == (
        "aggregation bridge produced no output"
    )


def test_parse_bridge_dict_no_json_is_not_ok():
    res = parse_bridge_dict("just logs, nothing parseable here", "capacity")
    assert res["ok"] is False
    assert res["error"].startswith("capacity bridge output was not JSON:")


def test_parse_bridge_dict_malformed_json_is_not_ok():
    # A dangling/partial object never parses -> the safe not-ok dict, not a raise.
    res = parse_bridge_dict('{"ok": true, "partial":', "aggregation")
    assert res["ok"] is False
    assert res["error"].startswith("aggregation bridge output was not JSON:")


def test_parse_bridge_dict_truncates_long_noise_in_error():
    # The error echoes only the trailing 500 chars of the offending stream.
    res = parse_bridge_dict("x" * 2000, "capacity")
    assert res["ok"] is False
    assert res["error"] == "capacity bridge output was not JSON: " + "x" * 500


def test_parse_bridge_dict_non_object_tail_is_not_ok():
    # Reconciliation: a bare JSON LIST is NOT a valid bridge result (callers expect a dict with
    # ok/error). Requiring a dict turns "would-be AttributeError on .get()" into the safe
    # not-ok path. (find_last_json itself would have returned the list.)
    assert find_last_json("[1, 2, 3]", "[") == [1, 2, 3]
    res = parse_bridge_dict("[1, 2, 3]", "capacity")
    assert res["ok"] is False
    assert res["error"].startswith("capacity bridge output was not JSON:")
