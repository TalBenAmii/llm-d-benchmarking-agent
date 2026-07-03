# Right-sizing the harness launcher CPU request (`LLMDBENCH_HARNESS_CPU_NR`)

This is the JUDGMENT for the `harness_cpu_nr` flag of `execute_llmdbenchmark`. The tool is
pure mechanism: when you supply `flags.harness_cpu_nr`, it sets the **backend-only env var**
`LLMDBENCH_HARNESS_CPU_NR` on the `llmdbenchmark` subprocess (never a CLI flag, never sent to
the browser). **You** decide whether to lower it and to what — this file is how. Omit it and
the harness keeps its default, so nothing changes for clusters that already fit.

## The problem it solves

`llm-d-benchmark` requests **16 CPUs** by default for the benchmark *launcher* pod
(`llmdbench-${HARNESS_NAME}-launcher`), via `LLMDBENCH_HARNESS_CPU_NR` (default `16` — see the
benchmark repo's `docs/run.md` "Use" table and `docs/resource_requirements.md`). A
single-node **Kind** cluster usually has far fewer allocatable cores than that. When the
request exceeds what any node can schedule, the launcher pod sits in **`Pending` with
`FailedScheduling` ("Insufficient cpu")** — and to a non-expert this looks like the benchmark
silently hanging. Lowering the request to what the node can actually give turns that opaque
hang into a scheduled, successful run.

> This is *only* the launcher's CPU request — the pod that drives load. It does not change the
> model server's resources or the measured system under test. On a Kind/CPU-sim MVP run it
> only affects whether the load generator gets scheduled.

## When to lower it (the decision)

1. **Read the node capacity first.** Call `probe_environment` with the `node_capacity` check
   (or `"all"`). It returns each node's `allocatable_cpu` plus `min_allocatable_cpu` (the
   binding constraint — the scheduler places the launcher on a *single* node, so the smallest
   allocatable node CPU is what matters).
2. **If `min_allocatable_cpu >= 16`** (a real multi-core node / large cluster): **do nothing.**
   Omit `harness_cpu_nr` and let the default 16 stand. Setting it here would only *throttle*
   the load generator for no reason.
3. **If `min_allocatable_cpu < 16`** (typical Kind / laptop / small CI node): **lower it.** Pick
   the value below, then pass it as `flags.harness_cpu_nr`.
4. **If `node_capacity` is unavailable** (no `kubectl`, cluster unreachable, `available:false`):
   don't guess a number blindly. Prefer probing again once the cluster is up; if you must
   proceed on a known Kind cluster, treat it as a small node and use the conservative Kind
   value below — but say so.

## What value to pick (harness-aware — this is the headline)

The right floor depends on the harness, because they generate load differently
(`docs/resource_requirements.md`):

- **`inference-perf` (and other multi-process harnesses)** spread load across worker
  processes — *more vCPUs ⇒ higher achievable concurrency and load*. It wants **headroom**.
  Give it as many cores as the node can spare while leaving room for the rest of the pod /
  kubelet — a good rule is **`min_allocatable_cpu - 1`, clamped to at least 2** (e.g. a 4-core
  Kind node ⇒ `3`; an 8-core node ⇒ `7`). Going too low here directly caps the concurrency the
  benchmark can reach.
- **`vllm-benchmark` (and other single-process harnesses)** are bound by the asyncio threads
  one process can drive, not by core count — *CPU **clock speed** helps more than core count*.
  It tolerates a **smaller** request: **2** is usually enough on a small node, and even **1**
  will schedule and run (slower per-request handling under high concurrency). Prefer **2** so
  the launcher isn't CPU-starved, but **1** is the last-resort value that still fits a
  1-core node.

A simple, safe default when unsure of the harness on a small node: **`min(node-1, 4)` clamped
to ≥2** — enough for either harness to schedule, biased toward inference-perf's need for
headroom without over-requesting on a tiny node.

### Worked examples

| Node allocatable CPU | Harness | `harness_cpu_nr` to pass | Why |
|---|---|---|---|
| ≥ 16 | any | *(omit — default 16)* | the default already fits; don't throttle |
| 8 | inference-perf | `7` | leave ~1 core headroom; maximize concurrency |
| 4 | inference-perf | `3` | small Kind node; still give it the cores it has |
| 4 | vllm-benchmark | `2` | single-process; 2 is plenty, clock-bound anyway |
| 2 | vllm-benchmark | `2` | fits exactly; schedules and runs |
| 1 | vllm-benchmark | `1` | last resort — schedules on a 1-core node |
| 1 | inference-perf | `1` | will schedule but concurrency is very limited — warn the user it's underpowered |

## What to tell the user

- When you lower it, say so plainly: *"Your cluster's node has only N CPUs and the benchmark
  launcher asks for 16 by default, which would leave it stuck Pending. I've set its CPU request
  to M so it can actually schedule and run."*
- When you keep the default, no need to mention it.
- When you had to pick `1` for a multi-process harness, warn that load/concurrency will be
  limited by the underpowered node — the numbers are valid but not headroom-rich.

## Boundaries

- **Two launcher-resource knobs `execute_llmdbenchmark` exposes:** `harness_cpu_nr` (CPU request,
  `LLMDBENCH_HARNESS_CPU_NR`, default `16`) and `harness_mem` (MEMORY request,
  `LLMDBENCH_HARNESS_CPU_MEM`, default `32Gi`, per `docs/run.md`). Both are backend-only ENV VARS
  (never CLI flags, never in the allowlist or a `command` event). `harness_mem` takes a Kubernetes
  memory quantity (`48Gi`, `512Mi`) — validated at the tool boundary, so a typo is a clean error,
  not a late pod-apply failure. **If a launcher OOMs, RAISE `harness_mem`** (e.g. `48Gi`/`64Gi`);
  lower it on a tiny node. Same headroom split as CPU: the multi-process `inference-perf` launcher
  needs more than single-process `vllm-benchmark`.
- The value is a backend env var, not an argv flag — it is never in the allowlist and never in
  a `command` event, so it stays off the browser/UI surface entirely.
- Repos are read-only; this is a runtime decision, not a repo edit.
