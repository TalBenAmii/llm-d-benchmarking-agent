# app/orchestrator/ — Kubernetes-native benchmark orchestrator

Turns a benchmark into an inspectable K8s **Job**, watches it to a terminal state, classifies
failures, retries transient faults, dead-letters deterministic ones, and parallelizes sweeps
with checkpoint/resume. **Mechanism only** — the *cluster* is the source of truth; this code
holds no local authoritative state (everything reconstructs from Job labels/annotations and a
ConfigMap checkpoint).

## The model you must understand
- **Job phase** (`job.py`): `pending` / `active` / `succeeded` / `failed` / `absent`.
- **Fault class** (`faults.py`, priority-ordered scan of Job+pod conditions): `timeout`, `oom`,
  `unschedulable`, `evicted`, `image_error`, `run_error`, `unknown`, `none`.
- **Retry decision** (`controller.py::run_with_retries`): **transient** (EVICTED, UNKNOWN) →
  resubmit as a *fresh* Job (`<run_id>-a2`, `-a3`, …); **deterministic** (OOM, UNSCHEDULABLE,
  IMAGE_ERROR, TIMEOUT) → dead-letter immediately; budget exhausted → dead-letter.

## Local invariants
- **`backoffLimit: 0`** (`job.py`): Kubernetes never retries — the orchestrator owns retries, so
  every attempt is a distinct, separately-inspectable Job. Don't "fix" this to a K8s retry.
- **Each attempt is a distinct Job** with a DNS-1123 name (`validate_job_name`, ≤63 chars); the
  `-aN` suffix counts toward the budget. Names/ids that aren't DNS-label-safe fail loudly here.
- **Labels vs annotations** (`job.py`): only simple DNS-safe ids go in **labels** (run_id, session_id,
  sweep_id, treatment) — those are the query keys; rich strings (spec/harness/workload with `/`) go in
  **annotations**. `managed-by=llmd-bench-agent` gates cleanup to our own Jobs.
- **Stateless watch** (`controller.py::watch`): every status read is a fresh `kubectl get`; the cluster
  is truth. A Job that vanishes after having existed is terminal (`absent`). `max_wait` is a *client*
  wall-clock bound — hitting it returns `active`/`pending`, NOT a failure (the Job may still finish).
- **Sweep checkpoint is the resume source of truth** (`checkpoint.py`): progress persists to a ConfigMap;
  COMPLETED treatments are skipped on resume; a completed treatment is never downgraded to in-flight.
  Two concurrent `run_sweep` calls **must not share a `sweep_id`** (shared mutable ConfigMap).
- **Best-effort, never-fatal side channels**: live log tail (`_tail_logs`) and metrics (`_safe_metric`)
  swallow all errors so they never interrupt the lifecycle.

## Key files
- `controller.py` — `BenchmarkOrchestrator`: submit / watch / diagnose / `run_with_retries` / `run_sweep` / reconstruct / cleanup.
- `job.py` — `JobSpec`/`JobStatus`, phase classification, manifest rendering, `Scheduling` (GPU/affinity/tolerations — type-validated, no policy).
- `faults.py` — failure classification.
- `kube.py` — `KubeClient`: allowlisted `kubectl` (apply/delete mutating + approval-gated; get/logs read-only).
- `checkpoint.py` — sweep ConfigMap checkpoint load/write/resume.

(Readiness analysis moved to its own package — see `app/readiness/CLAUDE.md`.)

## Scoped tests
```bash
pytest tests/orchestrator/test_orchestrator*.py        # controller, retry, sweep, checkpoint, faults, logstream, tool
```
`tests/orchestrator_fakes.py` provides an in-memory `FakeKubeClient` — orchestrator tests run hermetically (no cluster).
