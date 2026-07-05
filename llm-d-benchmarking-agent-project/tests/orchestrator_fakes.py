"""In-memory fakes for orchestrator tests — a FakeKubeClient mirroring the KubeClient
interface, plus builders for Job/Pod JSON. Lets the whole Job lifecycle (submit, watch
through state transitions, fault classification, reconstruction, cleanup) run hermetically
with no cluster — the same philosophy as the CaptureRunner.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
        # Phase 22: in-cluster ConfigMaps, keyed by (namespace, name) — the source of truth for
        # a DOE sweep's checkpoint. `apply` of a ConfigMap manifest upserts here (create-or-
        # update), and `list_configmaps` selects them by label, so a checkpoint round-trips
        # through the fake exactly as it would through the real `kubectl apply`/`get`.
        self._configmaps: dict[tuple[str, str], dict[str, Any]] = {}
        # Observability for tests: how many times a ConfigMap was applied (checkpoint writes).
        self.configmap_writes = 0
        # Phase 21: programmable live log streams, keyed by run-id. Each entry is a sequence of
        # lines yielded by `stream_log_lines`, with a small per-line delay so the tail interleaves
        # with the watch loop rather than dumping all lines before the first poll. Optional knobs:
        #   stream_line_delay  — seconds slept between yielded lines (default 0)
        #   stream_raises       — run-ids whose stream raises mid-way (tests fault isolation)
        self.log_streams: dict[str, list[str]] = {}
        self.stream_line_delay = 0.0
        self.stream_raises: set[str] = set()
        self.stream_started: list[str] = []   # run-ids whose tail was started (observability)
        # Optional concurrency probe: if apply_gate is an unset asyncio.Event, apply() blocks
        # on it, so a test can observe how many applies run at once (the sweep's cap).
        self.apply_gate = None
        self.apply_active = 0
        self.apply_peak = 0

    def program(self, run_id: str, *, namespace: str = "bench", phases: list[str] | None = None,
                jobs: list[dict] | None = None, labels: dict | None = None,
                pods: list[dict] | None = None, logs: str | None = None,
                log_lines: list[str] | None = None, reason: str = "") -> None:
        snaps = jobs if jobs is not None else [
            make_job(run_id, p, namespace=namespace, labels=labels, reason=reason) for p in (phases or [])
        ]
        self._runs[(namespace, run_id)] = {"snapshots": snaps, "cursor": 0}
        if pods is not None:
            self._pods[(namespace, run_id)] = pods
        if logs is not None:
            self.logs_text[run_id] = logs
        if log_lines is not None:
            self.log_streams[run_id] = list(log_lines)

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
        # A ConfigMap apply upserts into the in-memory CM store (create-or-update), mirroring
        # `kubectl apply`. This is how a sweep's checkpoint is persisted to the cluster.
        if manifest.get("kind") == "ConfigMap":
            name = manifest.get("metadata", {}).get("name", "")
            self._configmaps[(namespace, name)] = manifest
            self.configmap_writes += 1
            return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "apply"], cwd=None)
        rid = (manifest.get("metadata", {}).get("labels", {}) or {}).get(LABEL_RUN)
        key = (namespace, rid)
        if rid and key not in self._runs:
            snap = dict(manifest)
            snap["status"] = {"active": 1}
            self._runs[key] = {"snapshots": [snap], "cursor": 0}
        return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "apply"], cwd=None)

    async def list_configmaps(self, *, namespace: str, selector: str | None = None) -> list[dict]:
        sel = _parse_selector(selector)
        out: list[dict] = []
        for (ns, _name), cm in self._configmaps.items():
            if ns != namespace:
                continue
            labels = cm.get("metadata", {}).get("labels", {}) or {}
            if _matches(labels, sel):
                out.append(cm)
        return out

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

    async def stream_log_lines(self, *, namespace: str, selector: str,
                               tail=None) -> AsyncIterator[str]:
        """Yield the run's programmed log lines one at a time (mirrors the real `kubectl logs -f`
        async generator). A small per-line delay lets the lines interleave with the watch loop;
        a run-id in ``stream_raises`` raises mid-stream so a test can assert the run survives a
        failing tail. The async generator's ``finally`` is exercised on the orchestrator's
        terminal-state cancellation, just like the real bridge."""
        rid = _parse_selector(selector).get(LABEL_RUN, "")
        self.stream_started.append(rid)
        for idx, line in enumerate(self.log_streams.get(rid, [])):
            if rid in self.stream_raises and idx > 0:
                raise RuntimeError(f"simulated log-stream failure for {rid}")
            if self.stream_line_delay:
                await asyncio.sleep(self.stream_line_delay)
            yield line

    async def delete_job(self, name: str, *, namespace: str) -> RunResult:
        self.deleted.append((namespace, name))
        for key in list(self._runs):
            if key[0] == namespace and job_name(key[1]) == name:
                del self._runs[key]
        return RunResult(exit_code=0, duration_s=0.0, real_argv=["kubectl", "delete", "job", name], cwd=None)
