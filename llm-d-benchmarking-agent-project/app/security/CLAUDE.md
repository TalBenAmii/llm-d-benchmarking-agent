# app/security/ — the command allowlist (deny-by-default)

`allowlist.py` is a **pure validator**: it matches a logical argv list against the policy
**data** in `security/allowlist.yaml` and returns a `Decision(allowed, mode, reason, timeout_s,
quota, …)`. `runner.py` executes with `shell=False`, a scrubbed env, and the policy's timeout.

**Scope:** the allowlist governs the **DEDICATED command tools** (execute_llmdbenchmark, the
probes, the orchestrator) via `ctx.run_command`/`ctx.run_readonly` → `CommandExecutor`. It does
**NOT** restrict the agent's ad-hoc `run_shell` tool (arbitrary `bash -lc`), which is gated by the
read-only/mutating classifier + approval instead (`app/tools/shell.py`). The runner + its
env-scrubbing (below) are shared by BOTH paths, so API keys stay out of every subprocess.

## The contract (never break it)
- **`shell=False`, always** (`runner.py`): commands run as positional argv via
  `create_subprocess_exec(*argv)` — never a shell string. Metacharacter screening in the validator
  (`_DANGEROUS`) is defense-in-depth, not the primary protection.
- **No per-command knowledge in Python.** `allowlist.py` has **zero** `if exe == "..."` / `if sub ==
  "..."` branches — validation is uniform over the YAML shape (executables → subcommands → flags →
  positionals → value_constraints). If you're tempted to add a Python branch for one command, you're
  doing it wrong: encode it in the YAML.
- **Fail-closed.** Unknown executable/subcommand, empty/malformed argv, or any failed check → `_deny()`.
  `default: deny` is set in the YAML.
- **Env scrubbing** (`runner.py` `_ENV_PASSTHROUGH` + `LLMDBENCH_*`): the child sees only allowlisted
  vars. **Never add API keys / tokens** to the passthrough — secrets reach a child only via explicit
  `extra_env`, never argv, never emitted events.
- **Governance is data**: `timeout_s` and `quota{per_session,per_day}` live in the YAML, validated at
  load. A YAML `timeout_s` **overrides** any caller timeout. Quota is checked **before** approval.
- **Read-only auto-runs, mutating needs approval** (`requires_approval = allowed and mode == MUTATING`).
  A flag with `read_only_trigger: true` (e.g. `--dry-run`) downgrades an otherwise-mutating command.

## To widen capability: edit `security/allowlist.yaml` ONLY (no Python change)
Add the executable (`flat: true` for a simple tool, else `subcommands:`), set `mode:` (read_only |
mutating), constrain **every** user/LLM-influenced value (`value: {ref|enum|regex|ref_catalog|any_of}`),
optionally add `timeout_s` / `quota`, and add a test case in `tests/test_allowlist.py`. Worked examples
already in the file: `kind create/delete cluster`, `install_prereqs.sh`, `llmdbenchmark` subcommands.

## Gotchas
- Glob/wildcard chars (`*`, `?`, `[`) are shell metacharacters and are **rejected** by the screen — use exact values / regex, not shell syntax.
- A `repeated: true` positional must be **last** (it swallows following tokens); the loader rejects otherwise.
- Subcommand-level `timeout_s`/`quota` overrides the executable-level value.

## Key files
- `allowlist.py` — the pure validator (`validate()` → `Decision`).
- `runner.py` — subprocess executor (path resolve, env scrub, `shell=False`, timeout, process-group reap).
- `quota.py` — per-session/per-day counter (mechanism; caps come from the Decision).
- `auth.py` — optional Bearer auth + rate limiter (off by default).
- `../../security/allowlist.yaml` — **the single source of truth** (the policy data).

## Scoped tests
```bash
pytest tests/test_allowlist.py tests/test_governance.py
```
