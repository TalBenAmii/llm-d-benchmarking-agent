"""Agent tool: run a CHAOS / RESILIENCE drill.

DOUBLE-gated, opt-in: refuses unless ``ctx.settings.chaos_enabled`` AND the agent invokes this
named tool — chaos is never reachable from ``orchestrate_benchmark_run``. The drill drives the
COMPLETELY UNMODIFIED retry/dead-letter + reconstruct/checkpoint lifecycle against an in-process
driver wrapped by the :class:`~app.orchestrator.chaos.ChaosKubeClient` decorator, so injected
faults flow through the real ``classify_failure`` → ``run_with_retries`` path and the report is a
genuine proof. It runs against a fake/in-process cluster — it never deliberately breaks a real
one. Mechanism only: the agent chooses the ``chaos_plan`` (judgment, in
``knowledge/resilience.md``); Python validates its shape and joins the facts.
"""
from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.observability import instrument
from app.orchestrator.chaos import ChaosKubeClient, ChaosPlan
from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import (
    LABEL_RUN,
    JobSpec,
    job_name,
)
from app.orchestrator.resilience import build_resilience_report
from app.orchestrator.restart import prove_restart_recovery
from app.security.runner import RunResult
from app.tools.context import ToolContext, ToolError


class _DrillKubeClient:
    """A minimal in-process :class:`~app.orchestrator.kube.KubeClient` for the hermetic drill.

    By default an applied Job auto-completes as ``succeeded`` (so a NON-faulted attempt
    succeeds); the chaos decorator then rewrites the reads of faulted attempts into the failed
    fault shape. Mirrors the shapes the orchestrator + classifier consume (the same contract as
    ``tests/orchestrator_fakes.FakeKubeClient``, kept here so the SHIPPED tool needs no test
    code). Never touches a real cluster."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        # (namespace, run_id) -> the succeeded Job snapshot we serve for it.
        self._jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self._configmaps: dict[tuple[str, str], dict[str, Any]] = {}

    async def apply(self, manifest_path: str | Path, *, namespace: str) -> RunResult:
        import yaml
        manifest = yaml.safe_load(Path(manifest_path).read_text())
        self.applied.append((namespace, manifest))
        kind = manifest.get("kind")
        if kind == "ConfigMap":
            name = manifest.get("metadata", {}).get("name", "")
            self._configmaps[(namespace, name)] = manifest
            return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "apply"], cwd=None)
        labels = (manifest.get("metadata", {}) or {}).get("labels", {}) or {}
        rid = labels.get(LABEL_RUN)
        if rid:
            # Auto-complete as succeeded; the chaos decorator overrides faulted attempts on read.
            self._jobs[(namespace, rid)] = {
                "apiVersion": "batch/v1", "kind": "Job",
                "metadata": {"name": job_name(rid), "namespace": namespace, "labels": dict(labels),
                             "annotations": (manifest.get("metadata", {}) or {}).get("annotations", {})},
                "spec": {"backoffLimit": 0},
                "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
            }
        return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "apply"], cwd=None)

    async def list_jobs(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        sel = _parse_selector(selector)
        out = []
        for (ns, _rid), job in self._jobs.items():
            if ns != namespace:
                continue
            if _matches(job.get("metadata", {}).get("labels", {}), sel):
                out.append(job)
        return out

    async def list_pods(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        return []  # a succeeded run has no fault pods; the chaos decorator supplies faulted ones

    async def list_configmaps(self, *, namespace: str,
                              selector: str | None = None) -> list[dict[str, Any]]:
        sel = _parse_selector(selector)
        out = []
        for (ns, _name), cm in self._configmaps.items():
            if ns != namespace:
                continue
            if _matches(cm.get("metadata", {}).get("labels", {}), sel):
                out.append(cm)
        return out

    async def logs(self, *, namespace: str, selector: str, tail: int | None = None,
                   follow: bool = False) -> str:
        return ""

    async def stream_log_lines(self, *, namespace: str, selector: str,
                               tail: int | None = None) -> AsyncIterator[str]:
        return
        yield  # pragma: no cover - make this an async generator that yields nothing

    async def delete_job(self, name: str, *, namespace: str,
                         ignore_not_found: bool = True) -> RunResult:
        self.deleted.append((namespace, name))
        for key in list(self._jobs):
            if key[0] == namespace and job_name(key[1]) == name:
                del self._jobs[key]
        return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "delete", "job", name], cwd=None)


def _parse_selector(selector: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (selector or "").split(","):
        k, _, v = part.strip().partition("=")
        if k and "=" in part:
            out[k] = v
    return out


def _matches(labels: dict[str, str], sel: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in sel.items())


def _drill_spec(run_id: str, *, namespace: str, image: str, spec: str | None,
                harness: str | None, workload: str | None, session_id: str) -> JobSpec:
    return JobSpec(
        run_id=run_id,
        namespace=namespace,
        image=image,
        command=["llmdbenchmark", "run", "-p", namespace],
        session_id=session_id,
        spec=spec or "",
        harness=harness or "",
        workload=workload or "",
    )


async def run_resilience_drill(
    ctx: ToolContext,
    *,
    namespace: str,
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    image: str | None = None,
    chaos_plan: dict[str, Any] | None = None,
    max_attempts: int = 3,
    prove_restart: bool = True,
    slo_budget_s: float = 600.0,
) -> dict[str, Any]:
    # Production guard (layer 1): the chaos seam must be explicitly enabled in the backend.
    if not ctx.settings.chaos_enabled:
        raise ToolError(
            "the resilience drill is disabled: it injects faults via an opt-in chaos seam that is "
            "OFF in production. Set CHAOS_ENABLED=true in the backend .env to allow it. (The drill "
            "runs against an in-process cluster and never breaks a real one.)"
        )

    # Parse the agent's chaos plan — PURE SHAPE validation; a bad shape is a self-correctable
    # ToolError (the WHICH-faults-to-inject judgment is the agent's, knowledge/resilience.md).
    try:
        plan = ChaosPlan.from_dict(chaos_plan)
    except ValueError as exc:
        raise ToolError(f"invalid chaos_plan: {exc}") from exc

    session_id = ctx.workspace.name
    base_run_id = "rd-" + uuid.uuid4().hex[:6]

    # The in-process driver wrapped by the chaos decorator: injected faults flow through the
    # UNMODIFIED classify_failure → run_with_retries path. Drives a fake — never a real cluster.
    driver = _DrillKubeClient()
    chaos = ChaosKubeClient(driver, plan)
    orch = BenchmarkOrchestrator(chaos, ctx.workspace)

    drill_spec = _drill_spec(base_run_id, namespace=namespace, image=image or "chaos-drill",
                             spec=spec, harness=harness, workload=workload, session_id=session_id)

    start = time.monotonic()
    outcome = await orch.run_with_retries(
        drill_spec, max_attempts=max_attempts, poll_interval=0, max_wait=60.0,
    )
    elapsed = time.monotonic() - start

    # File the injected faults as a metric (best-effort; observability never disrupts the drill).
    for entry in chaos.ledger.realized():
        with contextlib.suppress(Exception):  # metrics must never break the drill
            instrument.record_fault_injected(entry.kind)

    # Restart-durability proof: a FRESH orchestrator resumes a partial sweep from the cluster
    # checkpoint with 0 duplicate Jobs (the honest object-discard-and-rehydrate model). Uses a
    # clean driver (the checkpoint/reconstruct path is exercised, not the chaos faults).
    restart = None
    if prove_restart:
        restart = await _prove_restart(namespace=namespace, session_id=session_id, image=image,
                                       spec=spec, harness=harness, workload=workload,
                                       workspace=ctx.workspace)

    report = build_resilience_report(outcome, chaos.ledger, restart,
                                     slo_budget_s=slo_budget_s, elapsed_s=elapsed)
    return report.to_dict()


async def _prove_restart(*, namespace: str, session_id: str, image: str | None, spec: str | None,
                         harness: str | None, workload: str | None, workspace: Path):
    """Stage a partial sweep (k of N treatments completed + checkpointed to the cluster
    ConfigMap), then prove a FRESH orchestrator resumes the remainder with 0 duplicate Jobs —
    reusing the EXISTING run_sweep checkpoint/resume + reconstruct machinery (no new logic)."""
    driver = _DrillKubeClient()
    sweep_id = "rd-sw-" + uuid.uuid4().hex[:6]
    treatments = [f"t{i}" for i in range(1, 5)]  # N=4
    specs = [
        _drill_spec(t, namespace=namespace, image=image or "chaos-drill", spec=spec,
                    harness=harness, workload=workload, session_id=session_id)
        for t in treatments
    ]
    for s in specs:
        s.sweep_id = sweep_id

    # Stage: run the first k=2 treatments (they complete + checkpoint to the cluster ConfigMap),
    # modelling the work done BEFORE the (simulated) orchestrator restart.
    staging = BenchmarkOrchestrator(driver, workspace)
    await staging.run_sweep(specs[:2], max_parallel=2, max_attempts=1,
                            poll_interval=0, sweep_id=sweep_id, namespace=namespace)

    # Restart: a brand-new orchestrator (no shared local state) resumes the full sweep.
    return await prove_restart_recovery(
        driver, workspace, namespace=namespace, session_id=session_id,
        sweep_id=sweep_id, specs=specs, max_parallel=2, max_attempts=1, poll_interval=0,
    )
