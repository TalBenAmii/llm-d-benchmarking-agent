"""Phase 8 — packaging: the production image + Helm/Kustomize one-command deploy.

Hermetic by default. The structural tests parse the shipped Dockerfile / chart templates /
Kustomize base as text + YAML and assert they agree with the app's real contract (the port,
the /healthz + /metrics paths app/main.py exposes, the least-privilege RBAC the orchestrator
actually uses, non-root hardening, no baked-in secrets, image pinning, and the orchestrator
ServiceAccount wiring). They run with no cluster, no Docker, and no extra binaries.

A second group RENDERS the chart/overlay with the real `helm`/`kustomize` when those binaries
happen to be installed (and skips cleanly otherwise) — this catches templating errors and
proves the rendered RBAC still matches the contract, without ever touching a cluster.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config import Settings
from app.packaging import assets
from app.packaging.assets import (
    AGENT_CONTAINER_PORT,
    AGENT_HEALTH_PATH,
    AGENT_METRICS_PATH,
    AGENT_READY_PATH,
    deploy_dir,
    helm_chart_dir,
    kustomize_base_dir,
    required_rbac_rules,
)

PROJECT_ROOT = Path(assets.PROJECT_ROOT)
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"


# ---- helpers ---------------------------------------------------------------

def _load_yaml_docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]


def _norm_rules(rules: list[dict]) -> set[tuple]:
    """A Role's rules as a comparable set of (sorted groups, sorted resources, sorted verbs)."""
    out = set()
    for r in rules:
        out.add((
            tuple(sorted(r.get("apiGroups", []))),
            tuple(sorted(r.get("resources", []))),
            tuple(sorted(r.get("verbs", []))),
        ))
    return out


CONTRACT_RULES = _norm_rules(required_rbac_rules())


def _find_kind(docs: list[dict], kind: str) -> dict:
    for d in docs:
        if d.get("kind") == kind:
            return d
    raise AssertionError(f"no {kind} found in docs: {[d.get('kind') for d in docs]}")


def _deployment_container(dep: dict) -> dict:
    containers = dep["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, "expected exactly one container in the agent Deployment"
    return containers[0]


# ===========================================================================
# The packaging contract (app.packaging.assets) is internally consistent
# ===========================================================================

def test_contract_matches_app_defaults():
    # The packaging contract must track the app's real config + main.py routes, or the deploy
    # would probe/scrape the wrong place. Settings default port is the container port.
    assert Settings(_env_file=None).port == AGENT_CONTAINER_PORT
    # The route paths are exactly what app/main.py serves.
    import app.main as main_mod

    src = Path(main_mod.__file__).read_text()
    assert f'"{AGENT_HEALTH_PATH}"' in src and f'"{AGENT_METRICS_PATH}"' in src
    # Phase 16 split readiness onto /readyz (per-component) from /healthz liveness — both served.
    assert f'"{AGENT_READY_PATH}"' in src


def test_contract_rbac_is_the_orchestrators_verbs_and_no_more():
    # Each kubectl op the RealKubeClient runs must be covered, and we must NOT grant anything
    # broader (no '*', no secrets/exec, no cluster scope) — the deploy reads this contract.
    rules = required_rbac_rules()
    by_res = {tuple(r["resources"]): r for r in rules}
    assert set(by_res[("jobs",)]["verbs"]) >= {"create", "get", "list", "watch", "delete"}
    assert set(by_res[("pods",)]["verbs"]) == {"get", "list", "watch"}
    assert by_res[("pods/log",)]["verbs"] == ["get"]
    all_verbs = {v for r in rules for v in r["verbs"]}
    all_res = {res for r in rules for res in r["resources"]}
    assert "*" not in all_verbs and "*" not in all_res
    assert not (all_res & {"secrets", "configmaps", "roles", "rolebindings"})


# ===========================================================================
# Dockerfile — production image hardening
# ===========================================================================

def test_dockerfile_exists_and_is_multistage_nonroot():
    assert DOCKERFILE.exists(), "a production Dockerfile must ship"
    text = DOCKERFILE.read_text()
    # Multi-stage (builder + runtime) keeps build tooling out of the final image.
    assert text.count("FROM ") >= 2
    assert "AS builder" in text and "AS runtime" in text
    # Runs as a non-root user (numeric uid so the K8s runAsNonRoot check is satisfiable).
    assert "USER 10001:10001" in text
    # kubectl is on PATH — the agent shells out to it (orchestrator + observability).
    assert "kubectl" in text
    # A healthcheck hits the real health path.
    assert "HEALTHCHECK" in text and AGENT_HEALTH_PATH in text
    # The server is launched (uvicorn) and the port is exposed.
    assert "uvicorn app.main:app" in text and f"EXPOSE {AGENT_CONTAINER_PORT}" in text
    # kubectl is pinned to a specific release, not "latest".
    assert "KUBECTL_VERSION=v" in text


def test_dockerfile_does_not_bake_in_secrets_or_scratch():
    text = DOCKERFILE.read_text()
    # The two read-only sibling repos and the workspace scratch are not copied into the image,
    # and crucially .env is never COPY'd.
    copied = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("COPY")]
    joined = "\n".join(copied)
    assert ".env" not in joined
    assert "llm-d-benchmark" not in joined and " llm-d " not in joined
    # The dirs the image needs at runtime ARE copied.
    assert any("app" in c for c in copied)
    assert any("security" in c for c in copied)
    assert any("knowledge" in c for c in copied)
    assert any("ui" in c for c in copied)


