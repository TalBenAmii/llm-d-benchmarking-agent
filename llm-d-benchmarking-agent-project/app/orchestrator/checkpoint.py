"""DOE sweep checkpoint — the cluster IS the source of truth for sweep progress.

A long DOE sweep can be interrupted (the orchestrator restarts, the chat session drops, the
host is rebooted) part-way through its N treatments. Consistent with the orchestrator's
*stateless design* (proposal §3.3/§4 — reconstruct state from the cluster, store nothing
locally), this module persists which treatments are **completed** (with their outcome) and
which are **in-flight** to a Kubernetes **ConfigMap** named for the sweep. On a resume, the
sweep reads that ConfigMap, SKIPS the already-completed treatments, runs only the remainder,
and merges the prior outcomes into the final roll-up.

This is **mechanism only**:

* The store is a thin read/serialize/write over a :class:`~app.orchestrator.kube.KubeClient`
  (the same allowlisted ``kubectl`` surface as the Job lifecycle). It holds NO local
  source-of-truth and NO judgment — *which* treatments to run, retry budgets, parallelism are
  the agent's / controller's decisions.
* The ConfigMap is the single source of truth. It is labelled ``managed-by`` + the sweep
  label so it is selectable, reconstructable, and reaped by the existing cleanup path.
* Resume is idempotent: re-running a sweep with the SAME ``sweep_id`` reads the same
  ConfigMap and continues; a completed treatment is never re-run.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from app.orchestrator.job import (
    LABEL_MANAGED,
    LABEL_SWEEP,
    MANAGED_BY,
    validate_job_name,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.orchestrator.kube import KubeClient

# Treatment progress states recorded in the checkpoint.
IN_FLIGHT = "in_flight"   # the orchestrator has started this treatment but not finished it
COMPLETED = "completed"   # the treatment reached a terminal outcome (success or dead-letter)

# The ConfigMap data key under which the JSON progress document is stored. A ConfigMap's
# `data` values must be strings, so the whole progress map is serialized into one JSON blob.
_PROGRESS_KEY = "progress.json"
# Schema version of the persisted document, so a future format change is detectable rather
# than silently mis-parsed.
_SCHEMA_VERSION = 1


def checkpoint_name(sweep_id: str) -> str:
    """The ConfigMap name carrying a sweep's checkpoint. DNS-1123 validated (it shares the
    Job-name budget/charset) so a malformed sweep id fails loudly here, not opaquely at
    ``kubectl apply`` time."""
    name = f"llmd-bench-sweep-{sweep_id}"
    validate_job_name(name)
    return name


@dataclass
class TreatmentRecord:
    """One treatment's checkpointed progress. ``run_id`` is the logical treatment id (the base
    id shared across attempts, e.g. ``t3``), matching ``RunOutcome.run_id`` and the treatment's
    ``JobSpec.run_id`` so a resume can correlate a record to a spec by exact id."""

    run_id: str
    state: str                          # IN_FLIGHT | COMPLETED
    succeeded: bool = False
    dead_lettered: bool = False
    fault_kind: str | None = None       # the terminal fault kind, if dead-lettered

    @property
    def completed(self) -> bool:
        return self.state == COMPLETED


@dataclass
class SweepCheckpoint:
    """The in-memory view of a sweep's persisted progress (loaded from / written to the
    cluster ConfigMap). Pure value object — no I/O."""

    sweep_id: str
    treatments: dict[str, TreatmentRecord] = field(default_factory=dict)

    def is_completed(self, run_id: str) -> bool:
        """True iff this treatment is recorded as COMPLETED (so a resume must SKIP it)."""
        rec = self.treatments.get(run_id)
        return rec is not None and rec.completed

    def completed_ids(self) -> set[str]:
        return {rid for rid, rec in self.treatments.items() if rec.completed}

    def record_in_flight(self, run_id: str) -> None:
        # Never downgrade a COMPLETED record back to in-flight (idempotent on re-entry).
        rec = self.treatments.get(run_id)
        if rec is not None and rec.completed:
            return
        self.treatments[run_id] = TreatmentRecord(run_id=run_id, state=IN_FLIGHT)

    def record_completed(self, run_id: str, *, succeeded: bool, dead_lettered: bool,
                         fault_kind: str | None) -> None:
        self.treatments[run_id] = TreatmentRecord(
            run_id=run_id, state=COMPLETED, succeeded=succeeded,
            dead_lettered=dead_lettered, fault_kind=fault_kind,
        )

    # ---- serialization (ConfigMap data is string->string, so we JSON the document) -------

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA_VERSION,
            "sweep_id": self.sweep_id,
            "treatments": [asdict(rec) for rec in self.treatments.values()],
        }

    @classmethod
    def from_document(cls, sweep_id: str, doc: dict[str, Any]) -> SweepCheckpoint:
        treatments: dict[str, TreatmentRecord] = {}
        for raw in doc.get("treatments", []) or []:
            if not isinstance(raw, dict):
                continue
            rid = raw.get("run_id")
            state = raw.get("state")
            if not isinstance(rid, str) or state not in (IN_FLIGHT, COMPLETED):
                continue
            treatments[rid] = TreatmentRecord(
                run_id=rid,
                state=state,
                succeeded=bool(raw.get("succeeded", False)),
                dead_lettered=bool(raw.get("dead_lettered", False)),
                fault_kind=raw.get("fault_kind"),
            )
        return cls(sweep_id=sweep_id, treatments=treatments)


def build_configmap_manifest(sweep_id: str, checkpoint: SweepCheckpoint, *,
                             namespace: str) -> dict[str, Any]:
    """Render the checkpoint into a Kubernetes ConfigMap manifest (a plain dict, ready to
    YAML-dump and ``kubectl apply``). Labelled ``managed-by`` + the sweep label so it is
    selectable, reconstructable, and reaped by the existing cleanup path. Pure function."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": checkpoint_name(sweep_id),
            "namespace": namespace,
            "labels": {LABEL_MANAGED: MANAGED_BY, LABEL_SWEEP: sweep_id},
        },
        "data": {
            _PROGRESS_KEY: json.dumps(checkpoint.to_document(), sort_keys=True),
        },
    }


