# Structured logging & correlation IDs (operator/agent reference)

The backend emits **structured logs** so a single benchmark run can be traced after the
fact. This file is the *interpretation* guidance (thick agent); the *mechanism* lives in
`app/observability/logging.py` (which also carries the per-turn correlation context) and is wired once at startup.

## What you get
- **One JSON object per line** (newline-delimited JSON) when `LOG_FORMAT=json` (the default).
  Every line has the standard keys `timestamp` (UTC ISO-8601, ms), `level`, `logger`,
  `message`, plus any structured `extra` fields the call site attached.
- A compact **text** line when `LOG_FORMAT=text` (local dev): `ts level logger [corr_id] message`.
- `LOG_LEVEL` (default `INFO`) is a stdlib level name (`DEBUG`/`INFO`/`WARNING`/...).

## Correlation fields
A fresh **`corr_id`** is minted at the WebSocket boundary — one per connection/turn — and
bound via `contextvars`. Because `asyncio.create_task` snapshots the current context, the
`corr_id` (and `session_id`) automatically ride into:
- the agent loop (`turn.start` / `tool.call.start` / `tool.call.result` / `turn.end`),
- every tool dispatch (the loop binds `tool=<name>` for the duration of the call),
- the command runner and the command-exec record (`command.exec`, `runner.exec.*`).

So to trace one turn end-to-end: **grep the logs for its `corr_id`**. To trace one chat
across turns: filter on `session_id`. To see only what a given tool did: add `tool=`.

The fields are *omitted* from a JSON line when unset (no bogus empty values), so a startup
line carries no `corr_id` and that is correct.

## Key event messages (the `message` field)
| message | where | notable extras |
|---|---|---|
| `startup` | lifespan | `log_format`, `provider` |
| `turn.start` / `turn.end` | loop | `session_id`, `tool_calls` (end), `user_chars` (start) |
| `tool.call.start` / `tool.call.result` | loop | `tool_call_id`, `ok` (result) |
| `command.exec` | tool context | `exe`, `mode` (read_only/mutating), `auto_run`, `duration_s`, `exit_code`, `timed_out` |
| `runner.exec.start` / `runner.exec.timeout` / `runner.exec.launch_failed` | runner | `exe`, `cwd`/`deadline_s` |

## What is NOT logged
Secrets never reach the logs: the command-exec record carries `exe` (argv[0]) only, never the
full argv or environment, and the runner already scrubs secrets from the child env. There is
**no decision logic** in the logging code — it is pure plumbing; what an operator *does* with
a correlated trail (declare a regression, retry a run) is judgment, not code.