def test_dockerignore_excludes_secrets_and_scratch():
    assert DOCKERIGNORE.exists()
    patterns = {ln.strip() for ln in DOCKERIGNORE.read_text().splitlines()
                if ln.strip() and not ln.strip().startswith("#")}
    for needed in (".env", "workspace/", ".venv/", "tests/", ".git/"):
        assert needed in patterns, f".dockerignore must exclude {needed}"


# ===========================================================================
# Helm chart — structural (no helm binary required)
# ===========================================================================

def test_helm_chart_has_required_files():
    chart = helm_chart_dir()
    assert (chart / "Chart.yaml").exists()
    assert (chart / "values.yaml").exists()
    tmpl = chart / "templates"
    for f in ("deployment.yaml", "service.yaml", "serviceaccount.yaml", "rbac.yaml",
              "secret.yaml", "_helpers.tpl"):
        assert (tmpl / f).exists(), f"missing chart template {f}"


def test_helm_chart_metadata_tracks_app_version():
    meta = yaml.safe_load((helm_chart_dir() / "Chart.yaml").read_text())
    assert meta["name"] == assets.HELM_CHART_NAME
    assert meta["apiVersion"] == "v2"
    # appVersion should match the packaged app version (pyproject).
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    assert str(meta["appVersion"]) in pyproject


def _strip_helm_directives(text: str) -> str:
    """Make a chart template parse as static YAML without a helm binary: drop whole-line
    Go-template control directives ({{- if ... }}, {{- end -}}) and replace any INLINE
    {{ ... }} expression (e.g. a templated name) with a placeholder scalar. The RBAC rules we
    assert on have no templating, so this is exact for that template; the rendered-helm tests
    cover the templated bits end-to-end."""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("{{") and s.endswith("}}"):
            continue
        out.append(re.sub(r"\{\{.*?\}\}", "PLACEHOLDER", ln))
    return "\n".join(out)


