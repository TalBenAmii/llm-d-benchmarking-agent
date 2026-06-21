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

import copy
import re
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

# The conventional extended-resource name a GPU device-plugin advertises. The agent MAY
# override it (e.g. ``amd.com/gpu``, ``habana.ai/gaudi``) at plan time — which accelerator a
# scenario needs is JUDGMENT (knowledge/resource_management.md), not a Python branch.
DEFAULT_GPU_RESOURCE = "nvidia.com/gpu"


@dataclass
class Scheduling:
    """Optional Kubernetes scheduling intent for a benchmark Job — pure data the agent
    supplies at plan time (informed by ``knowledge/resource_management.md``). This object is
    **mechanism only**: it carries the operator's choices and renders them into the right Job
    manifest paths. It holds NO placement judgment (which GPU, which node, how to avoid the
    measured stack) — that decision is the agent's, expressed as these fields.

    Fields (all optional; an empty ``Scheduling`` renders to nothing):
      * ``node_selector`` — exact node-label match (``spec.template.spec.nodeSelector``).
      * ``tolerations`` — tolerate node taints (e.g. a dedicated GPU pool taint).
      * ``affinity`` — a raw, agent-supplied ``affinity`` block, merged verbatim (full power:
        node affinity, pod affinity/anti-affinity). Use this for anything ``avoid_labels``
        can't express.
      * ``gpu_count`` / ``gpu_resource`` — request N GPUs of an extended resource on both
        ``requests`` and ``limits`` (Kubernetes requires extended resources to match).
      * ``gpu_type_label`` — a ``(key, value)`` node-label selector pinning the GPU TYPE
        (e.g. ``("nvidia.com/gpu.product", "NVIDIA-A100-SXM4-80GB")``); merged into
        ``nodeSelector``.
      * ``avoid_labels`` — a convenience that mechanically renders **pod anti-affinity** so the
        benchmark pod is NOT scheduled onto a node already running a pod carrying these labels
        (e.g. the llm-d stack being measured: ``{"llm-d.ai/role": "decode"}``). This is the
        anti-starvation lever from proposal §4 — keep the load generator off the nodes serving
        the system under test so the measurement isn't self-contended.
    """

    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: list[dict[str, Any]] = field(default_factory=list)
    affinity: dict[str, Any] = field(default_factory=dict)
    gpu_count: int | None = None
    gpu_resource: str = DEFAULT_GPU_RESOURCE
    gpu_type_label: tuple[str, str] | None = None
    avoid_labels: dict[str, str] = field(default_factory=dict)
    avoid_topology_key: str = "kubernetes.io/hostname"

    def is_empty(self) -> bool:
        return not (
            self.node_selector
            or self.tolerations
            or self.affinity
            or self.gpu_count
            or self.gpu_type_label
            or self.avoid_labels
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Scheduling | None:
        """Parse agent-supplied scheduling intent (the tool's ``scheduling`` arg) into a
        :class:`Scheduling`. PURE PARSING + shape validation — it accepts/rejects on TYPE, not
        on policy (no "is this a good placement?" logic; that judgment is the agent's). Returns
        ``None`` for ``None``/empty so the manifest is byte-for-byte the baseline when omitted.
        Unknown keys are rejected so a typo'd field can't silently no-op."""
        if not data:
            return None
        allowed = {
            "node_selector", "tolerations", "affinity", "gpu_count", "gpu_resource",
            "gpu_type_label", "avoid_labels", "avoid_topology_key",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown scheduling field(s): {sorted(unknown)}; allowed: {sorted(allowed)}")

        node_selector = _as_str_map(data.get("node_selector"), "node_selector")
        avoid_labels = _as_str_map(data.get("avoid_labels"), "avoid_labels")
        tolerations = _as_dict_list(data.get("tolerations"), "tolerations")
        affinity = data.get("affinity") or {}
        if not isinstance(affinity, dict):
            raise ValueError("scheduling.affinity must be a mapping (a Kubernetes affinity block)")

        gpu_count_raw = data.get("gpu_count")
        gpu_count: int | None = None
        if gpu_count_raw is not None:
            if isinstance(gpu_count_raw, bool) or not isinstance(gpu_count_raw, int):
                raise ValueError("scheduling.gpu_count must be an integer")
            if gpu_count_raw < 1:
                raise ValueError("scheduling.gpu_count must be >= 1 (omit it to request no GPU)")
            gpu_count = gpu_count_raw

        # Present-but-invalid is rejected; absent falls back to the default (so a typo'd empty
        # string can't silently no-op into the default).
        gpu_resource = DEFAULT_GPU_RESOURCE
        if "gpu_resource" in data:
            gpu_resource = data["gpu_resource"]
            if not isinstance(gpu_resource, str) or not gpu_resource:
                raise ValueError("scheduling.gpu_resource must be a non-empty string")

        gpu_type_label = _as_pair(data.get("gpu_type_label"), "gpu_type_label")
        topo = "kubernetes.io/hostname"
        if "avoid_topology_key" in data:
            topo = data["avoid_topology_key"]
            if not isinstance(topo, str) or not topo:
                raise ValueError("scheduling.avoid_topology_key must be a non-empty string")

        return cls(
            node_selector=node_selector,
            tolerations=tolerations,
            affinity=copy.deepcopy(affinity),
            gpu_count=gpu_count,
            gpu_resource=gpu_resource,
            gpu_type_label=gpu_type_label,
            avoid_labels=avoid_labels,
            avoid_topology_key=topo,
        )

    def node_selector_map(self) -> dict[str, str]:
        """The effective nodeSelector = explicit labels + the GPU-type pin (if any)."""
        out = dict(self.node_selector)
        if self.gpu_type_label is not None:
            out[self.gpu_type_label[0]] = self.gpu_type_label[1]
        return out

    def gpu_quantity(self) -> str | None:
        """The GPU resource quantity to add to requests AND limits, or ``None`` for no GPU."""
        return str(self.gpu_count) if self.gpu_count else None

    def effective_affinity(self) -> dict[str, Any]:
        """The merged affinity block: the agent's raw ``affinity`` plus a podAntiAffinity term
        synthesized from ``avoid_labels`` (so the benchmark pod avoids nodes already running the
        measured stack). Returns ``{}`` when neither is set. Mechanical assembly only."""
        affinity: dict[str, Any] = copy.deepcopy(self.affinity)
        if self.avoid_labels:
            term = {
                "labelSelector": {
                    "matchExpressions": [
                        {"key": k, "operator": "In", "values": [v]}
                        for k, v in sorted(self.avoid_labels.items())
                    ]
                },
                "topologyKey": self.avoid_topology_key,
            }
            anti = affinity.setdefault("podAntiAffinity", {})
            required = anti.setdefault("requiredDuringSchedulingIgnoredDuringExecution", [])
            required.append(term)
        return affinity


def _as_str_map(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"scheduling.{label} must be a mapping of string->string")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"scheduling.{label} keys and values must be strings")
        out[k] = v
    return out


def _as_dict_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ValueError(f"scheduling.{label} must be a list of mappings")
    return [copy.deepcopy(x) for x in value]


def _as_pair(value: Any, label: str) -> tuple[str, str] | None:
    if value is None:
        return None
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(x, str) and x for x in value)):
        return (value[0], value[1])
    raise ValueError(f"scheduling.{label} must be a [key, value] pair of non-empty strings")


