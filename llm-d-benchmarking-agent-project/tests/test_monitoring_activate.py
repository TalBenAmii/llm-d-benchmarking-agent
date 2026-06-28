"""Phase 27 — default-enable benchmark ``--monitoring`` + surface results.observability.

Hermetic, no cluster / GPU / network. Exercises the PRODUCER half this phase adds (the
CONSUMER half — parsing results.observability — is Phase 25, covered in test_standard_metrics):

  * build_argv emits the SUBCOMMAND-AWARE monitoring flag: True => --monitoring on
    standup/run/experiment/plan; False => --no-monitoring on STANDUP ONLY (run/experiment/plan
    have no such upstream flag and simply omit it); None/absent => nothing;
  * the allowlist permits EXACTLY those flags per subcommand (and rejects --no-monitoring's
    value-laden abuse via the metachar screen), with the flagged commands keeping their
    read-only/mutating classification;
  * the prometheus_crds probe is a read-only fact-reporter (present iff BOTH CRDs exist), and
    the CRD-less opt-out path is *selectable* from a probed no-CRD environment;
  * a fixture BR v0.2 with a populated results.observability surfaces KV-cache/GPU/queue metrics
    through summarize_report AND analyze_results once the producer "ran".
"""
from __future__ import annotations

import pytest
import yaml

from app.config import Settings
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.tools import analyze
from app.tools.context import ToolContext
from app.tools.execute import build_argv
from app.tools.registry import dispatch
from app.tools.schemas import ExecuteInput
from app.validation.report import summarize_report
from tests._helpers import _argv, kubectl_present
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# ---------------------------------------------------------------------------
# build_argv — subcommand-aware monitoring emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", ["standup", "run", "experiment", "plan"])
def test_monitoring_true_emits_monitoring_for_every_subcommand(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"monitoring": True})
    assert "--monitoring" in argv
    assert "--no-monitoring" not in argv


def test_monitoring_false_emits_no_monitoring_only_on_standup():
    argv = build_argv("standup", spec="cicd/kind", flags={"monitoring": False})
    assert "--no-monitoring" in argv
    assert "--monitoring" not in argv  # the bare --monitoring substring must NOT leak


@pytest.mark.parametrize("subcommand", ["run", "experiment", "plan"])
def test_monitoring_false_omits_flag_on_non_standup(subcommand):
    # run/experiment/plan are store_true upstream: no --no-monitoring exists; an opt-out just
    # omits the flag (no scraping), it does NOT silently turn monitoring on.
    argv = build_argv(subcommand, spec="cicd/kind", flags={"monitoring": False})
    assert "--monitoring" not in argv
    assert "--no-monitoring" not in argv


@pytest.mark.parametrize("subcommand", ["standup", "run", "experiment", "plan"])
def test_monitoring_unset_emits_nothing(subcommand):
    # No monitoring key => scenario defaults; we never inject a flag the agent didn't set.
    argv_none = build_argv(subcommand, spec="cicd/kind", flags={"monitoring": None})
    argv_absent = build_argv(subcommand, spec="cicd/kind", flags={})
    for argv in (argv_none, argv_absent):
        assert "--monitoring" not in argv and "--no-monitoring" not in argv


def test_monitoring_does_not_disturb_other_flags():
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"monitoring": True, "output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert "--monitoring" in argv and "-r" in argv and "local" in argv


def test_execute_schema_accepts_monitoring_flag():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"monitoring": True})
    assert m.flags == {"monitoring": True}


# ---------------------------------------------------------------------------
# allowlist — exactly the right flags are permitted per subcommand (DATA)
# ---------------------------------------------------------------------------


def test_allowlist_permits_monitoring_on_standup_run_experiment_plan(allowlist, catalog):
    for sub, extra in (
        ("standup", []),
        ("run", ["-l", "inference-perf", "-w", "sanity_random.yaml"]),
        ("experiment", ["-e", "workspace/exp.yaml"]),
        ("plan", []),
    ):
        d = allowlist.validate(_argv(sub, *extra, "--monitoring"), catalog=catalog)
        assert d.allowed, f"--monitoring should be allowed on {sub}: {d.reason}"


