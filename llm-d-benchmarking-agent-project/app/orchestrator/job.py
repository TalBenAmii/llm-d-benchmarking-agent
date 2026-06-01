"""Benchmark-run Job model: turn a structured spec into a Kubernetes Job manifest, and
classify a Job's live status into a small, stable phase enum.

A benchmark run is modelled as a K8s **Job** the orchestrator owns end-to-end, so it is
observable (Watch/poll), restart-reconstructable (labels/annotations), and individually
retryable. ``backoffLimit: 0`` means a single pod failure fails the Job immediately — the
orchestrator (not Kubernetes) decides whether to resubmit a fresh attempt, so every attempt
is a distinct, inspectable Job. ``activeDeadlineSeconds`` lets Kubernetes mark a hung run
``DeadlineExceeded`` (classified as a timeout).

Pure functions only — no cluster access (that's :mod:`app.orchestrator.kube`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Label keys (values must be DNS-label-safe → simple ids only; richer strings like the
# spec/harness/workload, which contain '/', go in annotations).
LABEL_MANAGED = "app.kubernetes.io/managed-by"
LABEL_SESSION = "llmd-bench/session"
LABEL_RUN = "llmd-bench/run-id"
LABEL_SWEEP = "llmd-bench/sweep"
LABEL_TREATMENT = "llmd-bench/treatment"
MANAGED_BY = "llmd-bench-agent"

ANNO_SPEC = "llmd-bench/spec"
ANNO_HARNESS = "llmd-bench/harness"
ANNO_WORKLOAD = "llmd-bench/workload"
ANNO_ATTEMPT = "llmd-bench/attempt"

# Phases (stable, UI/agent-facing).
PENDING = "pending"
ACTIVE = "active"
SUCCEEDED = "succeeded"
FAILED = "failed"
ABSENT = "absent"


def job_name(run_id: str) -> str:
    return f"llmd-bench-{run_id}"


@dataclass
class JobSpec:
    """What to run, as agent-supplied intent. Mechanism turns this into a manifest;
    judgment (spec/harness/workload, the grid) stays with the agent + knowledge files."""
    run_id: str
    namespace: str
    image: str
    command: list[str]                       # argv executed inside the Job's pod
    session_id: str = ""
    sweep_id: str = ""
    treatment: int | None = None
    attempt: int = 1
    spec: str = ""                            # llm-d spec (annotation; may contain '/')
    harness: str = ""
    workload: str = ""
    active_deadline_seconds: int | None = None
    cpu: str = "1"
    memory: str = "1Gi"
    env: dict[str, str] = field(default_factory=dict)
    service_account: str | None = None

    def labels(self) -> dict[str, str]:
        out = {LABEL_MANAGED: MANAGED_BY, LABEL_RUN: self.run_id}
        if self.session_id:
            out[LABEL_SESSION] = self.session_id
        if self.sweep_id:
            out[LABEL_SWEEP] = self.sweep_id
        if self.treatment is not None:
            out[LABEL_TREATMENT] = str(self.treatment)
        return out

    def annotations(self) -> dict[str, str]:
        out = {ANNO_ATTEMPT: str(self.attempt)}
        if self.spec:
            out[ANNO_SPEC] = self.spec
        if self.harness:
            out[ANNO_HARNESS] = self.harness
        if self.workload:
            out[ANNO_WORKLOAD] = self.workload
        return out


def build_job_manifest(spec: JobSpec) -> dict[str, Any]:
    """Render a :class:`JobSpec` into a Kubernetes Job manifest (a plain dict, ready to
    YAML-dump). Pod template carries the same labels so ``kubectl logs -l run-id=<id>`` and
    pod fault inspection select this run's pods."""
    container: dict[str, Any] = {
        "name": "benchmark",
        "image": spec.image,
        "command": list(spec.command),
        "resources": {
            "requests": {"cpu": spec.cpu, "memory": spec.memory},
            "limits": {"cpu": spec.cpu, "memory": spec.memory},
        },
    }
    if spec.env:
        container["env"] = [{"name": k, "value": v} for k, v in spec.env.items()]

    pod_spec: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
    if spec.service_account:
        pod_spec["serviceAccountName"] = spec.service_account

    job_spec: dict[str, Any] = {
        "backoffLimit": 0,  # the orchestrator owns retries, not Kubernetes
        "template": {"metadata": {"labels": spec.labels()}, "spec": pod_spec},
    }
    if spec.active_deadline_seconds is not None:
        job_spec["activeDeadlineSeconds"] = spec.active_deadline_seconds

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(spec.run_id),
            "namespace": spec.namespace,
            "labels": spec.labels(),
            "annotations": spec.annotations(),
        },
        "spec": job_spec,
    }


@dataclass
class JobStatus:
    name: str
    phase: str                       # pending | active | succeeded | failed | absent
    active: int = 0
    succeeded: int = 0
    failed: int = 0
    reason: str = ""                 # e.g. DeadlineExceeded, BackoffLimitExceeded
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.phase in (SUCCEEDED, FAILED)


def classify_job_status(job_obj: dict[str, Any]) -> JobStatus:
    """Map a Job object (from ``kubectl get job -o json``) to a stable phase. A Complete
    condition → succeeded; a Failed condition → failed (carrying its reason/message, e.g.
    DeadlineExceeded for a timeout); otherwise active vs pending by the active count."""
    meta = job_obj.get("metadata", {}) or {}
    status = job_obj.get("status", {}) or {}
    conditions = status.get("conditions", []) or []
    active = int(status.get("active", 0) or 0)
    succeeded = int(status.get("succeeded", 0) or 0)
    failed = int(status.get("failed", 0) or 0)

    def _cond(kind: str) -> dict[str, Any] | None:
        for c in conditions:
            if c.get("type") == kind and str(c.get("status")) == "True":
                return c
        return None

    name = meta.get("name", "")
    if _cond("Complete") or (succeeded > 0 and active == 0 and not _cond("Failed")):
        return JobStatus(name=name, phase=SUCCEEDED, active=active, succeeded=succeeded,
                         failed=failed, raw=job_obj)
    failed_cond = _cond("Failed")
    if failed_cond:
        return JobStatus(name=name, phase=FAILED, active=active, succeeded=succeeded, failed=failed,
                         reason=failed_cond.get("reason", ""), message=failed_cond.get("message", ""),
                         raw=job_obj)
    if active > 0:
        return JobStatus(name=name, phase=ACTIVE, active=active, succeeded=succeeded, failed=failed, raw=job_obj)
    return JobStatus(name=name, phase=PENDING, active=active, succeeded=succeeded, failed=failed, raw=job_obj)
