# Contributing

This project has two non-negotiable laws. Internalize them before writing code — almost every
review comment traces back to one of them.

## The two laws

### 1. Thin code, thick agent
**Python is mechanism; judgment lives in the LLM + `knowledge/`.** Do **not** put decision
logic (which spec/harness/workload, what flags, how to read a result, what to do about a fault)
in Python `if/elif` branches. Put the *facts and reasoning* in an editable `knowledge/*.md`
file and let the LLM reason over it. The Python layer only: serves the UI, runs the agent loop,
dispatches tools, validates I/O against schemas, enforces the security allowlist, and shells
out commands. If you find yourself encoding a *choice* in code, stop — that belongs in
`knowledge/`.

### 2. Allowlist-as-data (security)
**The security policy is DATA (`security/allowlist.yaml`); `app/security/allowlist.py` is a
pure validator with no per-command knowledge.** You **widen what the agent can run by editing
the YAML, never by adding a per-command branch in Python.** Commands run as argv lists with
`shell=False`; read-only probes auto-run, mutating commands require explicit approval. See
`docs/SECURITY.md` for the full model.

A change that adds a Python special-case for one command, or hard-codes a benchmarking
decision, will be rejected on principle even if it "works."

## The hermetic-test rule

**Every test must be hermetic: no live cluster, no GPU, no network, no long real runs.** The
suite must pass with no API key, no Docker, no kind, no kubectl-reachable cluster. Use the
existing fakes instead of touching real resources:

- **`FakeKubeClient`** (`tests/orchestrator_fakes.py`) — a scripted Kubernetes client for the
  orchestrator (Job lifecycle, phases, logs) with no `kubectl`.
- **`CaptureRunner`** / capturing runners — record the argv that *would* have run instead of
  executing it, so you assert the agent runs the **right** command without running it.
- **A fake LLM provider** — drives the agent loop deterministically (scripted tool calls), so
  no real model is called.
- **The `tests/test_ws.py` TestClient harness** — exercises the real `/ws` + HTTP surface
  in-process (FastAPI `TestClient`), no server/socket.
- **Fake/injected clocks** — the rate limiter and any time-based logic take an injectable
  monotonic clock; never `sleep`.
- **`tests/conftest.py` fixtures** — `tool_ctx`, `allowlist`, `catalog`, `bench_repo`, the BR
  v0.2 schema/example, etc.

Read-only commands against the *real* sibling repos on disk are fine (e.g. a `git status -s`,
parsing a real on-disk Benchmark Report) — those are fast, offline, and deterministic. What is
banned is anything that needs a cluster, a GPU, the network, or minutes of wall-clock.

**Do not skip-to-pass.** A test that `xfail`s, `skip`s unconditionally, or asserts nothing
meaningful is not coverage. (Skipping an *optional external binary* check when the binary is
absent — like `helm`/`kustomize`/`promtool` — is the one acceptable skip, because the hermetic
structural test already covers the contract.)

