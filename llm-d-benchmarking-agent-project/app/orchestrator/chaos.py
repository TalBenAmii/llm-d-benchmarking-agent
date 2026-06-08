"""Chaos / fault-injection harness — the ADDITIVE opt-in seam for the resilience drill.

The whole point of the resilience drill is to prove the *existing, unmodified* Job lifecycle
(``classify_failure`` → ``run_with_retries`` retry/dead-letter → checkpoint/reconstruct) is
correct under adverse conditions. So chaos is NOT a change to that lifecycle — it is a
:class:`~app.orchestrator.kube.KubeClient` **decorator** that wraps any underlying client and
*deterministically rewrites cluster READ responses* (the Job/pod JSON returned by ``list_jobs``
/ ``list_pods``) into fault-shaped JSON at a controlled point. Those fault-shaped reads flow
through the completely unchanged ``diagnose()`` → ``classify_failure`` → retry/dead-letter
path — zero new classification or retry logic.

Mechanism only:

* :class:`FaultInjection` / :class:`ChaosPlan` are PURE DATA the agent supplies (mirroring
  :class:`~app.orchestrator.job.Scheduling`): ``ChaosPlan.from_dict`` validates the *shape*,
  never the *wisdom* (which faults to inject for a scenario is the agent's judgment, in
  ``knowledge/resilience.md``).
* :class:`FaultLedger` is the authoritative append-only record of what was injected and when.
* :class:`ChaosKubeClient` implements the Protocol by delegation: it intercepts only
  ``list_jobs`` / ``list_pods`` to present the planned fault; every other method passes
  through verbatim. Deterministic via a seeded RNG.

Recommended usage: wrap the FAKE/in-process client (hermetic; never deliberately break a real
cluster). The decorator is constructed solely inside ``run_resilience_drill`` and only when the
backend ``CHAOS_ENABLED`` flag permits — two layers of opt-in, and never on the production
``orchestrate_benchmark_run`` path.
"""
from __future__ import annotations

import copy
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.orchestrator.faults import (
    EVICTED,
    IMAGE_ERROR,
    OOM,
    RUN_ERROR,
    TIMEOUT,
    UNKNOWN,
    UNSCHEDULABLE,
)
from app.orchestrator.job import (
    ANNO_ATTEMPT,
    LABEL_RUN,
    job_name,
)
from app.orchestrator.kube import KubeClient
from app.security.runner import RunResult

# The fault kinds a drill can inject. ``none`` is not injectable (it means "no fault"). These
# are exactly the kinds ``classify_failure`` recognizes, so a rewritten read is classified
# back to the same kind by the UNMODIFIED classifier.
INJECTABLE_KINDS = frozenset({TIMEOUT, OOM, UNSCHEDULABLE, EVICTED, IMAGE_ERROR, RUN_ERROR, UNKNOWN})

# Where, in an attempt's lifecycle, the fault is presented.
#   "before-watch" — the FIRST job read for the attempt already shows failed (the common case).
#   "mid-watch"    — the job reads as active for ``after_polls`` polls, then fails (a fault that
#                    develops after the run starts).
# Both rewrite the SAME terminal fault; the point only controls WHEN the failed snapshot appears.
POINT_BEFORE_WATCH = "before-watch"
POINT_MID_WATCH = "mid-watch"
_VALID_POINTS = frozenset({POINT_BEFORE_WATCH, POINT_MID_WATCH})


@dataclass
class FaultInjection:
    """One planned fault. ``at_attempt`` targets a specific attempt's Job (``-a<N>``), so a
    drill can inject a transient fault on attempt 1 and let attempt 2 succeed. ``probability``
    (with the plan's seed) makes realization deterministic. Pure data."""

    kind: str
    at_attempt: int = 1
    point: str = POINT_BEFORE_WATCH
    probability: float = 1.0
    after_polls: int = 1          # for "mid-watch": how many active polls before the fault shows
    exit_code: int | None = None  # optional container exit code (oom/run_error)
    message: str = ""


