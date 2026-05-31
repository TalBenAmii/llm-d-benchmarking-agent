# Preconditions & the "don't redeploy" rule

Always call `probe_environment` before proposing a plan or doing anything. Read the
structured result and reason about it — do not assume.

## What the signals mean
- `container_runtime.daemon_up == false` → the user must start Docker/Podman. If
  `socket_permission_error == true`, it's a permissions issue (docker group / rootless),
  not a "down" daemon. Explain the fix; never try `sudo` — it's not allowlisted.
- `repos.<name>.present == false` → clone with `ensure_repos` before anything else.
- `tools.<x> == false` → that tool is missing from PATH. `run_setup` (install.sh) installs
  most of them; for a container runtime the user must install it themselves.
- `venv.exists == false` → run `run_setup` before any `llmdbenchmark` command.
- `kind_clusters.clusters` non-empty → a local cluster exists; it may host a stack.
- `kube_context` / `cluster_info.reachable` → whether kubectl can talk to a cluster.

## The redeploy decision (important)
Before standing up anything, decide using `stack` (pass the target `namespace`):
- `stack.detected == true` (ready pods exist) → a stack is ALREADY running. Do **not**
  redeploy. Tell the user what's running and offer to:
  (a) benchmark the existing stack (go straight to `run`), or
  (b) tear it down and redeploy fresh (requires approval).
- `stack.exists == true` but `ready_count == 0` (pods Pending/CrashLoop/Error) → the stack
  is **stale**. Surface the pod states, and offer a gated `teardown` then a fresh standup.
- `stack.exists == false` → safe to deploy.

When unsure whether the running stack matches the use case, you can also preview endpoints
read-only with `execute_llmdbenchmark subcommand=run flags={list_endpoints:true}`.
