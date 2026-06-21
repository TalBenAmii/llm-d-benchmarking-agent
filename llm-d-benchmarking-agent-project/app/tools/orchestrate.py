"""Agent tool: run a benchmark as a Kubernetes Job via the orchestrator.

Thin wiring — it builds a :class:`~app.orchestrator.job.JobSpec` from the agent's intent and
drives :class:`~app.orchestrator.controller.BenchmarkOrchestrator` (submit → watch → diagnose,
with optional retry). All cluster access flows through the allowlisted kubectl runner on the
session's ToolContext, so apply/delete stay approval-gated. Judgment (which spec/harness/
workload, retry budget) is the agent's; this is mechanism.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.observability.resource_poller import resource_stats_poller
from app.orchestrator.controller import BenchmarkOrchestrator, RunOutcome, SweepOutcome
from app.orchestrator.job import JobSpec, Scheduling, job_name, validate_job_name
from app.orchestrator.kube import RealKubeClient
from app.readiness import check_endpoint_readiness
from app.tools.context import ToolContext, ToolError


def _live_log_sink(ctx: ToolContext) -> Callable[[str], Awaitable[None]] | None:
    """Build the per-line sink that forwards a benchmark pod's live logs to the UI as
    ``output`` events — the exact event the runner already emits for streamed command output,
    so the UI renders pod logs with no new transport. Returns None when no emitter is wired
    (streaming is then simply disabled), keeping non-UI callers unchanged."""
    emit = ctx.emit
    if emit is None:
        return None

    async def _sink(line: str) -> None:
        await emit("output", {"line": line})

    return _sink


def _default_command(spec: str | None, harness: str | None, workload: str | None, namespace: str) -> list[str]:
    argv = ["llmdbenchmark"]
    if spec:
        argv += ["--spec", spec]
    argv += ["run", "-p", namespace]
    if harness:
        argv += ["-l", harness]
    if workload:
        argv += ["-w", workload]
    return argv


def _serialize_failure(failure) -> dict[str, Any] | None:
    if failure is None or not getattr(failure, "is_failure", False):
        return None
    return {"kind": failure.kind, "message": failure.message, "pod": failure.pod,
            "container": failure.container, "exit_code": failure.exit_code}


def _serialize_outcome(outcome: RunOutcome) -> dict[str, Any]:
    return {
        "run_id": outcome.run_id,
        "succeeded": outcome.succeeded,
        "dead_lettered": outcome.dead_lettered,
        "attempts": [
            {"run_id": a.run_id, "phase": a.status.phase, "reason": a.status.reason,
             "failure": _serialize_failure(a.failure)}
            for a in outcome.attempts
        ],
        "final_failure": _serialize_failure(outcome.final_failure),
    }


async def orchestrate_benchmark_run(
    ctx: ToolContext,
    *,
    namespace: str,
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    image: str | None = None,
    service_account: str | None = None,
    command: list[str] | None = None,
    cpu: str = "1",
    memory: str = "1Gi",
    scheduling: dict[str, Any] | None = None,
    active_deadline_seconds: int | None = None,
    max_attempts: int = 1,
    watch: bool = True,
    poll_interval: float = 3.0,
    max_wait: float = 3600.0,
    require_ready_endpoint: bool = True,
) -> dict[str, Any]:
    image = image or ctx.settings.orchestrator_image
    if not image:
        raise ToolError(
            "an orchestrated benchmark run is a real Kubernetes Job and needs a container "
            "image carrying the llmdbenchmark CLI + kubectl — set ORCHESTRATOR_IMAGE in the "
            "backend .env or pass `image`. (The in-cluster agent image is built in the "
            "packaging phase; until then, use execute_llmdbenchmark for the local CLI path.)"
        )

    # Run the Job under the least-privilege ServiceAccount the packaging deploy creates (so an
    # in-cluster orchestrated run has exactly the RBAC it needs); empty → namespace default SA.
    sa = service_account if service_account is not None else (ctx.settings.orchestrator_service_account or None)

    # Parse the agent's scheduling intent (node affinity / GPU selection / anti-starvation
    # placement). PURE PARSING — the WHICH-GPU / WHERE-to-place choice is the agent's judgment
    # (knowledge/resource_management.md); a malformed shape is a ToolError the agent self-corrects.
    try:
        sched = Scheduling.from_dict(scheduling)
    except ValueError as exc:
        raise ToolError(f"invalid scheduling: {exc}") from exc

    # Phase 24: GATE on a real inference-endpoint readiness check before submitting — don't
    # benchmark an unready stack. This goes BEYOND pod presence: it verifies a Service has a
    # READY backing endpoint (see app/readiness/diagnostics.py). When not ready we submit
    # NOTHING and return a structured not-ready outcome carrying a standup suggestion the agent
    # can OFFER (approval-gated). The DECISION to stand up is the agent's/user's judgment; this
    # is just the mechanism. Skipped in simulate mode (the synthetic walk deploys nothing) and
    # when the caller explicitly opts out (e.g. it just stood the stack up and knows it's ready).
    if require_ready_endpoint and not ctx.settings.simulate:
        readiness = await check_endpoint_readiness(ctx, namespace=namespace, spec=spec)
        if not readiness.get("ready"):
            return {
                "submitted": False,
                "ready": False,
                "namespace": namespace,
                "readiness": readiness,
                "standup_suggestion": readiness.get("standup_suggestion"),
                "note": "Benchmark NOT submitted: the inference endpoint in this namespace is "
                        "not ready (no Service has a ready backing endpoint). Nothing was "
                        "mutated. Offer to stand up a stack first (approval-gated) — see "
                        "standup_suggestion — or pass require_ready_endpoint=false to override "
                        "if you know the endpoint is reachable another way.",
            }

    run_id = uuid.uuid4().hex[:8]
    spec_obj = JobSpec(
        run_id=run_id,
        namespace=namespace,
        image=image,
        command=command or _default_command(spec, harness, workload, namespace),
        session_id=ctx.workspace.name,        # session dir name → labels for reconstruction
        spec=spec or "",
        harness=harness or "",
        workload=workload or "",
        active_deadline_seconds=active_deadline_seconds,
        cpu=cpu,
        memory=memory,
        service_account=sa,
        scheduling=sched,
    )

    orch = BenchmarkOrchestrator(RealKubeClient(ctx), ctx.workspace)

    if not watch:
        job = await orch.submit(spec_obj)
        return {"submitted": True, "run_id": run_id, "job": job, "namespace": namespace,
                "note": "Job submitted; not watched. Use manage_orchestrated_runs(action='list') "
                        "to check its status, or action='stop' to delete it."}

    # Phase 21: forward the benchmark pod's live log lines to the UI as `output` events — the
    # SAME transport the runner already uses for streamed command output — so the user sees
    # benchmark progress in real time during the run, not just at the end. Best-effort: a
    # failing tail never breaks the run (guarded in the orchestrator), and with no emitter
    # wired (e.g. a bare unit test) streaming is simply disabled.
    on_log_line = _live_log_sink(ctx)

    # Stream live cluster CPU/memory for the run alongside it — backend-only, zero LLM cost (it
    # never enters the message stream). Namespace-wide (the bench namespace is dedicated, so no
    # run-id selector needed). No-op without a UI emitter or in simulate mode.
    async with resource_stats_poller(ctx, namespace=namespace):
        outcome = await orch.run_with_retries(
            spec_obj, max_attempts=max_attempts, poll_interval=poll_interval, max_wait=max_wait,
            on_log_line=on_log_line,
        )
    return {"namespace": namespace, **_serialize_outcome(outcome)}


# DNS-1123 budget for a treatment run-id. A Job name is "llmd-bench-<run_id>" and
# run_with_retries appends "-aN" (the attempt, <=5 → "-a5"); the whole name must be <=63 chars.
# So a run-id is <=49 chars and, formed as "{sweep_id}-{slug(name)}", the slug gets whatever the
# sweep_id leaves. The sweep_id is also bounded so the checkpoint ConfigMap name
# ("llmd-bench-sweep-<id>") fits and a usable slug budget remains.
_RUN_ID_BUDGET = 63 - len("llmd-bench-") - len("-a5")   # 49
_SWEEP_ID_MAX = 40
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _slug(text: str) -> str:
    """Lowercase, DNS-1123-safe slug of a treatment name (each non-alphanumeric run → '-')."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _name_fallback_slug(name: str) -> str:
    """A short, DNS-safe, NAME-DERIVED slug for a treatment whose name slugs to nothing (e.g.
    all-punctuation / non-ASCII — the schema's ``name`` field has no pattern constraint). It is
    a pure function of the NAME (not the treatment's position), so the SAME name always yields
    the SAME run-id across a sweep and its later same-``sweep_id`` resume (the checkpoint can
    then skip the completed treatment). Distinct empty-slug names hash to distinct slugs, so the
    caller's run-id collision check stays a backstop, not the primary distinguisher."""
    return "t" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def _sweep_run_id(sweep_id: str, name: str, index: int) -> str:
    """Stable, DNS-safe run-id for a treatment: ``{sweep_id}-{slug(name)}`` truncated to the
    Job-name budget. A PURE function of (sweep_id, name) — so a resume maps the same treatment
    name to the same run-id (and the cluster checkpoint can skip it) regardless of the
    treatment's POSITION in the list. When the name slugs to nothing, fall back to a stable
    name-derived hash slug (NOT the 1-based index, which would shift when treatments are
    reordered / inserted between a sweep and its resume — re-running an already-completed
    treatment as a duplicate Job)."""
    budget = _RUN_ID_BUDGET - len(sweep_id) - 1   # minus the joining '-'
    slug = _slug(name)[:max(budget, 0)].strip("-") or _name_fallback_slug(name)
    return f"{sweep_id}-{slug}"


