"""Phase 56 — Stack discovery tool (llm-d-discover).

Hermetic: NO network, NO cluster, NO real discovery tool. The handler is exercised end-to-end
through a ``CaptureRunner`` that fakes the ``llm-d-discover`` subprocess' JSON stdout — so the
whole chain (build argv -> allowlisted READ-ONLY dispatch -> parse the JSON list of BR-v0.2
stack components -> wrap as scenario capture in the workspace -> structured facts) is covered
without ever invoking the real tool. The allowlist value-pinning + read-only classification is
asserted directly against the real policy DATA.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.security.allowlist import READ_ONLY
from app.tools.context import ToolError
from app.tools.setup.discover import _parse_components, _summarize_stack, discover_stack
from app.tools.registry import dispatch, tool_definitions
from tests._helpers import _real_repo_ctx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


# A realistic `llm-d-discover -f benchmark-report` payload: a JSON LIST of BR-v0.2
# scenario.stack[] component dicts (one prefill + one decode inference engine + the EPP router),
# shaped exactly like discovery_to_stack_components() emits (metadata + standardized [+ native]).
_BR_COMPONENTS = [
    {
        "metadata": {"label": "vllm-prefill-0", "cfg_id": "abc"},
        "standardized": {
            "kind": "inference_engine",
            "tool": "vllm",
            "tool_version": "v0.10.0",
            "role": "prefill",
            "replicas": 1,
            "model": {"name": "meta-llama/Llama-3.1-8B"},
            "accelerator": {
                "model": "A100-80GB",
                "count": 1,
                "parallelism": {"tp": 1, "pp": 1, "dp": 1, "workers": 1, "ep": 1},
            },
        },
        "native": {"args": [], "envars": {}, "config": {}},
    },
    {
        "metadata": {"label": "vllm-decode-1", "cfg_id": "def"},
        "standardized": {
            "kind": "inference_engine",
            "tool": "vllm",
            "tool_version": "v0.10.0",
            "role": "decode",
            "replicas": 2,
            "model": {"name": "meta-llama/Llama-3.1-8B"},
            "accelerator": {
                "model": "A100-80GB",
                "count": 2,
                "parallelism": {"tp": 2, "pp": 1, "dp": 1, "workers": 1, "ep": 1},
            },
        },
        "native": {"args": [], "envars": {}, "config": {}},
    },
    {
        "metadata": {"label": "EPP", "cfg_id": "ghi"},
        "standardized": {"kind": "generic", "tool": "request_router", "tool_version": "v0.3.0"},
        "native": {"args": [], "envars": {}, "config": {}},
    },
]
_BR_OUTPUT = json.dumps(_BR_COMPONENTS, indent=2)


# ---- pure parsing / summarizing -------------------------------------------------

def test_parse_components_clean_json_list():
    comps = _parse_components(_BR_OUTPUT)
    assert isinstance(comps, list) and len(comps) == 3


def test_parse_components_tolerates_leading_log_noise():
    noisy = "2025-01-01 - INFO - Connecting to Kubernetes cluster...\n" + _BR_OUTPUT
    comps = _parse_components(noisy)
    assert len(comps) == 3


def test_parse_components_empty_raises():
    with pytest.raises(ToolError):
        _parse_components("")


def test_parse_components_non_list_raises():
    # A JSON OBJECT (not a list) is not a valid benchmark-report stack output.
    with pytest.raises(ToolError):
        _parse_components(json.dumps({"not": "a list"}))


def test_summarize_extracts_stack_facts():
    s = _summarize_stack(_BR_COMPONENTS)
    assert s["component_count"] == 3
    assert s["inference_engine_count"] == 2
    assert s["models"] == ["meta-llama/Llama-3.1-8B"]  # de-duplicated
    assert set(s["roles"]) == {"prefill", "decode"}
    assert "request_router" in s["tools"]
    # Per-engine parallelism is surfaced verbatim (facts, no verdict).
    decode = next(e for e in s["inference_engines"] if e["role"] == "decode")
    assert decode["replicas"] == 2
    assert decode["accelerator"]["parallelism"]["tp"] == 2


def test_summarize_tolerates_non_dict_components():
    # Regression: _parse_components only validates list-ness, not element shape. A garbled stream
    # with a non-dict element (or a non-dict standardized/model) must not crash _summarize_stack.
    s = _summarize_stack(["pod-a", 5, {"standardized": "x"}, {"standardized": {"model": "y"}}])
    assert s["component_count"] == 4
    assert s["models"] == [] and s["inference_engine_count"] == 0


# ---- the tool end-to-end (faked subprocess) -------------------------------------

async def test_discover_runs_readonly_and_writes_workspace(tmp_path):
    ctx, runner, emitted = _real_repo_ctx(tmp_path, canned={"llm-d-discover": _BR_OUTPUT})
    res = await discover_stack(ctx, endpoint_url="https://model.example.com/v1")

    assert res["ran"] is True
    assert res["endpoint_url"] == "https://model.example.com/v1"
    assert res["stack"]["component_count"] == 3
    assert res["stack"]["inference_engine_count"] == 2

    # It invoked `llm-d-discover <url> -f benchmark-report` exactly once.
    calls = [c for c in runner.calls if c["argv"] and c["argv"][0] == "llm-d-discover"]
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[1] == "https://model.example.com/v1"
    assert argv[2:4] == ["-f", "benchmark-report"]

    # Read-only => auto-ran (no approval), and the command was announced.
    cmd_events = [p for t, p in emitted if t == "command"]
    assert cmd_events and cmd_events[0]["auto_run"] is True
    assert cmd_events[0]["mode"] == READ_ONLY

    # The raw output + the wrapped scenario capture both land in the SESSION workspace.
    scen_path = Path(res["scenario_capture_path"])
    raw_path = Path(res["discovery_output_path"])
    assert scen_path.parent == ctx.workspace and raw_path.parent == ctx.workspace
    # The "report path" ingestion wraps the components as a BR-v0.2 scenario.stack capture.
    capture = json.loads(scen_path.read_text())
    assert capture["scenario"]["stack"] == _BR_COMPONENTS


async def test_discover_threads_optional_flags(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"llm-d-discover": _BR_OUTPUT})
    await discover_stack(
        ctx,
        endpoint_url="https://model.example.com/v1",
        kubeconfig="kubeconfigs/remote.yaml",
        context="prod",
        filter_type="vllm",
    )
    argv = next(c["argv"] for c in runner.calls if c["argv"][0] == "llm-d-discover")
    assert argv[argv.index("-k") + 1] == "kubeconfigs/remote.yaml"
    assert argv[argv.index("-c") + 1] == "prod"
    assert argv[argv.index("--filter") + 1] == "vllm"


async def test_discover_tool_not_installed_returns_ran_false(tmp_path):
    """When the venv binary is missing the runner raises a clear error; the handler degrades
    to ran=False with an install hint (it must NOT masquerade as a successful capture)."""
    ctx, _, _ = _real_repo_ctx(tmp_path)

    async def boom(*a, **k):
        raise ToolError("llm-d-discover not found — the benchmark venv is not set up yet")

    ctx.run_command = boom  # type: ignore[method-assign]
    res = await discover_stack(ctx, endpoint_url="https://model.example.com/v1")
    assert res["ran"] is False
    assert "stack" not in res
    assert "pip install -e" in res["note"]


async def test_discover_nonzero_exit_returns_ran_false(tmp_path):
    ctx, _, _ = _real_repo_ctx(tmp_path)

    class _Res:
        exit_code = 1
        output = "ERROR - Discovery failed: endpoint unreachable"

    async def fail(*a, **k):
        return _Res()

    ctx.run_command = fail  # type: ignore[method-assign]
    res = await discover_stack(ctx, endpoint_url="https://model.example.com/v1")
    assert res["ran"] is False
    assert "unreachable" in res["error"] or "unreachable" in res["stdout_tail"]
    assert "stack" not in res


async def test_discover_via_dispatch_validates_args(tmp_path):
    ctx, _, _ = _real_repo_ctx(tmp_path, canned={"llm-d-discover": _BR_OUTPUT})
    res = await dispatch(ctx, "discover_stack", {"endpoint_url": "https://model.example.com/v1"})
    assert res["ran"] is True
    # Missing the required endpoint_url is returned (not raised) so the agent self-corrects.
    bad = await dispatch(ctx, "discover_stack", {})
    assert "error" in bad


def test_discover_stack_is_registered_as_a_tool():
    names = {d["name"] for d in tool_definitions()}
    assert "discover_stack" in names


# ---- allowlist wiring (the policy DATA) -----------------------------------------

def test_allowlist_discover_is_read_only_and_autoruns(allowlist):
    d = allowlist.validate(
        ["llm-d-discover", "https://model.example.com/v1", "-f", "benchmark-report"]
    )
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval


def test_allowlist_discover_value_pins_output_format(allowlist):
    # A bogus output format is refused (enum-pinned).
    d = allowlist.validate(["llm-d-discover", "https://x/v1", "-f", "evil-format"])
    assert not d.allowed
    # Every real upstream format is accepted.
    for fmt in ("json", "yaml", "summary", "native", "native-yaml", "benchmark-report"):
        ok = allowlist.validate(["llm-d-discover", "https://x/v1", "-f", fmt])
        assert ok.allowed, fmt


def test_allowlist_discover_pins_url_and_kubeconfig(allowlist):
    # A non-URL positional is refused (endpoint_url constraint).
    assert not allowlist.validate(["llm-d-discover", "not-a-url"]).allowed
    # Path traversal in the kubeconfig is refused (kubeconfig_path: no '..').
    bad_kc = allowlist.validate(
        ["llm-d-discover", "https://x/v1", "-k", "../../etc/passwd"]
    )
    assert not bad_kc.allowed


def test_allowlist_discover_requires_the_url_positional(allowlist):
    assert not allowlist.validate(["llm-d-discover"]).allowed


def test_allowlist_discover_rejects_metacharacters(allowlist):
    # The blanket metacharacter screen still applies (defense in depth).
    assert not allowlist.validate(
        ["llm-d-discover", "https://x/v1; rm -rf /"]
    ).allowed


# ---- knowledge discoverability --------------------------------------------------

def test_stack_discovery_knowledge_is_loadable(tmp_path):
    from app.tools.access.knowledge_access import read_knowledge

    ctx, _, _ = _real_repo_ctx(tmp_path)
    out = read_knowledge(ctx, name="stack_discovery")
    assert "error" not in out
    body = out.get("content", "")
    assert "llm-d-discover" in body
    assert "probe_environment" in body  # documents that probing stays the default