@dataclass
class ChaosPlan:
    """An agent-supplied set of fault injections + a deterministic seed. Same pattern as
    :class:`~app.orchestrator.job.Scheduling`: a pure, type-validated, policy-free data object;
    Python rejects on *shape*, never on *wisdom*."""

    injections: list[FaultInjection] = field(default_factory=list)
    seed: int = 0

    def is_empty(self) -> bool:
        return not self.injections

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ChaosPlan:
        """Parse an agent-supplied chaos plan (the tool's ``chaos_plan`` arg) into a
        :class:`ChaosPlan`. PURE PARSING + shape validation — accept/reject on TYPE, not on
        policy. Unknown top-level or per-injection keys are rejected so a typo can't silently
        no-op. ``None``/empty yields an empty plan (no chaos). Mirrors
        :meth:`Scheduling.from_dict`."""
        if not data:
            return cls()
        allowed_top = {"injections", "faults", "seed"}
        unknown = set(data) - allowed_top
        if unknown:
            raise ValueError(
                f"unknown chaos_plan field(s): {sorted(unknown)}; allowed: {sorted(allowed_top)}"
            )

        seed_raw = data.get("seed", 0)
        if isinstance(seed_raw, bool) or not isinstance(seed_raw, int):
            raise ValueError("chaos_plan.seed must be an integer")

        # Accept either `injections` or the friendlier alias `faults` (a list of fault dicts).
        raw_list = data.get("injections")
        if raw_list is None:
            raw_list = data.get("faults")
        if raw_list is None:
            raw_list = []
        if not isinstance(raw_list, list):
            raise ValueError("chaos_plan.injections must be a list of fault objects")

        injections = [cls._parse_injection(i, idx) for idx, i in enumerate(raw_list)]
        return cls(injections=injections, seed=seed_raw)

    @staticmethod
    def _parse_injection(raw: Any, idx: int) -> FaultInjection:
        if not isinstance(raw, dict):
            raise ValueError(f"chaos_plan.injections[{idx}] must be a mapping")
        allowed = {"kind", "at_attempt", "point", "probability", "after_polls", "exit_code", "message"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unknown injection field(s) {sorted(unknown)} at injections[{idx}]; "
                f"allowed: {sorted(allowed)}"
            )

        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in INJECTABLE_KINDS:
            raise ValueError(
                f"injections[{idx}].kind must be one of {sorted(INJECTABLE_KINDS)} (got {kind!r})"
            )

        at_attempt = raw.get("at_attempt", 1)
        if isinstance(at_attempt, bool) or not isinstance(at_attempt, int) or at_attempt < 1:
            raise ValueError(f"injections[{idx}].at_attempt must be an integer >= 1")

        point = raw.get("point", POINT_BEFORE_WATCH)
        if point not in _VALID_POINTS:
            raise ValueError(
                f"injections[{idx}].point must be one of {sorted(_VALID_POINTS)} (got {point!r})"
            )

        probability = raw.get("probability", 1.0)
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError(f"injections[{idx}].probability must be a number in [0, 1]")
        probability = float(probability)
        if not (0.0 <= probability <= 1.0):
            raise ValueError(f"injections[{idx}].probability must be in [0, 1]")

        after_polls = raw.get("after_polls", 1)
        if isinstance(after_polls, bool) or not isinstance(after_polls, int) or after_polls < 1:
            raise ValueError(f"injections[{idx}].after_polls must be an integer >= 1")

        exit_code = raw.get("exit_code")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError(f"injections[{idx}].exit_code must be an integer")

        message = raw.get("message", "")
        if not isinstance(message, str):
            raise ValueError(f"injections[{idx}].message must be a string")

        return FaultInjection(
            kind=kind, at_attempt=at_attempt, point=point, probability=probability,
            after_polls=after_polls, exit_code=exit_code, message=message,
        )


@dataclass
class LedgerEntry:
    """One realized-or-skipped injection, recorded against the per-attempt run id."""

    run_id: str          # the per-attempt run id the fault was injected against (e.g. rd-1-a1)
    kind: str
    attempt: int
    point: str
    realized: bool       # True iff the probability roll fired (the fault was actually presented)


@dataclass
class FaultLedger:
    """Append-only, authoritative record of what the chaos decorator injected and when. The
    resilience report cross-references THIS (ground truth) against the ``RunOutcome`` the
    unmodified controller produced."""

    entries: list[LedgerEntry] = field(default_factory=list)

    def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def realized(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.realized]


# --- pod/job rewriting (shapes mirror tests/orchestrator_fakes.make_job/make_pod) -----------