def _serialize_sweep(outcome: SweepOutcome, names_by_run_id: dict[str, str]) -> dict[str, Any]:
    """Roll the per-treatment outcomes up into a flat, treatment-named result (the agent never
    sees the internal run-ids — it gets back the treatment names it supplied)."""
    def _name(run_id: str) -> str:
        return names_by_run_id.get(run_id, run_id)

    per = []
    for o in outcome.outcomes:
        d = _serialize_outcome(o)
        d["treatment"] = _name(o.run_id)
        per.append(d)
    return {
        "treatments": per,
        "n_treatments": len(per),
        "succeeded": [_name(r) for r in outcome.succeeded],
        "dead_lettered": [_name(r) for r in outcome.dead_lettered],
        "resumed": [_name(r) for r in outcome.resumed],
        "n_succeeded": len(outcome.succeeded),
        "n_dead_lettered": len(outcome.dead_lettered),
        "all_succeeded": outcome.all_succeeded,
    }


async def orchestrate_sweep(
    ctx: ToolContext,
    *,
    namespace: str,
    treatments: list[dict[str, Any]],
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    image: str | None = None,
    service_account: str | None = None,
    cpu: str = "1",
    memory: str = "1Gi",
    scheduling: dict[str, Any] | None = None,
    active_deadline_seconds: int | None = None,
    max_parallel: int = 2,
    max_attempts: int = 2,
    poll_interval: float = 3.0,
    max_wait: float = 3600.0,
    sweep_id: str | None = None,
    checkpoint: bool = True,
    require_ready_endpoint: bool = True,
) -> dict[str, Any]:
    """Run N benchmark treatments as PARALLEL Kubernetes Jobs under a concurrency cap, each with
    its own retry/dead-letter budget, with optional cluster-checkpointed resume — the proposal's
    parallel DoE-treatment scheduling. Thin wiring over
    :meth:`~app.orchestrator.controller.BenchmarkOrchestrator.run_sweep`; the WHICH-treatments
    judgment is the agent's (knowledge/sweep_playbook.md)."""
    image = image or ctx.settings.orchestrator_image
    if not image:
        raise ToolError(
            "an orchestrated sweep is a set of real Kubernetes Jobs and needs a container image "
            "carrying the llmdbenchmark CLI + kubectl — set ORCHESTRATOR_IMAGE in the backend "
            ".env or pass `image`. (Until the in-cluster agent image is built, use "
            "execute_llmdbenchmark(subcommand='experiment') for the local sequential DoE path.)"
        )

    if not treatments:
        raise ToolError("orchestrate_sweep needs at least one treatment")

    sa = service_account if service_account is not None else (ctx.settings.orchestrator_service_account or None)

    # Parse the agent's scheduling intent once and apply it to EVERY treatment (PURE PARSING —
    # the placement judgment is the agent's, knowledge/resource_management.md).
    try:
        sched = Scheduling.from_dict(scheduling)
    except ValueError as exc:
        raise ToolError(f"invalid scheduling: {exc}") from exc

    # Resolve / validate the sweep id — the resume key AND the Job/checkpoint label. A fresh
    # sweep gets a generated id (returned so the agent can resume); a supplied one is validated.
    if sweep_id is None:
        sweep_id = "sw-" + uuid.uuid4().hex[:8]
    elif len(sweep_id) > _SWEEP_ID_MAX or not _DNS_LABEL.fullmatch(sweep_id):
        raise ToolError(
            f"invalid sweep_id {sweep_id!r}: must be a short DNS-1123 label (lowercase "
            f"alphanumeric/'-', <= {_SWEEP_ID_MAX} chars)."
        )

    # Treatment names must be unique — they form the stable, resume-keyed run-ids.
    names = [t["name"] for t in treatments]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ToolError(f"treatment names must be unique (they key the resumable run-ids); duplicated: {dupes}")

    # One JobSpec per treatment: a stable run-id from its name, inheriting the top-level
    # spec/harness/workload/cpu/memory unless the treatment overrides them.
    specs: list[JobSpec] = []
    names_by_run_id: dict[str, str] = {}
    for i, t in enumerate(treatments, start=1):
        run_id = _sweep_run_id(sweep_id, t["name"], i)
        # The worst-case attempt Job name must be a valid DNS-1123 label (fail loudly here, not
        # opaquely at apply time). Truncation can collide two distinct names → reject so the
        # agent picks more distinct names (a silent merge would corrupt the checkpoint).
        try:
            validate_job_name(job_name(f"{run_id}-a{max_attempts}"))
        except ValueError as exc:
            raise ToolError(f"treatment {t['name']!r} yields an invalid Job name: {exc}") from exc
        if run_id in names_by_run_id:
            raise ToolError(
                f"treatments {names_by_run_id[run_id]!r} and {t['name']!r} collide on the same "
                f"run-id {run_id!r} after slugifying — give them more distinct names."
            )
        names_by_run_id[run_id] = t["name"]
        eff_spec = t.get("spec") or spec
        eff_harness = t.get("harness") or harness
        eff_workload = t.get("workload") or workload
        specs.append(JobSpec(
            run_id=run_id,
            namespace=namespace,
            image=image,
            command=t.get("command") or _default_command(eff_spec, eff_harness, eff_workload, namespace),
            session_id=ctx.workspace.name,
            sweep_id=sweep_id,
            treatment=i,
            spec=eff_spec or "",
            harness=eff_harness or "",
            workload=eff_workload or "",
            active_deadline_seconds=active_deadline_seconds,
            cpu=t.get("cpu") or cpu,
            memory=t.get("memory") or memory,
            service_account=sa,
            scheduling=sched,
        ))

    # Gate ONCE on a real endpoint-readiness check before submitting anything — all treatments
    # share the one stood-up stack in this namespace. Not ready → submit NOTHING (mirror
    # orchestrate_benchmark_run). Skipped in simulate mode and when the caller opts out.
    if require_ready_endpoint and not ctx.settings.simulate:
        readiness = await check_endpoint_readiness(ctx, namespace=namespace, spec=spec)
        if not readiness.get("ready"):
            return {
                "submitted": False,
                "ready": False,
                "namespace": namespace,
                "sweep_id": sweep_id,
                "readiness": readiness,
                "standup_suggestion": readiness.get("standup_suggestion"),
                "note": "Sweep NOT submitted: the inference endpoint in this namespace is not "
                        "ready (no Service has a ready backing endpoint). Nothing was mutated. "
                        "Offer to stand up a stack first (approval-gated) — see "
                        "standup_suggestion — or pass require_ready_endpoint=false to override.",
            }

    orch = BenchmarkOrchestrator(RealKubeClient(ctx), ctx.workspace)
    # Each treatment streams its pod logs live, prefixed with its run-id (best-effort; a failing
    # tail never affects the sweep). Live cluster CPU/mem rides alongside via the resource poller.
    on_log_line = _live_log_sink(ctx)
    async with resource_stats_poller(ctx, namespace=namespace):
        outcome = await orch.run_sweep(
            specs,
            max_parallel=max_parallel,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            max_wait=max_wait,
            on_log_line=on_log_line,
            # When checkpointing, the cluster ConfigMap is the resume source of truth; otherwise
            # run statelessly (no checkpoint writes, no resume).
            sweep_id=sweep_id if checkpoint else None,
            namespace=namespace,
        )
    return {
        "namespace": namespace,
        "sweep_id": sweep_id,
        "checkpointed": checkpoint,
        "max_parallel": max_parallel,
        **_serialize_sweep(outcome, names_by_run_id),
    }