def job_name(run_id: str) -> str:
    return f"llmd-bench-{run_id}"


_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def validate_job_name(name: str) -> None:
    """Fail early with a clear error if the Job name isn't a DNS-1123 label (<=63 chars,
    lowercase alphanumeric + '-'), rather than letting kubectl reject the manifest opaquely.
    Note run_with_retries appends a '-aN' suffix, which counts toward the 63-char budget."""
    if len(name) > 63 or not _DNS1123.fullmatch(name):
        raise ValueError(
            f"invalid Job name {name!r}: must be a DNS-1123 label "
            f"(lowercase alphanumeric/'-', <=63 chars). Use a short, lowercase run_id."
        )


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
    # Optional scheduling intent (node affinity / GPU selection / anti-starvation placement).
    # When None the rendered manifest is byte-for-byte the generic cpu/memory baseline.
    scheduling: Scheduling | None = None

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
    name = job_name(spec.run_id)
    validate_job_name(name)

    container: dict[str, Any] = {
        "name": "benchmark",
        "image": spec.image,
        "command": list(spec.command),
        # Baseline, non-breaking hardening for the agent-chosen workload (it runs in-cluster):
        # no privilege escalation, drop all Linux caps, default seccomp. The in-cluster RBAC /
        # ServiceAccount + image pinning are defined by the packaging phase.
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "resources": {
            "requests": {"cpu": spec.cpu, "memory": spec.memory},
            "limits": {"cpu": spec.cpu, "memory": spec.memory},
        },
    }
    if spec.env:
        container["env"] = [{"name": k, "value": v} for k, v in spec.env.items()]

    # Optional GPU request — mechanism only: add the agent-chosen extended resource to BOTH
    # requests and limits (Kubernetes requires extended-resource requests == limits).
    sched = spec.scheduling
    if sched is not None:
        gpu_qty = sched.gpu_quantity()
        if gpu_qty is not None:
            container["resources"]["requests"][sched.gpu_resource] = gpu_qty
            container["resources"]["limits"][sched.gpu_resource] = gpu_qty

    pod_spec: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
    if spec.service_account:
        pod_spec["serviceAccountName"] = spec.service_account

    # Optional placement — each block is added ONLY when non-empty, so an unset/empty
    # Scheduling leaves the pod spec byte-for-byte identical to the generic baseline.
    if sched is not None:
        node_selector = sched.node_selector_map()
        if node_selector:
            pod_spec["nodeSelector"] = node_selector
        affinity = sched.effective_affinity()
        if affinity:
            pod_spec["affinity"] = affinity
        if sched.tolerations:
            pod_spec["tolerations"] = [copy.deepcopy(t) for t in sched.tolerations]

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
            "name": name,
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