**The one opt-in integration exception (`tests/integration/`).** The proposal's explicit
"integration tests with `llm-d-inference-sim`" live here. The live test stands up the real
sim, so it is **gated** on `LLMD_SIM_INTEGRATION=1` *and* the sim being locatable — it SKIPS
cleanly by default (and never hangs reaching a server that isn't there). Crucially, its
**wiring is still covered hermetically**: a sim-shaped BR v0.2 fixture (built from the repo's
own example) is driven through the real analyze/compare tools in the default suite. So the
integration logic is tested even when the sim is absent. See `knowledge/sim_integration.md`.

Run the suite from your worktree:

```bash
REPOS_DIR=/path/to/repos PYTHONPATH="$PWD" pytest tests/ -q
```

`REPOS_DIR` must point at a directory containing populated `llm-d/` and `llm-d-benchmark/`
checkouts (the read-only siblings), or the catalog/report tests will fail.

## How to add a tool

A "tool" is a function the LLM can call. The mechanism is uniform; the judgment about *when* to
call it lives in the prompt + `knowledge/`.

1. **Define the input schema.** Add a Pydantic `…Input` model in the `app/tools/schemas/`
   package (the module for the tool's family, e.g. `schemas/execute.py`). This is determinism
   gate (a): the LLM's arguments are validated against it before your handler runs.
2. **Write the handler.** Add a function in a module under `app/tools/` taking
   `(input_model, ctx: ToolContext)`. Run commands **only** through `ctx.run_readonly` /
   `ctx.run_command` (which go through the allowlist + runner). Return a JSON-serializable dict
   of *facts* — no judgment, no prose verdicts the LLM should be forming.
3. **Register it.** Add a `ToolSpec(name, description, InputModel, handler)` to
   `build_registry()` in `app/tools/registry.py`, and a description in `_DESCRIPTIONS`.
4. **Allow any new commands as DATA.** If the tool needs a command not yet permitted, add it to
   `security/allowlist.yaml` with the right `mode` (read-only auto-runs; mutating is
   approval-gated) and any value constraints / `timeout_s` / `quota`. Never add a Python branch.
5. **Put the judgment in `knowledge/`.** Add or extend a `knowledge/*.md` so the LLM knows when
   and how to use the tool and how to interpret its facts.
6. **Test it hermetically.** Add tests using the fakes above. Update the expected tool-name set
   in `tests/test_schemas.py`.

## How to add a flow

A "flow" is an end-to-end conversation path (probe → plan → deploy → run → analyze). Flows are
validated by the harness in `tests/flows/` (see `docs/VALIDATION.md`): a scripted provider
drives the agent and the harness asserts the **right commands** were issued (captured, not
executed) and the right tools were called in order. Add a flow scenario there rather than a
bespoke integration test.

## How to add a phase

The build-out is organized into phases (per-phase history lives in git; remaining/deferred
phases are tracked in `FEATURES.md` (the DEFERRED phases), owned by the integrator). When implementing one:

1. Work on a dedicated branch/worktree off the integration branch — **never `main`**.
2. Keep changes scoped to the phase. Obey both laws above.
3. Add **meaningful hermetic tests** that cover the feature (no vacuous asserts).
4. Add a `CHANGELOG.md` entry (Keep-a-Changelog format) under **Unreleased**.
5. Run the full suite with a timeout (`timeout 600 pytest tests/ -q`) — an `exit 124` (hang)
   means a test reached a real resource; make it hermetic, do not skip it.
6. Document operator-facing judgment in `knowledge/`, not in code.

## Code style

- `from __future__ import annotations`; type-hint public functions.
- No new runtime dependency without an explicit reason — the dependency surface is deliberately
  small (the metrics exporter is hand-rolled rather than pulling `prometheus_client`, for
  example). Dev-only tooling is the exception.
- Secrets never reach the browser, child processes (LLM/auth keys), or logs. See
  `docs/SECURITY.md`.
- Fail loudly on a misconfiguration (e.g. a missing repo path, a malformed allowlist) rather
  than silently degrading.

## Quality gates (ruff + mypy + coverage)

Three CI-enforced gates keep the tree clean. Run them all locally with `make quality`
(or `make lint` / `make typecheck` / `make coverage` individually); CI runs the same three
in the `quality-gates` job (the hermetic flow-validation job runs unchanged alongside it).

- **`ruff check .`** — a sensible modern ruleset (`E/W/F/I/B/UP/C4/SIM`, configured in
  `[tool.ruff.lint]`). `E501` (line length) is **deliberately not enforced** — the codebase
  favours longer explanatory lines and enforcing it would be pure churn. Idiomatic test
  patterns (broad `assertRaises`, `assert False`, nested `with`) are relaxed for `tests/`.
- **`mypy app`** — meaningful but **not** `--strict` over the whole tree (that would demand a
  rewrite for no real benefit). Genuine `Optional`/arg-type bugs are flagged; the two LLM
  provider modules that hand dict-shaped payloads to the richly-typed `anthropic`/`openai`
  SDKs have `arg-type`/`call-overload` relaxed at that boundary only (`[[tool.mypy.overrides]]`).
  Prefer a precise annotation or a `TypeGuard` over a blanket `type: ignore`; targeted ignores
  must carry an error code.
- **coverage** — the full suite under `--cov=app`, gated by `--cov-fail-under`. The threshold
  lives in **one** place, `COV_FAIL_UNDER` in the `Makefile`, and is mirrored by the CI step
  and asserted by `tests/test_quality_gates.py` (so they can't drift). It is set a few points
  below the measured baseline (~89%), **not** a hardcoded 80%. Never delete a functional test
  to satisfy coverage — add a test or raise nothing.

The covered suite reads the real Benchmark Report schema/specs, so it needs the read-only
`llm-d` / `llm-d-benchmark` repos present (`REPOS_DIR` points at their parent); CI clones them
shallowly for that job.
