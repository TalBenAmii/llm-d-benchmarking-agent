# Preconditions & the "don't redeploy" rule

Always call `probe_environment` before proposing a plan or doing anything. Read the
structured result and reason about it — do not assume.

## What the signals mean
- `container_runtime.daemon_up == false` → Docker/Podman is installed but not running. If
  `socket_permission_error == true`, it's a permissions issue (docker group / rootless),
  not a "down" daemon — explain the fix. To (re)start the Docker daemon you may run
  `run_command argv=["install_prereqs.sh","--docker"]` (it skips the install if docker is
  already present and just tries to start it); on WSL/Docker-Desktop it may still need to
  be started manually. Never run raw `sudo` — only the pinned `install_prereqs.sh` is
  allowlisted.
- `repos.<name>.present == false` → clone with `ensure_repos` before anything else.
- `tools.<x> == false` → that tool is missing from PATH. `run_setup` (install.sh) installs
  most of them (kubectl, helm, helmfile, jq, yq, kustomize, skopeo, crane, and uv→Python
  3.11). It does NOT install `docker` or `kind` — but you can, with the vetted installer:
  - `tools.docker == false` (and no podman) → install Docker Engine with
    `run_command argv=["install_prereqs.sh","--docker"]` (mutating; needs root or
    passwordless sudo). Relay any warning the installer prints (e.g. daemon couldn't
    auto-start, or the user must re-login for docker-group membership).
  - `tools.kind == false` → install the kind binary with
    `run_command argv=["install_prereqs.sh","--kind"]` (combine as `--docker --kind` or
    `--all` to do both at once).
- `venv.exists == false` → run `run_setup` before any `llmdbenchmark` command.
- `kind_clusters.clusters` empty (but `kind` is present) → no local cluster yet. For the
  quickstart, create one yourself: `run_command argv=["kind","create","cluster","--name","llmd-quickstart"]`.
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
