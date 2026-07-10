# Troubleshooting

A symptom-first guide to the common failures, **where to look**, and how to use the agent's
structured logs (Phase 11) and its readiness/metrics endpoints to diagnose them. Nothing here
invents a feature — every endpoint, env var, and log key below exists in the code.

## First moves (always)

1. **Hit the health probes.**
   - `GET /healthz` → `{"status":"ok"}` means the process is **live**.
   - `GET /readyz` → **200** when the startup self-check passed, **503** with structured
     reasons when it did not (workspace writable, provider coherent, repos resolvable, auth
     coherent). The 503 body tells you *which* check failed — start there.
2. **Read the structured logs.** With `LOG_FORMAT=json` (the default) every line is one JSON
   object with `timestamp`, `level`, `logger`, `message`, plus structured `extra` fields. The
   correlation fields are the key to navigation (`knowledge/logging.md`):
   - **one turn** → grep its `corr_id` (minted at the WebSocket handshake, one per connection/turn);
   - **one chat across turns** → filter on `session_id`;
   - **one tool's activity** → add `tool=<name>`;
   - **one orchestrated run** → filter on `run_id`.
3. **Turn up the volume.** Set `LOG_LEVEL=DEBUG` for the backend; use `LOG_FORMAT=text` for a
   compact human line (`ts level logger [corr_id] message`) during local dev.

## Debug mode (UI)

The chat UI has a **Debug toggle** (`>_`) that reveals the full **executed-command trail**
*inline in the chat* — every command the agent ran (read-only probes included) appears in
place, between the messages, in execution order, each badged read-only/mutating and
auto/approved. Toggling it off hides the commands again; the setting persists. Use it to
answer *"what did the agent actually run?"* without leaving the conversation. The inline
trail is replayed in its original transcript position on reconnect/resume.

## Symptom → what to check

### The agent connects but never responds / no LLM output
- **No API key.** A real session needs `ANTHROPIC_API_KEY` (or an OpenAI-compatible key) in
  `.env`. `GET /readyz` reports `provider_coherent: false` when the configured provider has no
  key. Tests run a fake provider, so green tests do **not** imply a configured key.
- **Wrong provider/model.** `LLM_PROVIDER` must be `anthropic` or `openai`; an unknown value
  fails the self-check with `unknown LLM_PROVIDER`.
- Check the logs for `turn.start` without a matching `turn.end` on the same `corr_id` — that
  localizes a hang to the LLM call vs a tool.

### A command "isn't allowed" / is denied
- The allowlist (`security/allowlist.yaml`) is **deny-by-default**. A denial means the
  executable, a subcommand, a flag value, or a token failed validation. The denial `reason`
  appears in the surfaced error and the log. **Fix by widening the YAML policy — never by
  editing Python.** See `docs/SECURITY.md` for the model.
- A token containing a shell metacharacter is rejected on principle (defense in depth), even
  though the runner uses `shell=False`.

### A mutating command never runs
- Mutating commands **require explicit UI approval** (an `approval` frame over `/ws`). If you
  approve after disconnecting, the approval is auto-rejected (a detached turn won't hang holding
  a concurrency slot — Phase 2). Reconnect and re-drive.

### "venv not set up" / `llmdbenchmark` not found
- The benchmark CLI lives in the **benchmark repo's own `.venv`** built by its `install.sh`.
  The runner raises `… not found — the benchmark venv is not set up yet (run install.sh first)`.
  Have the agent run the bootstrap (`install.sh --uv`) first, or set up the venv manually.

### `kind create cluster` fails on a fresh host ("could not find a log line … Multi-User System")
- The kind node's systemd can't boot because the host's inotify limits are too low (node logs show
  `Failed to allocate directory watch: Too many open files`). The repo-root `install.sh` now raises
  `fs.inotify.max_user_watches`/`max_user_instances` automatically (persisted to
  `/etc/sysctl.d/99-inotify-kind.conf`) before creating the cluster. Creating a cluster by hand? Bump
  those limits first (best-effort `sudo sysctl -w …`), then retry `kind create cluster`.

### Repos not found / catalog or report tools fail
- The two read-only sibling repos (`llm-d/`, `llm-d-benchmark/`) must resolve under
  `REPOS_DIR` (defaults to the parent of the project dir). `GET /readyz` reports
  `repos_resolvable: false` with `missing repo(s): …` when they don't. Set `REPOS_DIR` to the
  directory that contains them. (In a bare git worktree the nested sibling repos can be empty —
  point `REPOS_DIR` at a populated checkout.)

### A command hangs, then is killed
- Every command has a deadline (`Decision.timeout_s` from the allowlist YAML, else a sane
  default). On timeout the runner logs `runner.exec.timeout` with `deadline_s` and SIGKILLs the
  child's **whole process group**. The result carries `timed_out: true`. If a class of command
  legitimately needs longer, raise its `timeout_s` in `security/allowlist.yaml` (data, not code).

### A benchmark run failed
- Don't scrape logs for the verdict — read the **metrics** (`knowledge/observability.md`):
  - `llmdbench_orchestrator_runs_terminal_total{outcome}` — `succeeded` vs `dead_lettered`.
  - `llmdbench_orchestrator_run_faults_total{kind}` — the dominant fault
    (`oom`/`timeout`/`unschedulable`/`evicted`/`image_error`/`run_error`/`unknown`).
- For `oom`/`unschedulable`/`evicted`, use the read-only `observe_run_metrics` tool
  (`scope="nodes"` for node pressure, or `scope="pods"` for the run's pods) to confirm resource
  pressure. The remediation judgment lives in `knowledge/orchestrator.md`.
- The orchestrator refuses to submit a Job when `ORCHESTRATOR_IMAGE` is unset (it would be
  unrunnable). For the **local** path, `execute_llmdbenchmark` runs the harness directly.

### Capacity pre-flight says it won't fit
- `check_capacity` runs the **benchmark repo's own** planner (weights + activation + KV-cache
  vs accelerator memory). A shortfall under `enforce=true` is a deployment-halting ERROR; else
  an advisory WARNING. Interpretation guidance: `knowledge/capacity.md`.

### Metrics are missing or reset to zero
- The agent's counters are **process-lifetime** — they reset on a backend restart, so a sudden
  return to zero usually means a restart, not lost data. Confirm with the `AgentDown` alert /
  the `up` series. If `/metrics` is empty of the `llmdbench_*` families, no commands/runs have
  happened yet since startup.

## Where each signal lives (cheat sheet)

| Question | Source |
|---|---|
| Is the process live? | `GET /healthz` |
| Is it configured correctly to serve? | `GET /readyz` (200/503 + per-check reasons) |
| What did the agent run? | UI Debug toggle; logs `command.exec` (filter `corr_id`) |
| Why did a run fail? | `/metrics`: `…runs_terminal_total`, `…run_faults_total{kind}` |
| Is anything running now? | `/metrics`: `llmdbench_orchestrator_runs_in_flight` |
| Live cluster resource pressure | `observe_run_metrics` tool (`kubectl top`) |
| Alerting on the above | `deploy/observability/alerts.rules.yaml` |