def _as_int(v: Any) -> int:
    """Coerce a Job-status count to int, never crashing. ``kubectl get -o json`` normally yields
    integer ``active``/``succeeded``/``failed`` counts, but a forged or corrupt status object can
    carry a non-numeric value; a bare ``int("garbage")`` would then raise ``ValueError`` straight
    out of ``classify_job_status`` and abort the whole watch/reconstruct loop (which the cluster is
    the source of truth for). Anything not cleanly int-convertible reads as 0."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def classify_job_status(job_obj: dict[str, Any]) -> JobStatus:
    """Map a Job object (from ``kubectl get job -o json``) to a stable phase. A Complete
    condition → succeeded; a Failed condition → failed (carrying its reason/message, e.g.
    DeadlineExceeded for a timeout); otherwise active vs pending by the active count."""
    # `kube.parse_items` does NOT filter non-dict `items` elements, so a forged/corrupt
    # `kubectl get jobs -o json` can hand a non-dict job straight to `status()`/`reconstruct()`.
    # A bare `job_obj.get(...)` on a str/None/int would AttributeError and abort the whole
    # watch()/reconstruct() loop (the cluster is the source of truth those loops read). The
    # sibling `classify_failure` already filters non-dict pods (BUG-029); mirror that here so a
    # malformed top-level job degrades to ABSENT instead of crashing. Same class as BUG-023/037.
    if not isinstance(job_obj, dict):
        return JobStatus(name="", phase=ABSENT)
    meta = job_obj.get("metadata", {}) or {}
    status = job_obj.get("status", {}) or {}
    raw_conditions = status.get("conditions")
    # `kubectl get job -o json` emits a list of condition dicts, but a forged/corrupt object can
    # carry a scalar `conditions` or non-dict elements; the `(... or [])` fallback only catches a
    # FALSY value, so a truthy non-list (or a list with a str/None element) would `c.get(...)` →
    # AttributeError and abort the whole watch()/reconstruct() loop. Same crash class as BUG-029
    # (classify_failure) + BUG-023 (the counts): coerce a non-list to [] and skip non-dict elements
    # so a real terminal signal still classifies and malformed input degrades, never raises.
    conditions = [c for c in raw_conditions if isinstance(c, dict)] if isinstance(raw_conditions, list) else []
    active = _as_int(status.get("active", 0))
    succeeded = _as_int(status.get("succeeded", 0))
    failed = _as_int(status.get("failed", 0))

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
    if failed > 0 and active == 0:
        # failed count is set but the Failed condition isn't written yet (a real, brief K8s
        # window). With backoffLimit:0 the single attempt has failed — it IS terminal, so
        # don't misreport it as pending (which would make watch() poll forever).
        return JobStatus(name=name, phase=FAILED, active=active, succeeded=succeeded, failed=failed,
                         message="job failed", raw=job_obj)
    if active > 0:
        return JobStatus(name=name, phase=ACTIVE, active=active, succeeded=succeeded, failed=failed, raw=job_obj)
    return JobStatus(name=name, phase=PENDING, active=active, succeeded=succeeded, failed=failed, raw=job_obj)
