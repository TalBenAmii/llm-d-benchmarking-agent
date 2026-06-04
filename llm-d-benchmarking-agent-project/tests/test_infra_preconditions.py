"""Phase 60 — Infra precondition gate: K8s server version + vLLM/NIXL image minimums.

Hermetic, no live cluster / GPU / network. The probe is pure mechanism (FACTS only); the
go/no-go verdict lives entirely in knowledge/infrastructure_preconditions.yaml. So these tests:

  * feed canned `kubectl version --output json` for 1.27 / 1.29 / 1.33 through
    probe_environment (fake CaptureRunner, no real cluster) and assert the EXTRACTED
    server_version major.minor — including stripping a managed-cluster trailing '+';
  * parse a canned scenario YAML's vLLM/NIXL/UCX image tags off disk and assert image_tags;
  * assert the probe is read-only (it only ran `kubectl version --output json`, un-gated) and
    that exact argv is permitted read-only by the allowlist;
  * assert the threshold DATA (K8s 1.29 / 1.33 / 1.28 sidecar gotcha; vLLM 0.10.0 / NIXL 0.5.0
    / UCX 0.19.0 / NVSHMEM 3.3.9) lives in knowledge/infrastructure_preconditions.yaml — NOT in
    Python (no version-comparison if/elif in app/tools/probe.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.config import Settings
from app.security.allowlist import READ_ONLY, Allowlist
from app.tools.context import ToolContext
from app.tools.registry import dispatch
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# `kubectl version --output json` real shape: clientVersion + serverVersion {major, minor, ...}.
# minor on a managed cluster is often suffixed with '+', e.g. GKE reports "29+".


def _kver(minor: str, *, major: str = "1") -> str:
    git = f"v{major}.{minor.rstrip('+')}.4"
    return json.dumps({
        "clientVersion": {"major": "1", "minor": "30", "gitVersion": "v1.30.0"},
        "serverVersion": {"major": major, "minor": minor, "gitVersion": git, "platform": "linux/amd64"},
    })


VER_127 = _kver("27")
VER_129 = _kver("29")
VER_133 = _kver("33")
VER_129_PLUS = _kver("29+")  # managed-cluster '+' suffix

# A canned scenario YAML in the real `config/scenarios/<spec>.yaml` shape (scenario[].images.*).
SCENARIO_YAML = """
scenario:
  - name: real-pd
    model:
      name: meta-llama/Llama-3.1-8B
    images:
      vllm:
        repository: ghcr.io/llm-d/llm-d
        tag: v0.10.0
      nixl:
        repository: ghcr.io/llm-d/nixl
        tag: v0.5.0
      ucx:
        repository: ghcr.io/llm-d/ucx
        tag: v0.19.0
    standalone:
      enabled: false
      image:
        repository: docker.io/vllm/vllm-openai
        tag: v0.9.1