def parse_checkpoint(sweep_id: str, configmap: dict[str, Any] | None) -> SweepCheckpoint:
    """Parse a ConfigMap object (as ``kubectl get configmap -o json`` returns it) back into a
    :class:`SweepCheckpoint`. A missing ConfigMap, missing/blank data key, or unparseable JSON
    yields an EMPTY checkpoint (a fresh sweep), never an error — so a first run and a corrupt
    checkpoint both degrade to "run everything", and the source of truth is rebuilt cleanly."""
    if not configmap:
        return SweepCheckpoint(sweep_id=sweep_id)
    data = configmap.get("data") or {}
    blob = data.get(_PROGRESS_KEY)
    if not isinstance(blob, str) or not blob.strip():
        return SweepCheckpoint(sweep_id=sweep_id)
    try:
        doc = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return SweepCheckpoint(sweep_id=sweep_id)
    if not isinstance(doc, dict):
        return SweepCheckpoint(sweep_id=sweep_id)
    return SweepCheckpoint.from_document(sweep_id, doc)


class CheckpointStore:
    """Reads/writes a sweep's checkpoint ConfigMap through a :class:`KubeClient`. The cluster
    is the source of truth — this store keeps no authoritative local copy. ``write`` renders
    the manifest into the workspace and ``kubectl apply``s it (create-or-update, so repeated
    writes are idempotent); ``load`` selects the ConfigMap by the sweep label and parses it."""

    def __init__(self, kube: KubeClient, workspace: Any):
        from pathlib import Path
        self._kube = kube
        self._workspace = Path(workspace)

    def _selector(self, sweep_id: str) -> str:
        # Select THIS sweep's checkpoint: managed-by + the sweep label (a name match would
        # need a name positional the read-only `get` doesn't allow; the label is exact).
        return f"{LABEL_MANAGED}={MANAGED_BY},{LABEL_SWEEP}={sweep_id}"

    async def load(self, sweep_id: str, *, namespace: str) -> SweepCheckpoint:
        """Read the sweep's checkpoint from the cluster. Returns an empty checkpoint if none
        exists yet (a fresh sweep)."""
        cms = await self._kube.list_configmaps(namespace=namespace, selector=self._selector(sweep_id))
        return parse_checkpoint(sweep_id, cms[0] if cms else None)

    async def write(self, checkpoint: SweepCheckpoint, *, namespace: str) -> None:
        """Persist the checkpoint to the cluster ConfigMap (create-or-update via apply). The
        manifest is written into the workspace (the same confinement as Job manifests) so the
        allowlisted ``kubectl apply -f`` accepts it."""
        manifest = build_configmap_manifest(checkpoint.sweep_id, checkpoint, namespace=namespace)
        path = self._workspace / "sweeps" / f"{checkpoint.sweep_id}.checkpoint.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        import yaml
        path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        await self._kube.apply(path, namespace=namespace)