def test_helm_rbac_template_matches_contract():
    # The Role rules are static in the template (no templating in the rules), so after stripping
    # the surrounding control directives we can parse them directly and compare to the contract.
    text = _strip_helm_directives((helm_chart_dir() / "templates" / "rbac.yaml").read_text())
    role = next(d for d in yaml.safe_load_all(text)
                if isinstance(d, dict) and d.get("kind") == "Role")
    assert _norm_rules(role["rules"]) == CONTRACT_RULES
    # And a RoleBinding (namespaced) binds it — not a ClusterRoleBinding.
    kinds = [d.get("kind") for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    assert "RoleBinding" in kinds and "ClusterRole" not in kinds and "ClusterRoleBinding" not in kinds


def test_helm_values_pin_image_and_default_safely():
    vals = yaml.safe_load((helm_chart_dir() / "values.yaml").read_text())
    # Image is referenced by repo+tag (pinnable to digest); pull policy set.
    assert vals["image"]["repository"]
    assert "tag" in vals["image"] and "digest" in vals["image"]
    # The orchestrator image defaults to empty so the tool refuses an unrunnable Job by default.
    assert vals["config"]["orchestratorImage"] == ""
    # No secret material is baked into the chart defaults.
    assert vals["secret"]["anthropicApiKey"] == ""
    assert vals["secret"]["openaiApiKey"] == ""
    # Non-root hardening defaults.
    assert vals["podSecurityContext"]["runAsNonRoot"] is True
    assert vals["securityContext"]["readOnlyRootFilesystem"] is True
    assert vals["securityContext"]["allowPrivilegeEscalation"] is False
    assert vals["securityContext"]["capabilities"]["drop"] == ["ALL"]


# ===========================================================================
# Kustomize base — structural (no kustomize binary required)
# ===========================================================================

def test_kustomize_base_lists_all_resources():
    base = kustomize_base_dir()
    kz = yaml.safe_load((base / "kustomization.yaml").read_text())
    for r in ("serviceaccount.yaml", "rbac.yaml", "deployment.yaml", "service.yaml"):
        assert r in kz["resources"]
        assert (base / r).exists()
    # The image is named so overlays / `kustomize edit set image` can pin it.
    assert kz["images"] and kz["images"][0]["name"]


def test_kustomize_rbac_matches_contract():
    role = _find_kind(_load_yaml_docs(kustomize_base_dir() / "rbac.yaml"), "Role")
    assert _norm_rules(role["rules"]) == CONTRACT_RULES


def test_kustomize_deployment_matches_app_contract():
    dep = _find_kind(_load_yaml_docs(kustomize_base_dir() / "deployment.yaml"), "Deployment")
    spec = dep["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "llm-d-benchmarking-agent"
    # Non-root + hardened.
    assert spec["securityContext"]["runAsNonRoot"] is True
    c = _deployment_container(dep)
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    # Probes hit the real health path on the container port.
    port = next(p["containerPort"] for p in c["ports"] if p.get("name") == "http")
    assert port == AGENT_CONTAINER_PORT
    assert c["livenessProbe"]["httpGet"]["path"] == AGENT_HEALTH_PATH
    # Readiness hits the dedicated /readyz (Phase 16), not liveness's /healthz.
    assert c["readinessProbe"]["httpGet"]["path"] == AGENT_READY_PATH
    # Writable scratch is an emptyDir (root FS is read-only), keyed to WORKSPACE_DIR.
    env = {e["name"]: e for e in c["env"]}
    assert env["WORKSPACE_DIR"]["value"] == "/workspace"
    mounts = {m["name"]: m["mountPath"] for m in c["volumeMounts"]}
    assert mounts.get("workspace") == "/workspace"
    vols = {v["name"]: v for v in spec["volumes"]}
    assert "emptyDir" in vols["workspace"]
    # Secrets come from secretKeyRef (never inline values).
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HF_TOKEN"):
        assert "secretKeyRef" in env[key]["valueFrom"]
        assert "value" not in env[key]


def test_pod_is_scrape_annotated_for_metrics():
    for path in (helm_chart_dir() / "templates" / "deployment.yaml",
                 kustomize_base_dir() / "deployment.yaml"):
        text = path.read_text()
        assert "prometheus.io/scrape" in text
        assert AGENT_METRICS_PATH in text


def test_overlay_keeps_secret_env_out_of_git():
    # The example overlay generates a Secret from secret.env, which must be gitignored.
    overlay = deploy_dir() / "kustomize" / "overlays" / "example"
    assert (overlay / "kustomization.yaml").exists()
    assert (overlay / "secret.env.example").exists()
    assert not (overlay / "secret.env").exists(), "a real secret.env must never be committed"
    gi = (PROJECT_ROOT / ".gitignore").read_text()
    assert "secret.env" in gi


# ===========================================================================
# Optional: render with the real helm/kustomize if available (still no cluster)
# ===========================================================================

def _render_yaml_docs(argv: list[str]) -> list[dict]:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"{argv[0]} failed:\n{out.stderr}"
    return [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_renders_consistent_manifests():
    chart = str(helm_chart_dir())
    # Lint must pass.
    lint = subprocess.run(["helm", "lint", chart], capture_output=True, text=True, timeout=60)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    docs = _render_yaml_docs(["helm", "template", "rel", chart])
    kinds = {d["kind"] for d in docs}
    assert {"ServiceAccount", "Role", "RoleBinding", "Service", "Deployment"} <= kinds

    role = _find_kind(docs, "Role")
    assert _norm_rules(role["rules"]) == CONTRACT_RULES

    dep = _find_kind(docs, "Deployment")
    sa_name = dep["spec"]["template"]["spec"]["serviceAccountName"]
    # The Deployment runs as the SA the chart creates, and the RoleBinding targets that SA.
    rb = _find_kind(docs, "RoleBinding")
    assert any(s["kind"] == "ServiceAccount" and s["name"] == sa_name for s in rb["subjects"])
    sa = _find_kind(docs, "ServiceAccount")
    assert sa["metadata"]["name"] == sa_name


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_digest_pin_overrides_tag():
    docs = _render_yaml_docs(
        ["helm", "template", "rel", str(helm_chart_dir()), "--set", "image.digest=sha256:abc123"]
    )
    dep = _find_kind(docs, "Deployment")
    image = _deployment_container(dep)["image"]
    assert image.endswith("@sha256:abc123"), image


@pytest.mark.skipif(shutil.which("kustomize") is None, reason="kustomize not installed")
def test_kustomize_base_renders_consistent_manifests():
    docs = _render_yaml_docs(["kustomize", "build", str(kustomize_base_dir())])
    kinds = {d["kind"] for d in docs}
    assert {"ServiceAccount", "Role", "RoleBinding", "Service", "Deployment"} <= kinds
    role = _find_kind(docs, "Role")
    assert _norm_rules(role["rules"]) == CONTRACT_RULES