# Default container exit codes for fault kinds where a code is conventional.
_DEFAULT_EXIT = {OOM: 137, RUN_ERROR: 1}


def _failed_job_snapshot(base: dict[str, Any], *, reason: str, message: str) -> dict[str, Any]:
    """Rewrite a Job snapshot into a `failed` phase carrying a Failed condition. Same JSON shape
    `classify_job_status` consumes; preserves metadata/labels so selectors still match."""
    job = copy.deepcopy(base)
    job["status"] = {
        "failed": 1,
        "conditions": [
            {"type": "Failed", "status": "True", "reason": reason, "message": message}
        ],
    }
    return job


def _fault_pod(run_id: str, namespace: str, inj: FaultInjection) -> dict[str, Any]:
    """Build the fault-shaped pod JSON `classify_failure` will classify back to ``inj.kind``.
    Mirrors the exact shapes `tests/orchestrator_fakes.make_pod` produces."""
    name = f"{job_name(run_id)}-xyz"
    pod: dict[str, Any] = {
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {LABEL_RUN: run_id, "job-name": job_name(run_id)}},
        "status": {"phase": "Failed", "containerStatuses": []},
    }
    st = pod["status"]
    kind = inj.kind
    code = inj.exit_code if inj.exit_code is not None else _DEFAULT_EXIT.get(kind)
    if kind == OOM:
        term: dict[str, Any] = {"reason": "OOMKilled"}
        if code is not None:
            term["exitCode"] = code
        st["containerStatuses"] = [{"name": "benchmark", "state": {"terminated": term}}]
    elif kind == RUN_ERROR:
        term = {"reason": inj.message or "Error"}
        if code is not None:
            term["exitCode"] = code
        st["containerStatuses"] = [{"name": "benchmark", "state": {"terminated": term}}]
    elif kind == IMAGE_ERROR:
        st["containerStatuses"] = [
            {"name": "benchmark", "state": {"waiting": {"reason": "ImagePullBackOff",
                                                        "message": inj.message or "image pull failed"}}}
        ]
    elif kind == UNSCHEDULABLE:
        st["conditions"] = [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
             "message": inj.message or "0/1 nodes are available: insufficient cpu"}
        ]
    elif kind == EVICTED:
        st["reason"] = "Evicted"
        st["message"] = inj.message or "pod evicted under node pressure"
    # UNKNOWN (and TIMEOUT) leave no pod-level signal: the Job's failed/DeadlineExceeded carries it.
    return pod


def _failed_reason_for(kind: str) -> str:
    """The Job Failed-condition reason for a kind. TIMEOUT must read as ``DeadlineExceeded`` so
    the UNMODIFIED `classify_failure` reports it as a timeout from the Job alone."""
    return "DeadlineExceeded" if kind == TIMEOUT else "BackoffLimitExceeded"