"""


def _probe_ctx(tmp_path: Path, *, canned, spec: str | None = None, scenario_yaml: str | None = None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws")
    # Author the spec's scenario YAML on disk under the (fake) bench repo so _parse_image_tags
    # reads it — repos stay read-only, this is a test fixture under tmp_path, not a real edit.
    if spec and scenario_yaml is not None:
        scen = settings.bench_repo / "config" / "scenarios" / f"{spec}.yaml"
        scen.parent.mkdir(parents=True, exist_ok=True)
        scen.write_text(scenario_yaml)
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


@pytest.fixture(autouse=True)
def _kubectl_present(monkeypatch):
    real_which = __import__("shutil").which

    def fake_which(name, *a, **k):
        return "/usr/bin/kubectl" if name == "kubectl" else real_which(name, *a, **k)

    monkeypatch.setattr("app.tools.probe.shutil.which", fake_which)


# ---------------------------------------------------------------------------
# server_version fact extraction for 1.27 / 1.29 / 1.33 (the spec's canned versions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canned_version,expected_minor",
    [(VER_127, "27"), (VER_129, "29"), (VER_133, "33")],
)
async def test_server_version_extracted_for_each_k8s(tmp_path, canned_version, expected_minor):
    ctx, runner = _probe_ctx(tmp_path, canned={"version --output json": canned_version})
    res = await dispatch(ctx, "probe_environment", {"checks": ["cluster_preconditions"]})
    cp = res["cluster_preconditions"]
    assert cp["available"] is True
    sv = cp["server_version"]
    assert sv["major"] == "1"
    assert sv["minor"] == expected_minor
    # The probe ran EXACTLY the read-only `kubectl version --output json` (no approval gate).
    vcalls = [c for c in runner.calls if c["argv"][:2] == ["kubectl", "version"]]
    assert len(vcalls) == 1
    assert vcalls[0]["argv"] == ["kubectl", "version", "--output", "json"]


async def test_server_version_strips_managed_cluster_plus_suffix(tmp_path):
    ctx, _ = _probe_ctx(tmp_path, canned={"version --output json": VER_129_PLUS})
    cp = (await dispatch(ctx, "probe_environment", {"checks": ["cluster_preconditions"]}))["cluster_preconditions"]
    # GKE-style "29+" is normalized to the bare "29" so the agent can compare it to thresholds,
    # while the raw value is preserved for provenance.
    assert cp["server_version"]["minor"] == "29"
    assert cp["server_version"]["raw"]["minor"] == "29+"


async def test_no_reachable_cluster_yields_null_server_version_not_a_verdict(tmp_path):
    # Non-JSON / client-only output (no serverVersion) => null fact, never a fabricated pass.
    ctx, _ = _probe_ctx(tmp_path, canned={"version --output json": "Client Version: v1.30.0\n"})
    cp = (await dispatch(ctx, "probe_environment", {"checks": ["cluster_preconditions"]}))["cluster_preconditions"]
    assert cp["available"] is False
    assert cp["server_version"] is None


# ---------------------------------------------------------------------------
# image_tag fact extraction from the spec's scenario YAML on disk
# ---------------------------------------------------------------------------


async def test_image_tags_parsed_from_spec_scenario(tmp_path):
    ctx, _ = _probe_ctx(
        tmp_path, canned={"version --output json": VER_133},
        spec="cicd/kind", scenario_yaml=SCENARIO_YAML,
    )
    res = await dispatch(ctx, "probe_environment",
                         {"checks": ["cluster_preconditions"], "spec": "cicd/kind"})
    cp = res["cluster_preconditions"]
    assert cp["spec"] == "cicd/kind"
    by_name = {t["name"]: t for t in cp["image_tags"]}
    # vLLM/NIXL/UCX image tags are extracted as plain FACTS (no judgment in the probe).
    assert by_name["vllm"]["tag"] == "v0.10.0"
    assert by_name["vllm"]["repository"] == "ghcr.io/llm-d/llm-d"
    assert by_name["nixl"]["tag"] == "v0.5.0"
    assert by_name["ucx"]["tag"] == "v0.19.0"
    # The nested standalone.image {repository,tag} is captured too (recursive walk, dotted path).
    assert any(t["repository"] == "docker.io/vllm/vllm-openai" and t["tag"] == "v0.9.1"
               for t in cp["image_tags"])


async def test_image_tags_empty_without_spec(tmp_path):
    # No spec => nothing to parse; the K8s version is still reported. Absent tags are never
    # fabricated into a pass.
    ctx, _ = _probe_ctx(tmp_path, canned={"version --output json": VER_133})
    cp = (await dispatch(ctx, "probe_environment", {"checks": ["cluster_preconditions"]}))["cluster_preconditions"]
    assert cp["image_tags"] == []
    assert cp["server_version"]["minor"] == "33"


async def test_image_tags_empty_when_scenario_missing(tmp_path):
    # Spec given but no scenario file on disk => [] (treated as 'unknown'), never raises.
    ctx, _ = _probe_ctx(tmp_path, canned={"version --output json": VER_133}, spec=None)
    cp = (await dispatch(ctx, "probe_environment",
                         {"checks": ["cluster_preconditions"], "spec": "no/such-spec"}))["cluster_preconditions"]
    assert cp["image_tags"] == []


# ---------------------------------------------------------------------------
# the threshold DATA lives in knowledge/, and the probe is read-only-allowlisted
# ---------------------------------------------------------------------------

KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge" / "infrastructure_preconditions.yaml"


def test_thresholds_live_in_knowledge_yaml():
    assert KNOWLEDGE.is_file(), "knowledge/infrastructure_preconditions.yaml must exist"
    data = yaml.safe_load(KNOWLEDGE.read_text())
    k8s = data["kubernetes"]
    # The K8s thresholds: 1.29 minimum, 1.33 recommended for sidecars, <=1.28 the Init:0/1 gotcha.
    assert str(k8s["minimum"]) == "1.29"
    assert str(k8s["recommended_for_sidecars"]) == "1.33"
    assert str(k8s["sidecar_init_stall_at_or_below"]) == "1.28"
    # The image library minimums.
    img = data["image_minimums"]
    assert str(img["vllm"]) == "0.10.0"
    assert str(img["nixl"]) == "0.5.0"
    assert str(img["ucx"]) == "0.19.0"
    assert str(img["nvshmem"]) == "3.3.9"


def test_verdict_wording_present_for_127_129_133():
    data = yaml.safe_load(KNOWLEDGE.read_text())
    blob = json.dumps(data).lower()
    # The three canonical verdicts are spelled out as DATA the LLM reasons over.
    assert "init:0/1" in blob  # the sidecar stall gotcha
    assert "non-sidecar" in blob  # the 1.27 escape hatch
    assert "1.33" in blob  # the recommended-for-sidecars band


def test_no_version_comparison_logic_in_python():
    """Thin-code guard: the DECISION must live in knowledge/, not as if/elif thresholds in the
    probe. Assert no threshold NUMBER appears in executable code (string/number literals) of
    app/tools/probe.py — only in docstrings/comments, which are allowed to *describe* and point
    to knowledge/. We strip docstrings + comments via the AST so prose pointers don't trip it."""
    import ast
    import io
    import tokenize

    probe_path = Path(__file__).resolve().parents[1] / "app" / "tools" / "probe.py"
    src = probe_path.read_text()

    # 1) No threshold appears as a STRING or NUMBER literal in the code (a comparison like
    #    `minor >= "1.29"` or `float(tag) < 0.10` would need one of these literals).
    tree = ast.parse(src)
    code_literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
            code_literals.append(str(node.value))
    # Drop module/class/function docstrings (ast.Constant string statements) — those are prose.
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                doc_nodes.add(doc)
    non_doc_literals = [lit for lit in code_literals if lit not in doc_nodes]
    for forbidden in ("1.29", "1.33", "1.28", "0.10.0", "0.5.0", "0.19.0", "3.3.9"):
        for lit in non_doc_literals:
            assert forbidden not in lit, (
                f"threshold {forbidden!r} leaked into a code literal in probe.py "
                f"({lit!r}) — version judgment belongs in knowledge/, not Python"
            )

    # 2) Belt-and-suspenders: strip all comments too and re-scan the code-only text, so a
    #    threshold can never hide in an inline `# if minor < 1.29` comment-as-code either.
    tokens = tokenize.generate_tokens(io.StringIO(src).readline)
    code_no_comments = "".join(
        t.string for t in tokens if t.type not in (tokenize.COMMENT,)
    )
    # Remove the (prose) docstrings from the code text before the final substring scan.
    for doc in doc_nodes:
        code_no_comments = code_no_comments.replace(doc, "")
    for forbidden in ("1.29", "1.33", "1.28", "0.10.0", "0.5.0", "0.19.0", "3.3.9"):
        assert forbidden not in code_no_comments, (
            f"threshold {forbidden!r} appears in probe.py code — it belongs in knowledge/"
        )


def test_kubectl_version_json_is_read_only_allowlisted(tmp_path):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    al = Allowlist.from_file(settings.allowlist_path)
    d = al.validate(["kubectl", "version", "--output", "json"], catalog=None)
    assert d.allowed and d.mode == READ_ONLY
