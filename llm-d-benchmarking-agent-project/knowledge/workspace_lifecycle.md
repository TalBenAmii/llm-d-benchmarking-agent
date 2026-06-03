# Workspace lifecycle — retention/GC + startup self-check (Phase 18)

The agent's `workspace/` holds runtime scratch that grows with use: per-session transcripts
(`workspace/sessions/<id>/`), per-run scratch and orchestrator Job manifests
(`workspace/runs/<id>/`, `workspace/jobs/`), and the cross-session history store
(`workspace/history/*.json`). Left unbounded, a long-lived server fills its disk. Phase 18 adds a
**retention/GC pass** (mechanism in `app/storage/retention.py`) governed by **caps that are
DATA** on `Settings`, plus a **startup configuration self-check** surfaced at `/readyz`.

This file is the *judgment* layer: what the caps mean, what defaults are safe, and how to read a
self-check failure. The Python is pure mechanism — it walks, counts, and compares against the
caps; it makes no policy decision in `if/elif`.

## Retention caps (env / `Settings`)

| Cap | Env var | Default | Meaning |
|-----|---------|---------|---------|
| Max age | `RETENTION_MAX_AGE_DAYS` | `0` = unlimited | Remove items whose mtime is older than N days. |
| Max items | `RETENTION_MAX_ITEMS` | `500` per area | Keep at most N items per area; oldest overflow removed. |
| Max bytes | `RETENTION_MAX_BYTES` | `0` = unlimited | Keep an area's total on-disk size under N bytes; oldest removed until it fits. |

`0` (or unset/`None`) means **unlimited** for that dimension. Each cap is applied
**independently** to each managed area (`sessions/`, `runs/`, `jobs/`, `history/`); an item is
removed if **any** cap says it must, **oldest first** in every case.

### Why these defaults don't surprise existing users
Out of the box only the **item-count ceiling (500/area)** is active; **age and bytes are
unlimited**. So a fresh install reclaims **nothing** until an area exceeds 500 items — no
time-based or size-based deletion happens silently. Turn on active reclamation deliberately by
setting `RETENTION_MAX_AGE_DAYS` (e.g. `30`) and/or `RETENTION_MAX_BYTES` (e.g. a few GiB) to fit
your disk budget. GC runs once at **startup** (`RETENTION_GC_ON_STARTUP=true`, default on); the
same `run_gc()` is the seam for a periodic hook if you want one.

### Active-run safety (never broken)
A session that is **currently held in memory or running a turn** is treated as **active** and is
**never pruned**, regardless of caps — even if it is the oldest/largest item. Only the
`sessions/` area is active-aware (only sessions can be "running"); `runs/`, `jobs/`, and
`history/` are scratch/records with no live owner. This is the hard invariant: GC reclaims dead
scratch, never an in-flight benchmark's state.

## Startup self-check → `/readyz`

`self_check(settings)` returns a structured `SelfCheckResult` (overall `ok` plus a per-probe
pass/fail + reason). Probes:

- **workspace_writable** — the workspace root exists and accepts a write. A failure means every
  session snapshot / run manifest / history write will fail at request time → fix the path or
  permissions before serving traffic.
- **provider_coherent** — `LLM_PROVIDER` is a known provider AND its required key is set
  (`ANTHROPIC_API_KEY` for anthropic, `OPENAI_API_KEY` for openai/openai-compatible/vllm). The
  most common misconfiguration (provider named, key forgotten) surfaces here, not on first chat.
  It only inspects config — it never contacts the provider.
- **repos_resolvable** — both read-only sibling repos (`llm-d/`, `llm-d-benchmark/`) resolve on
  disk. Missing repos break catalog/report/capacity paths. Set `REPOS_DIR` correctly.
- **auth_coherent** — if `AUTH_ENABLED` is set, `AUTH_TOKEN` must be non-empty (else every
  request 401s). Mirrors the fail-loud startup guard as a structured readiness signal.

`/readyz` returns **200** when ready, **503** with the structured reasons when not. Liveness stays
on `/healthz`; readiness is the deploy/orchestrator gate. `readiness()` folds this self-check into
the `/readyz` composer. When `STARTUP_SELF_CHECK=false`, readiness reports ready with the
self-check marked skipped (an operator who turned it off isn't held un-ready).

### Reading a failure
A 503 from `/readyz` is a **configuration** problem, not a transient one — the listed reasons name
exactly which probe failed and why. Resolve the named cause (path, key, repo, token) and restart;
retrying without changing config will keep failing.
