"""In-memory fakes for orchestrator tests — a FakeKubeClient mirroring the KubeClient
interface, plus builders for Job/Pod JSON. Lets the whole Job lifecycle (submit, watch
through state transitions, fault classification, reconstruction, cleanup) run hermetically
with no cluster — the same philosophy as the CaptureRunner.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.orchestrator.job import (
    ANNO_ATTEMPT,
    LABEL_MANAGED,
    LABEL_RUN,
    MANAGED_BY,
    job_name,
)
from app.security.runner import RunResult


def make_job(run_id: str, phase: str, *, namespace: str = "bench", labels: dict | None = None,
             reason: str = "", message: str = "") -> dict[str, Any]:
    """Build a Job object (as `kubectl get job -o json` would return) for a given phase."""
    lbl = {LABEL_MANAGED: MANAGED_BY, LABEL_RUN: run_id}
    if labels:
        lbl.update(labels)
    status: dict[str, Any] = {}
    if phase == "active":
        status = {"active": 1}
    elif phase == "succeeded":
        status = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    elif phase == "failed":
        status = {"failed": 1, "conditions": [
            {"type": "Failed", "status": "True", "reason": reason or "BackoffLimitExceeded",
             "message": message}]}
    # "pending" -> empty status
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job_name(run_id), "namespace": namespace, "labels": lbl,
                     "annotations": {ANNO_ATTEMPT: "1"}},
        "spec": {"backoffLimit": 0},
        "status": status,
    }


def make_pod(run_id: str, *, phase: str = "Running", namespace: str = "bench",
             waiting: str | None = None, terminated: str | None = None,
             exit_code: int | None = None, reason: str = "") -> dict[str, Any]:
    """Build a Pod object. ``waiting``/``terminated`` set a container state reason
    (e.g. OOMKilled, Error); ``reason`` sets the pod-level status reason (e.g. Evicted)."""
    cstate: dict[str, Any] = {}
    if waiting:
        cstate = {"waiting": {"reason": waiting}}
    elif terminated:
        t = {"reason": terminated}
        if exit_code is not None:
            t["exitCode"] = exit_code
        cstate = {"terminated": t}
    pod: dict[str, Any] = {
        "metadata": {"name": f"{job_name(run_id)}-xyz", "namespace": namespace,
                     "labels": {LABEL_RUN: run_id, "job-name": job_name(run_id)}},
        "status": {"phase": phase,
                   "containerStatuses": [{"name": "benchmark", "state": cstate}] if cstate else []},
    }
    if reason:
        pod["status"]["reason"] = reason
    return pod


def _parse_selector(selector: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (selector or "").split(","):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            out[k] = v
    return out


def _matches(labels: dict[str, str], sel: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in sel.items())


class FakeKubeClient:
    """Programmable, in-memory KubeClient. ``program`` registers a run with a SEQUENCE of
    job snapshots; each ``list_jobs`` that selects a run advances it one step (clamped), so a
    watch loop observes the programmed progression. ``apply`` auto-registers a run from the
    manifest if not pre-programmed."""

    def __init__(self):
        self.applied: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        self.logs_text: dict[str, str] = {}
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}
        self._pods: dict[tuple[str, str], list[dict]] = {}
        # Optional concurrency probe: if apply_gate is an unset asyncio.Event, apply() blocks
        # on it, so a test can observe how many applies run at once (the sweep's cap).
        self.apply_gate = None
        self.apply_active = 0
        self.apply_peak = 0

    def program(self, run_id: str, *, namespace: str = "bench", phases: list[str] | None = None,
                jobs: list[dict] | None = None, labels: dict | None = None,
                pods: list[dict] | None = None, logs: str | None = None,
                reason: str = "") -> None:
        snaps = jobs if jobs is not None else [
            make_job(run_id, p, namespace=namespace, labels=labels, reason=reason) for p in (phases or [])
        ]
        self._runs[(namespace, run_id)] = {"snapshots": snaps, "cursor": 0}
        if pods is not None:
            self._pods[(namespace, run_id)] = pods
        if logs is not None:
            self.logs_text[run_id] = logs

    async def apply(self, manifest_path, *, namespace: str) -> RunResult:
        manifest = yaml.safe_load(Path(manifest_path).read_text())
        self.applied.append((namespace, manifest))
        self.apply_active += 1
        self.apply_peak = max(self.apply_peak, self.apply_active)
        try:
            if self.apply_gate is not None:
                await self.apply_gate.wait()
        finally:
            self.apply_active -= 1
        rid = (manifest.get("metadata", {}).get("labels", {}) or {}).get(LABEL_RUN)
        key = (namespace, rid)
        if rid and key not in self._runs:
            snap = dict(manifest)
            snap["status"] = {"active": 1}
            self._runs[key] = {"snapshots": [snap], "cursor": 0}
        return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "apply"], cwd=None)

    async def list_jobs(self, *, namespace: str, selector: str | None = None) -> list[dict]:
        sel = _parse_selector(selector)
        out: list[dict] = []
        for (ns, _rid), rec in self._runs.items():
            if ns != namespace:
                continue
            snaps = rec["snapshots"]
            snap = snaps[min(rec["cursor"], len(snaps) - 1)]
            labels = snap.get("metadata", {}).get("labels", {})
            if _matches(labels, sel):
                out.append(snap)
                if rec["cursor"] < len(snaps) - 1:
                    rec["cursor"] += 1
        return out

    async def list_pods(self, *, namespace: str, selector: str | None = None) -> list[dict]:
        sel = _parse_selector(selector)
        out: list[dict] = []
        for (ns, _rid), pods in self._pods.items():
            if ns != namespace:
                continue
            for p in pods:
                if _matches(p.get("metadata", {}).get("labels", {}), sel):
                    out.append(p)
        return out

    async def logs(self, *, namespace: str, selector: str, tail=None, follow: bool = False) -> str:
        rid = _parse_selector(selector).get(LABEL_RUN, "")
        return self.logs_text.get(rid, "")

    async def delete_job(self, name: str, *, namespace: str, ignore_not_found: bool = True) -> RunResult:
        self.deleted.append((namespace, name))
        for key in list(self._runs):
            if key[0] == namespace and job_name(key[1]) == name:
                del self._runs[key]
        return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "delete", "job", name], cwd=None)
