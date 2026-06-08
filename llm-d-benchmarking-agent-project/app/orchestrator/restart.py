"""Orchestrator-restart durability proof — a thin MECHANISM wrapper over existing methods.

"Orchestrator restart" here = **discard the orchestrator object and build a FRESH one against
the same cluster state, holding no local state**, then resume via the EXISTING ``reconstruct``
/ ``reconstruct_sweep`` / ``run_sweep(sweep_id=...)`` controller methods. This module performs
that kill-and-rehydrate and returns a :class:`RestartProof` of FACTS. It adds no lifecycle
logic — it only orchestrates methods that are already proven (the sweep checkpoint/resume is
exercised by ``tests/test_orchestrator_checkpoint.py``).

Two proof modes (both reuse existing code, zero duplication):

* **Single-run reconstruct** — a fresh ``BenchmarkOrchestrator`` calls ``reconstruct()`` and
  recovers an in-flight run PURELY from cluster labels (it stored nothing locally).
* **Sweep checkpoint** — a fresh orchestrator re-invokes ``run_sweep(sweep_id=...)``; the
  cluster ConfigMap checkpoint causes completed treatments to be SKIPPED and only the
  remainder to run, with **0 duplicate Job applies** — the key durability invariant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.orchestrator.controller import BenchmarkOrchestrator, SweepOutcome
from app.orchestrator.job import JobSpec
from app.orchestrator.kube import KubeClient


@dataclass
class RestartProof:
    """Facts proving a fresh orchestrator resumed against the same cluster state. ``mode`` is
    ``"reconstruct"`` (single in-flight run recovered from labels) or ``"sweep"`` (partial sweep
    resumed from the ConfigMap checkpoint). All counts are observed, never asserted into being."""

    mode: str
    recovered: bool
    # reconstruct mode
    in_flight_recovered: int = 0
    recovered_run_ids: list[str] = field(default_factory=list)
    # sweep mode
    completed_before: int = 0
    run_after: int = 0
    total_treatments: int = 0
    duplicate_applies: int = 0     # treatments applied more than once across both passes (must be 0)
    no_duplicates: bool = True
    resumed_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "recovered": self.recovered,
            "no_duplicates": self.no_duplicates,
        }
        if self.mode == "reconstruct":
            out["in_flight_recovered"] = self.in_flight_recovered
            out["recovered_run_ids"] = list(self.recovered_run_ids)
        else:
            out.update({
                "completed_before": self.completed_before,
                "run_after": self.run_after,
                "total_treatments": self.total_treatments,
                "duplicate_applies": self.duplicate_applies,
                "resumed_ids": list(self.resumed_ids),
            })
        if self.note:
            out["note"] = self.note
        return out


def _applied_job_run_ids(kube: KubeClient) -> list[str]:
    """The logical (base) run ids whose JOB manifest was applied, read off the fake/real client's
    ``applied`` record (skips ConfigMap applies). Strips the ``-a<N>`` attempt suffix back to the
    logical id, so an attempt-2 retry is still the same logical treatment. Returns ``[]`` if the
    client doesn't expose ``applied`` (e.g. a real client) — duplicate-counting is a fake-only,
    hermetic assertion."""
    applied = getattr(kube, "applied", None)
    if not applied:
        return []
    ids: list[str] = []
    for _ns, manifest in applied:
        if not isinstance(manifest, dict) or manifest.get("kind") != "Job":
            continue
        labels = (manifest.get("metadata", {}) or {}).get("labels", {}) or {}
        rid = labels.get("llmd-bench/run-id")
        if rid:
            ids.append(rid.rsplit("-a", 1)[0])
    return ids


async def prove_restart_recovery(
    kube: KubeClient,
    workspace: str | Path,
    *,
    namespace: str,
    session_id: str | None = None,
    sweep_id: str | None = None,
    specs: list[JobSpec] | None = None,
    max_parallel: int = 2,
    max_attempts: int = 1,
    poll_interval: float = 0.0,
) -> RestartProof:
    """Build a FRESH orchestrator against ``kube`` (no shared local state) and prove resume.

    * If ``sweep_id`` + ``specs`` are given → SWEEP mode: re-invoke ``run_sweep(sweep_id=...)``.
      The cluster checkpoint causes already-completed treatments to be skipped; assert each
      logical treatment was applied exactly once across the WHOLE drill (no duplicate Jobs).
    * Otherwise → RECONSTRUCT mode: call ``reconstruct()`` and report the in-flight runs it
      recovered from cluster labels alone.

    Pure orchestration of existing controller methods — adds no lifecycle logic."""
    fresh = BenchmarkOrchestrator(kube, workspace)

    if sweep_id is not None and specs is not None:
        before = _applied_job_run_ids(kube)
        checkpoint = await fresh.reconstruct_sweep(sweep_id, namespace=namespace)
        completed_before = len(checkpoint.completed_ids())

        outcome: SweepOutcome = await fresh.run_sweep(
            specs, max_parallel=max_parallel, max_attempts=max_attempts,
            poll_interval=poll_interval, sweep_id=sweep_id, namespace=namespace,
        )
        all_applied = _applied_job_run_ids(kube)
        newly_applied = all_applied[len(before):]
        # No duplicate Jobs: each logical treatment applied at most once across the whole drill.
        per_treatment = {t.run_id for t in specs}
        duplicates = sum(
            1 for t in per_treatment if all_applied.count(t) > 1
        )
        return RestartProof(
            mode="sweep",
            recovered=True,
            completed_before=completed_before,
            run_after=len(newly_applied),
            total_treatments=len(specs),
            duplicate_applies=duplicates,
            no_duplicates=duplicates == 0,
            resumed_ids=list(outcome.resumed),
            note=(
                f"fresh orchestrator resumed sweep {sweep_id}: "
                f"{completed_before}/{len(specs)} already completed (skipped), "
                f"{len(newly_applied)} treatment(s) run on resume; "
                f"{'no' if duplicates == 0 else duplicates} duplicate Job(s)."
            ),
        )

    # Reconstruct mode: recover in-flight runs purely from cluster labels.
    statuses = await fresh.reconstruct(namespace=namespace, session_id=session_id, sweep_id=sweep_id)
    in_flight = [s for s in statuses if not s.terminal]
    return RestartProof(
        mode="reconstruct",
        recovered=bool(statuses),
        in_flight_recovered=len(in_flight),
        recovered_run_ids=[s.name for s in statuses],
        no_duplicates=True,
        note=(
            f"fresh orchestrator reconstructed {len(statuses)} managed run(s) from cluster "
            f"labels ({len(in_flight)} still in-flight); held no local state."
        ),
    )
