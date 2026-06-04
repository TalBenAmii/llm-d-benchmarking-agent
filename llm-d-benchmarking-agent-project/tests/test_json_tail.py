"""Tests for app.tools.json_tail.find_last_json.

Covers the contract (last balanced JSON value off a noisy stream) and pins the raw_decode
rewrite to be result-identical to the original naive backward-scan via an oracle.
"""
import json

import pytest

from app.tools.json_tail import find_last_json


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