class ChaosKubeClient:
    """A :class:`~app.orchestrator.kube.KubeClient` decorator that rewrites cluster READ
    responses into fault-shaped JSON per a :class:`ChaosPlan`, recording each into a
    :class:`FaultLedger`. All other operations pass through to the wrapped client verbatim —
    the production lifecycle path (apply/delete/logs/streams/configmaps) is untouched.

    Determinism: a single seeded RNG decides probability rolls; the same plan + seed reproduce
    the same injections.

    It targets a specific attempt's Job by the ``-a<N>`` suffix on the run-id label, so a
    transient fault can fire on attempt 1 and the retried attempt 2 can succeed (proving the
    unmodified retry path)."""

    def __init__(self, inner: KubeClient, plan: ChaosPlan, *, ledger: FaultLedger | None = None):
        self._inner = inner
        self._plan = plan
        self.ledger = ledger if ledger is not None else FaultLedger()
        self._rng = random.Random(plan.seed)
        # Per-attempt-run-id poll counter, so "mid-watch" can fail after N active polls.
        self._poll_counts: dict[str, int] = {}
        # Per-attempt-run-id realized-fault cache, so repeated reads of the same attempt present
        # the SAME fault (and the probability roll happens exactly once per attempt).
        self._decided: dict[str, FaultInjection | None] = {}

    # ---- the two intercepted reads -----------------------------------------

    async def list_jobs(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        jobs = await self._inner.list_jobs(namespace=namespace, selector=selector)
        out: list[dict[str, Any]] = []
        for job in jobs:
            inj = self._decide(job, namespace)
            if inj is None:
                out.append(job)
                continue
            run_id = _run_id_of(job)
            if inj.point == POINT_MID_WATCH:
                self._poll_counts[run_id] = self._poll_counts.get(run_id, 0) + 1
                if self._poll_counts[run_id] <= inj.after_polls:
                    # Still "running" — present an active snapshot until the fault is due.
                    out.append(_active_job_snapshot(job))
                    continue
            out.append(_failed_job_snapshot(
                job, reason=_failed_reason_for(inj.kind),
                message=inj.message or f"chaos-injected {inj.kind}",
            ))
        return out

    async def list_pods(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        pods = await self._inner.list_pods(namespace=namespace, selector=selector)
        run_id = _run_id_from_selector(selector)
        if run_id is not None:
            decided = self._decided.get(run_id)
            if decided is not None:
                # diagnose() reads pods AFTER seeing the failed job; present the fault-shaped pod
                # so the UNMODIFIED classify_failure recovers exactly the injected kind.
                return [_fault_pod(run_id, namespace, decided)]
        return pods

    # ---- pure pass-throughs (production lifecycle path, untouched) ----------

    async def apply(self, manifest_path: str | Path, *, namespace: str) -> RunResult:
        return await self._inner.apply(manifest_path, namespace=namespace)

    async def list_configmaps(self, *, namespace: str,
                              selector: str | None = None) -> list[dict[str, Any]]:
        return await self._inner.list_configmaps(namespace=namespace, selector=selector)

    async def logs(self, *, namespace: str, selector: str, tail: int | None = None,
                   follow: bool = False) -> str:
        return await self._inner.logs(namespace=namespace, selector=selector, tail=tail, follow=follow)

    def stream_log_lines(self, *, namespace: str, selector: str,
                         tail: int | None = None) -> AsyncIterator[str]:
        return self._inner.stream_log_lines(namespace=namespace, selector=selector, tail=tail)

    async def delete_job(self, name: str, *, namespace: str,
                         ignore_not_found: bool = True) -> RunResult:
        return await self._inner.delete_job(name, namespace=namespace, ignore_not_found=ignore_not_found)

    # ---- internal: decide (once per attempt) whether a fault fires ----------

    def _decide(self, job: dict[str, Any], namespace: str) -> FaultInjection | None:
        """Decide (and cache, once) which injection — if any — applies to this attempt's Job.
        The probability roll happens exactly once per per-attempt run id; the result is cached so
        repeated reads of the same attempt are consistent and the ledger records each fault once."""
        run_id = _run_id_of(job)
        if not run_id:
            return None
        if run_id in self._decided:
            return self._decided[run_id]

        attempt = _attempt_of(run_id, job)
        match = next((i for i in self._plan.injections if i.at_attempt == attempt), None)
        if match is None:
            self._decided[run_id] = None
            return None

        realized = self._rng.random() < match.probability
        self.ledger.record(LedgerEntry(
            run_id=run_id, kind=match.kind, attempt=attempt, point=match.point, realized=realized,
        ))
        decided = match if realized else None
        self._decided[run_id] = decided
        return decided


def _active_job_snapshot(base: dict[str, Any]) -> dict[str, Any]:
    job = copy.deepcopy(base)
    job["status"] = {"active": 1}
    return job


def _run_id_of(job: dict[str, Any]) -> str:
    return ((job.get("metadata", {}) or {}).get("labels", {}) or {}).get(LABEL_RUN, "")


def _attempt_of(run_id: str, job: dict[str, Any]) -> int:
    """The attempt number for a per-attempt Job. ``run_with_retries`` names attempts
    ``<base>-a<N>``, which is the AUTHORITATIVE per-attempt marker it always sets — prefer it,
    then fall back to the manifest's attempt annotation (the FakeKubeClient hardcodes the
    annotation to "1" for every snapshot, so the run-id suffix must win)."""
    if "-a" in run_id:
        tail = run_id.rsplit("-a", 1)[1]
        if tail.isdigit():
            return int(tail)
    anno = (job.get("metadata", {}) or {}).get("annotations", {}) or {}
    raw = anno.get(ANNO_ATTEMPT)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 1


def _run_id_from_selector(selector: str | None) -> str | None:
    for part in (selector or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == LABEL_RUN and v:
            return v
    return None