def test_allowlist_permits_no_monitoring_on_standup(allowlist, catalog):
    d = allowlist.validate(_argv("standup", "--no-monitoring"), catalog=catalog)
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_monitoring_flags_do_not_change_mode_classification(allowlist, catalog):
    # standup stays mutating with --monitoring; plan stays read-only with --monitoring.
    assert allowlist.validate(_argv("standup", "--monitoring"), catalog=catalog).mode == MUTATING
    assert allowlist.validate(_argv("plan", "--monitoring"), catalog=catalog).mode == READ_ONLY
    # --dry-run still downgrades a monitored standup to a read-only preview.
    d = allowlist.validate(_argv("standup", "--monitoring", "--dry-run"), catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


def test_no_monitoring_value_abuse_is_screened(allowlist, catalog):
    # It is a boolean flag; a metachar-laden trailing value is still rejected by the screen.
    assert not allowlist.validate(
        _argv("standup", "--no-monitoring", "a;rm -rf /"), catalog=catalog
    ).allowed


# ---------------------------------------------------------------------------
# prometheus_crds probe — read-only fact reporter + the CRD-less opt-out
# ---------------------------------------------------------------------------

# `kubectl get crd -o name` prints one CRD per line, apiVersion-prefixed.
CRDS_PRESENT = (
    "customresourcedefinition.apiextensions.k8s.io/podmonitors.monitoring.coreos.com\n"
    "customresourcedefinition.apiextensions.k8s.io/servicemonitors.monitoring.coreos.com\n"
    "customresourcedefinition.apiextensions.k8s.io/something.else.io\n"
)
CRDS_PARTIAL = (  # PodMonitor only — NOT enough (both are required)
    "customresourcedefinition.apiextensions.k8s.io/podmonitors.monitoring.coreos.com\n"
)
CRDS_NONE = "customresourcedefinition.apiextensions.k8s.io/something.else.io\n"


def _probe_ctx(tmp_path, *, canned):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws")
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
    kubectl_present(monkeypatch, target="app.tools.probe")


async def test_probe_prometheus_crds_present(tmp_path):
    ctx, runner = _probe_ctx(tmp_path, canned={"get crd": CRDS_PRESENT})
    res = await dispatch(ctx, "probe_environment", {"checks": ["prometheus_crds"]})
    p = res["prometheus_crds"]
    assert p["available"] is True
    assert p["podmonitors_crd"] is True and p["servicemonitors_crd"] is True
    assert p["present"] is True
    # the probe is READ-ONLY: it never asked for approval, and only ran `kubectl get crd`.
    crd_calls = [c for c in runner.calls if c["argv"][:3] == ["kubectl", "get", "crd"]]
    assert len(crd_calls) == 1


async def test_probe_prometheus_crds_partial_is_not_present(tmp_path):
    ctx, _ = _probe_ctx(tmp_path, canned={"get crd": CRDS_PARTIAL})
    p = (await dispatch(ctx, "probe_environment", {"checks": ["prometheus_crds"]}))["prometheus_crds"]
    assert p["podmonitors_crd"] is True and p["servicemonitors_crd"] is False
    assert p["present"] is False  # BOTH required


async def test_probe_prometheus_crds_absent(tmp_path):
    ctx, _ = _probe_ctx(tmp_path, canned={"get crd": CRDS_NONE})
    p = (await dispatch(ctx, "probe_environment", {"checks": ["prometheus_crds"]}))["prometheus_crds"]
    assert p["present"] is False
    assert p["podmonitors_crd"] is False and p["servicemonitors_crd"] is False


async def test_crdless_environment_selects_no_monitoring_opt_out(tmp_path):
    """End-to-end of the OPT-OUT: probe a CRD-less cluster (whose scenario also won't install
    the CRDs), let that fact drive monitoring=False, and assert the standup emits --no-monitoring
    while a run merely omits scraping. The decision itself is knowledge-driven; here we prove the
    selected boolean produces the correct argv per subcommand."""
    ctx, _ = _probe_ctx(tmp_path, canned={"get crd": CRDS_NONE})
    probed = (await dispatch(ctx, "probe_environment", {"checks": ["prometheus_crds"]}))["prometheus_crds"]
    assert probed["present"] is False
    # The agent's knowledge maps "no CRDs and no scenario install" -> monitoring off.
    monitoring = False
    assert "--no-monitoring" in build_argv("standup", spec="cicd/kind", flags={"monitoring": monitoring})
    assert "--monitoring" not in build_argv("run", spec="cicd/kind", flags={"monitoring": monitoring})


# ---------------------------------------------------------------------------
# the OTHER side: when monitoring HAS run, the metrics surface in the summary
# ---------------------------------------------------------------------------


def _report_with_observability():
    """A BR v0.2 carrying a populated results.observability — exactly what --monitoring fills."""
    def stat(mean):
        return {"units": "percent", "mean": mean, "p50": mean, "p99": mean}
    return {
        "version": "0.2",
        "run": {"uid": "monitored-run"},
        "scenario": {"load": {"standardized": {"tool": "inference-perf"}}},
        "results": {
            "request_performance": {"aggregate": {"requests": {"total": 500, "failures": 0}}},
            "observability": {
                "components": [
                    {
                        "component_label": "vllm-decode-0",
                        "aggregate": {
                            "cache_hit_rate": stat(72.0),
                            "gpu_utilization": stat(88.0),
                            "waiting_requests": {"units": "count", "mean": 4.0, "p99": 19.0},
                        },
                    }
                ]
            },
        },
    }


def test_populated_observability_surfaces_in_summary():
    s = summarize_report(_report_with_observability())
    sm = s["standard_metrics"]
    assert sm is not None
    assert set(sm) == {"kv_cache_hit_rate", "schedule_delay", "gpu_utilization"}
    assert sm["kv_cache_hit_rate"]["value"]["mean"] == 72.0
    assert sm["gpu_utilization"]["value"]["mean"] == 88.0
    assert sm["schedule_delay"]["proxy"] is True


def test_empty_observability_yields_no_metrics_not_fabricated():
    rep = _report_with_observability()
    rep["results"]["observability"] = {}  # producer did NOT run -> empty block
    assert summarize_report(rep)["standard_metrics"] is None


async def test_analyze_results_surfaces_observability_for_single_run(tool_ctx, br_example, tmp_path):
    """Drive a SCHEMA-VALID BR v0.2 (the repo's own example) carrying a populated
    results.observability through the analyze_results TOOL, and assert the KV-cache / GPU /
    queue-depth metrics surface per run (single run, no sweep). analyze_results requires a
    schema-valid report, so we build on the real example rather than a minimal hand fixture."""
    from app.validation.report import load_report

    if not br_example.exists():
        pytest.skip("BR v0.2 example not present")
    rep = load_report(br_example)
    # Pin a known GPU-util value via the standardized ResourceMetrics shape so the assertion is
    # exact (the example already carries gpu_utilization as a standardized aggregate field).
    for comp in rep["results"]["observability"].get("components", []):
        agg = comp.get("aggregate")
        if isinstance(agg, dict) and "gpu_utilization" in agg:
            agg["gpu_utilization"]["mean"] = 88.0
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    (run_dir / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))
    out = await analyze.analyze_results(tool_ctx, sources=[str(run_dir)])
    assert out["analyzed"] is True and out["n"] == 1
    sm = out["runs"][0]["standard_metrics"]
    assert {"kv_cache_hit_rate", "schedule_delay", "gpu_utilization"} <= set(sm)
    assert sm["gpu_utilization"]["source"] == "standardized"
    assert sm["gpu_utilization"]["value"]["mean"] == 88.0


# ---------------------------------------------------------------------------
# sanity: the canned probe really validated through the allowlist (no metachar leak)
# ---------------------------------------------------------------------------


def test_get_crd_is_read_only_allowlisted(allowlist):
    # The probe lists ALL CRDs read-only and filters in Python (one positional: the resource
    # type), so this exact argv is what the prometheus_crds probe issues.
    d = allowlist.validate(["kubectl", "get", "crd", "-o", "name"], catalog=None)
    assert d.allowed and d.mode == READ_ONLY
