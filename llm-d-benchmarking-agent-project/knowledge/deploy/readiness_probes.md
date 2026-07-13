# Model-load readiness: still loading weights vs wedged/broken

This guide is the JUDGMENT for the Phase 59 serving-readiness gate. The
`check_endpoint_readiness` tool gathers FACTS only (pod conditions + the `/v1/models` vs
`/health` probe outcomes) into a `serving_readiness` block; **you** turn those facts into a
verdict and a recommended action HERE — there is no loading-vs-broken `if/elif` in Python.

Use this whenever a Service exists but is **Running-but-NotReady** (the gate returns
`reason: "endpoints_not_ready"` with a `serving_readiness` block). Read it BEFORE telling the
user to wait or to stop, and BEFORE any benchmark is submitted against that namespace.

## Why presence is not readiness

A vLLM inference container has three distinct lifecycle stages (see
`llm-d/docs/operations/readiness-probes.md`):

1. **Container Running** — the Kubernetes container started. Says nothing about serving.
2. **API server alive** — the OpenAI-compatible server process accepts connections. This is
   what the **liveness** probe (`GET /health`, port 8000/8200) checks. `/health` returns
   `200 {}` *immediately* when the server starts — it does **not** wait for the model.
3. **Model loaded** — weights are loaded and the server can actually serve inference. This is
   what the **startup** and **readiness** probes (`GET /v1/models`) check. `/v1/models` returns
   `503` (or connection-refused) while weights load, and `200` with a model list once ready.

So a Running pod with `/health` 200 but `/v1/models` 503 is **not a bug** — it is mid-load.
That is exactly the case a pod-presence check would wrongly pass.

### Ports by role (the gate probes both)

| Role | Port | Notes |
| --- | --- | --- |
| Prefill / Standalone | 8000 | Direct vLLM API |
| Decode | 8200 | Proxied through a sidecar (8200 → 8000) |

`serving_readiness.roles` is inferred from the live pod container ports; the probes try 8000
then 8200, so a decode-only pod is still reached.

## The startup budget (how long "still loading" is legitimate)

The recommended `startupProbe` on `/v1/models` is generous on purpose — large models download
and load slowly:

```
failureThreshold: 60
periodSeconds: 30     # (some manifests use 10)
initialDelaySeconds: 15–30
```

So the legitimate startup window is `failureThreshold * periodSeconds`:

- `60 * 30s` = **30 minutes** (the documented default budget)
- `60 * 10s` = **10 minutes** (the wide-ep decode example)

**Read it from `serving_readiness.youngest_age_seconds`.** A pod younger than the startup
budget that is loading weights is *expected* to be NotReady. Only once age exceeds the budget
(and `/v1/models` is still 503) has the budget been *exhausted* — treat that as a problem
(model too big for `failureThreshold`, slow/failed download, or under-resourced).

## Reading the facts → the verdict

`serving_readiness` carries: `pods[]` (each with `phase`, `ready_condition`,
`containers_ready_condition`, `restart_count`, `age_seconds`, `role`), `max_restart_count`,
`youngest_age_seconds`, `roles`, and the probe results `health_status_code` /
`health_reachable` and `models_status_code` / `models_reachable`.

Decide as follows (judgment — adapt to the specifics, do not hard-code):

### Verdict A — "still loading weights" → KEEP WAITING

All of:

- Pod `phase` is `Running` and `restart_count` is **low** (0–1).
- `/health` is reachable and `200` (the server process is alive).
- `/v1/models` is **503** (or briefly connection-refused right at boot).
- `youngest_age_seconds` is **within** the startup budget (`failureThreshold * periodSeconds`,
  default ~30 min).

Meaning: the API server is up and the model is still loading. This is legitimate — the
startup probe is doing its job. **Recommend waiting** (re-check in ~30–60s; surface remaining
budget, e.g. "loaded for 4 min of a ~30 min budget"). Do NOT submit a benchmark yet; do NOT
recommend a standup/restart. You may suggest `kubectl logs ... | grep -i "loading model"` to
confirm progress.

### Verdict B — "wedged / broken" → STOP WAITING

Any of:

- `/health` is **unreachable / connection-refused** (`health_reachable` false) — the server
  process itself is down or never came up.
- `max_restart_count` is **high** / climbing (crash-looping — e.g. OOMKilled on load, a CUDA
  error, a wrong port in the probe).
- `youngest_age_seconds` **exceeds** the startup budget and `/v1/models` is still 503/refused
  (the budget is exhausted — the model will not finish loading as configured).
- `phase` is not `Running` (e.g. `Pending` unschedulable, `CrashLoopBackOff`).

Meaning: waiting longer will not help. **Stop waiting.** Tell the user *why* (crash-loop,
liveness down, budget exhausted, unschedulable) and recommend the matching action — inspect
logs/events (`kubectl logs`, `kubectl get events`), check resources (CPU/mem/GPU), verify the
probe port matches the serving port, or tear down + re-standup. None of these are run for the
user automatically; standup/teardown are mutating and need explicit approval.

The gap here is the STACK, not your toolchain: do NOT preemptively build the benchmark venv
(`run_setup` / `install.sh`) or run other prep off the back of a not-ready endpoint — a readiness
check never established that need. Address the checked gap (offer a standup/wait), nothing else.

### Verdict C — "serving-ready"

If the endpoint gate already returned `ready: true` (`/v1/models` 200 with a model list and a
ready backing endpoint), the stack is serving — proceed to benchmark. (`serving_readiness` is
only populated for the NotReady case.)

## One-line summary

`/health` 200 + `/v1/models` 503 + young pod + low restarts = **still loading, keep waiting**.
`/health` refused, OR high restartCount, OR age past the `failureThreshold*periodSeconds`
budget = **wedged/broken, stop**. Both 200 = **serving-ready, go**.
