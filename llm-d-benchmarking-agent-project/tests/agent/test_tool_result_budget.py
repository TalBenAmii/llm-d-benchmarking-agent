"""Tests for app.agent.context_mgmt.clamp_tool_result_content.

The agent loop feeds each tool result back to the model as a string. The clamp must keep that
string within the char budget AND keep it valid JSON — the old `json.dumps(result)[:budget]`
sliced mid-structure and handed the model malformed JSON.
"""
import json

from app.agent.context_mgmt import clamp_tool_result_content


def test_small_result_passes_through_unchanged():
    # Below budget: byte-identical to the plain serialization (no envelope, no overhead).
    result = {"ok": True, "value": 42, "note": "hello"}
    out = clamp_tool_result_content(result, budget=6_000)
    assert out == json.dumps(result)
    assert json.loads(out) == result


def test_large_result_stays_valid_json_within_budget():
    result = {"runs": [{"name": f"run-{i}", "blob": "x" * 200} for i in range(200)]}
    budget = 2_000
    out = clamp_tool_result_content(result, budget=budget)
    assert len(out) <= budget
    parsed = json.loads(out)  # must not raise — the whole point of the fix
    assert parsed["_truncated"] is True
    assert parsed["_original_chars"] == len(json.dumps(result))
    assert "preview" in parsed and "_note" in parsed


def test_old_naive_slice_would_have_been_invalid():
    # Guard the regression directly: the naive approach produces malformed JSON here.
    result = {"data": [{"k": i, "v": "y" * 50} for i in range(100)]}
    budget = 1_500
    naive = json.dumps(result)[:budget]
    try:
        json.loads(naive)
        naive_was_valid = True
    except ValueError:
        naive_was_valid = False
    assert not naive_was_valid, "test fixture no longer exercises the mid-slice failure"

    out = clamp_tool_result_content(result, budget=budget)
    json.loads(out)  # the fix keeps it valid
    assert len(out) <= budget


def test_signal_fields_preserved_verbatim():
    # A big payload with a small error marker: the error must survive intact in the envelope.
    result = {"error": "kubectl failed: connection refused", "log": "noise " * 5_000}
    out = clamp_tool_result_content(result, budget=1_000)
    parsed = json.loads(out)
    assert parsed["error"] == "kubectl failed: connection refused"
    assert parsed["_truncated"] is True
    assert len(out) <= 1_000


def test_status_flags_preserved():
    result = {"rejected": True, "reason": "user declined", "detail": "z" * 10_000}
    out = clamp_tool_result_content(result, budget=900)
    parsed = json.loads(out)
    assert parsed["rejected"] is True
    assert parsed["reason"] == "user declined"


def test_preview_is_a_prefix_of_the_full_serialization():
    result = {"items": list(range(5_000))}
    full = json.dumps(result)
    out = clamp_tool_result_content(result, budget=800)
    preview = json.loads(out)["preview"]
    assert preview  # non-empty
    assert full.startswith(preview)


def test_non_dict_large_result_still_valid():
    result = list(range(5_000))  # a bare JSON array, not a dict
    out = clamp_tool_result_content(result, budget=600)
    parsed = json.loads(out)
    assert parsed["_truncated"] is True
    assert len(out) <= 600


def test_escaping_heavy_payload_respects_budget():
    # Quotes and newlines double under JSON escaping; the clamp must account for that expansion.
    result = {"text": '"' * 4_000 + "\n" * 4_000}
    budget = 1_200
    out = clamp_tool_result_content(result, budget=budget)
    assert len(out) <= budget
    json.loads(out)


def test_tiny_budget_falls_back_to_minimal_valid_envelope():
    result = {"a": "x" * 1_000, "b": "y" * 1_000}
    out = clamp_tool_result_content(result, budget=120)
    parsed = json.loads(out)  # still valid JSON even when the budget is very tight
    assert parsed["_truncated"] is True
    assert parsed["_original_chars"] == len(json.dumps(result))
