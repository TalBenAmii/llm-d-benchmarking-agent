"""Fault classification for a failed benchmark Job.

Pure functions over the Job + its pods' JSON. Maps the raw Kubernetes failure surface (pod
phases, container terminated/waiting reasons, scheduling conditions) into a small, stable
fault *kind* the agent can reason about. We report FACTS only (kind + which pod/container +
exit code + the cluster's own message); how to remediate is the agent's job, guided by
knowledge files — keeping judgment out of Python.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Fault kinds (stable, agent/UI-facing), in classification-priority order.
TIMEOUT = "timeout"            # Job activeDeadlineSeconds exceeded
OOM = "oom"                    # a container was OOMKilled
UNSCHEDULABLE = "unschedulable"  # pod couldn't be scheduled (insufficient CPU/mem, etc.)
EVICTED = "evicted"           # node resource pressure evicted the pod
IMAGE_ERROR = "image_error"   # image pull / config error
RUN_ERROR = "run_error"       # container ran and exited non-zero
UNKNOWN = "unknown"
NONE = "none"

_IMAGE_WAIT_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName",
                       "CreateContainerConfigError", "CreateContainerError"}


@dataclass
class Failure:
    kind: str
    message: str = ""
    pod: str = ""
    container: str = ""
    exit_code: int | None = None

    @property
    def is_failure(self) -> bool:
        return self.kind != NONE


def _container_statuses(pod: dict[str, Any]) -> list[dict[str, Any]]:
    # Defensive: a truthy non-list ``containerStatuses`` (the ``or []`` fallback only catches
    # falsy) would otherwise be iterated element-by-element and crash ``.get`` on a non-dict —
    # honoring the "classification never crashes" invariant (cf. job.py::_as_int / BUG-023).
    cs = (pod.get("status", {}) or {}).get("containerStatuses", []) or []
    return [c for c in cs if isinstance(c, dict)] if isinstance(cs, list) else []


def _scan_oom(pods: list[dict]) -> Failure | None:
    for pod in pods:
        for cs in _container_statuses(pod):
            for key in ("state", "lastState"):
                term = (cs.get(key, {}) or {}).get("terminated", {}) or {}
                if term.get("reason") == "OOMKilled":
                    return Failure(OOM, message="container OOMKilled (out of memory)",
                                   pod=pod.get("metadata", {}).get("name", ""),
                                   container=cs.get("name", ""),
                                   exit_code=term.get("exitCode"))
    return None


def _scan_unschedulable(pods: list[dict]) -> Failure | None:
    for pod in pods:
        conds = (pod.get("status", {}) or {}).get("conditions", []) or []
        if not isinstance(conds, list):
            continue                                    # a non-list `conditions` isn't iterable as gates
        for cond in conds:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") == "PodScheduled" and str(cond.get("status")) == "False" \
                    and cond.get("reason") == "Unschedulable":
                return Failure(UNSCHEDULABLE, message=cond.get("message", "pod is unschedulable"),
                               pod=pod.get("metadata", {}).get("name", ""))
    return None


def _scan_evicted(pods: list[dict]) -> Failure | None:
    for pod in pods:
        st = pod.get("status", {}) or {}
        if st.get("reason") == "Evicted":
            return Failure(EVICTED, message=st.get("message", "pod evicted under node pressure"),
                           pod=pod.get("metadata", {}).get("name", ""))
    return None


def _scan_image_error(pods: list[dict]) -> Failure | None:
    for pod in pods:
        for cs in _container_statuses(pod):
            wait = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            if wait.get("reason") in _IMAGE_WAIT_REASONS:
                return Failure(IMAGE_ERROR, message=wait.get("message", wait.get("reason", "image error")),
                               pod=pod.get("metadata", {}).get("name", ""), container=cs.get("name", ""))
    return None


def _scan_run_error(pods: list[dict]) -> Failure | None:
    for pod in pods:
        for cs in _container_statuses(pod):
            term = (cs.get("state", {}) or {}).get("terminated", {}) or {}
            code = term.get("exitCode")
            if term and code not in (0, None):
                return Failure(RUN_ERROR, message=term.get("reason", "container exited non-zero"),
                               pod=pod.get("metadata", {}).get("name", ""),
                               container=cs.get("name", ""), exit_code=code)
    return None


def classify_failure(job_status, pods: list[dict[str, Any]]) -> Failure:
    """Classify why a (failed) Job failed. ``job_status`` is a
    :class:`~app.orchestrator.job.JobStatus`; ``pods`` are the Job's pods (``get pods -l
    run-id=<id> -o json``). Returns ``Failure(kind=NONE)`` if nothing indicates a failure.

    Priority: a Job-level DeadlineExceeded (timeout) wins; then OOM, unschedulable, eviction,
    image errors, and finally a non-zero container exit. This order surfaces the most
    actionable root cause when several signals coexist (e.g. an OOMKill that also yields a
    non-zero exit reports as OOM)."""
    if getattr(job_status, "reason", "") == "DeadlineExceeded":
        return Failure(TIMEOUT, message="benchmark exceeded its activeDeadlineSeconds")

    # Drop any non-dict pod element so a malformed/forged ``get pods -o json`` items list can't
    # crash a scanner's ``pod.get(...)`` — classification degrades to UNKNOWN, never raises.
    pods = [p for p in pods if isinstance(p, dict)]
    for scan in (_scan_oom, _scan_unschedulable, _scan_evicted, _scan_image_error, _scan_run_error):
        found = scan(pods)
        if found is not None:
            return found

    # The Job reports failed but pods give no specific signal.
    if getattr(job_status, "phase", "") == "failed":
        return Failure(UNKNOWN, message=getattr(job_status, "message", "") or "job failed without a clear pod-level cause")
    return Failure(NONE)
