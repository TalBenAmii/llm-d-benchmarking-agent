# Allowlist governance: per-command timeouts & usage quotas (agent reference)

Execution **limits are policy DATA** — they live in `security/allowlist.yaml`, never in
Python. The code is pure mechanism (the command runner enforces a deadline; a per-session
counter tallies uses); every *number* is a reviewed edit to the YAML. This file is the
*judgment* (thick agent) about how to react when a limit bites; the mechanism is in
`app/security/runner.py` (timeouts), `app/security/quota.py` + `app/tools/context.py`
(quotas), and the loader/validator in `app/security/allowlist.py`.

## Two fields, both optional, on an executable AND/OR a subcommand
- **`timeout_s: <int>`** — the per-command execution deadline (seconds). The runner kills the
  process (its whole process group) at the deadline and the result is flagged `timed_out`. A
  subcommand's `timeout_s` overrides the executable's; when neither is declared, the runner's
  sane global default applies. This is the **single** source of timeouts — there is no Python
  per-command timeout table.
- **`quota: { per_session: <int>, per_day: <int> }`** — usage caps. A command whose next use
  would exceed either cap is **refused before execution** (and before any approval prompt)
  with a structured quota error. `per_session` counts for the life of one chat session;
  `per_day` counts per UTC calendar day. Declare at least one key.

Both are **schema-validated at startup**: a non-positive / non-int `timeout_s`, a non-mapping
or empty `quota`, or an unknown quota key **rejects the whole allowlist with a clear error**.
Fail loud — never silently mis-enforce.

## How to react (judgment, not code)
- **A command times out** (`timed_out: true`): the deadline in the YAML was hit. For a heavy
  step (standup / run / experiment) this usually means the work genuinely didn't finish (slow
  host, image pull, model load) — relay that, and consider whether a smaller workload / spec
  fits. Do NOT silently retry the same heavy command in a loop. If the limit is too tight for
  a legitimate slow environment, the fix is a reviewed edit to `timeout_s` in the YAML.
- **A command is over-quota** (`quota_exceeded: true` with `key` / `window` / `cap` / `used`):
  the session (or day) hit its configured ceiling for that command. Tell the user the limit
  was reached, what the cap is, and ask whether to wait (per-day windows reset at UTC
  midnight) or adjust the plan (e.g. fewer benchmark runs). Don't try to route around it.

## Why this lives in data
Tuning a timeout or a usage cap must NOT require a code change or a redeploy of logic — it is
a policy decision, reviewable as a one-line diff to `security/allowlist.yaml`. The Python only
counts, compares, and kills; the judgment ("how many runs is too many", "how long is too
long") is data you can edit.
