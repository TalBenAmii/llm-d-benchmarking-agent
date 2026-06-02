# API & Tool Reference

Two interfaces: the **HTTP/WebSocket API** the browser (or any client) speaks to the
backend, and the **agent tool surface** — the 18 schema-validated tools that are the LLM's
*entire* set of actions. The tool input schemas are defined in
[`app/tools/schemas.py`](../app/tools/schemas.py) (the single source of truth, emitted to
the LLM as JSON Schema); the registry + descriptions live in
[`app/tools/registry.py`](../app/tools/registry.py).

---

## HTTP endpoints (`app/main.py`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve the chat UI (`ui/index.html`). |
| `GET` | `/static/*` | Static UI assets. |
| `GET` | `/healthz` | **Liveness** (minimal): `{ok: true}` — process is up and serving; no dependency checks. K8s `livenessProbe` target. |
| `GET` | `/readyz` | **Readiness** (Phase 16): `{ready, self_check:{checks:[…]}}` with per-component status (provider configured, repos present, runner ok, workspace writable). `200` when ready, `503` when not. K8s `readinessProbe` target. |
| `GET` | `/metrics` | Prometheus text exposition of the agent + orchestrator metrics (content-type `text/plain; version=0.0.4`). Scrape target. |
| `GET` | `/api/sessions` | Recent chats for the sidebar (summaries, newest first). |
| `DELETE` | `/api/sessions/{id}` | Delete a saved chat; `404` if unknown. |
| `GET` | `/api/history?tag=&model=` | Stored historical results (summaries, newest first) + the list of trendable metrics. |
| `GET` | `/api/history/trend?metric=&tag=&model=` | Time-series of one metric across stored results (values + the metric's better-direction; no verdict). |

## WebSocket `/ws`

Connect to `/ws` for a fresh chat, or `/ws?session=<id>` to **resume** a saved one (an
unknown id mints a new session). All frames are JSON `{ "type": <event>, "data": {...} }`.

Every **inbound** frame is validated against an explicit schema (`app/agent/ws_schemas.py`)
before the handler acts on it. A malformed frame (non-object, unknown/missing `type`, wrong
field shape, or extra fields) is rejected with an `error` event of `kind: "protocol_error"`
and **the connection is kept alive** — a bad frame never crashes the handler.

If you **reconnect while a turn is still running** on that session (e.g. the socket dropped
mid-benchmark), the server replays the in-flight turn's buffered **live** events (a bounded
per-turn ring buffer) so the client catches up to the live stream — the events it missed, in
order — and then continues live, rather than waiting blind for only the final result.

**Server → client events** (`app/agent/events.py`):

| Event | Payload | Meaning |
|---|---|---|
| `ready` | `{session_id, resumed, running}` | Connection established; `running` flags a still-in-flight background turn. |
| `history` | `{items, commands}` | On resume: the transcript + the executed-command trail to replay. |
| `assistant_text` | `{text}` | A chat message from the agent. |
| `tool_call` | `{id, name, input}` | The agent invoked a tool. |
| `command` | `{argv, text, mode, auto_run}` | **Every** command actually executed (including auto-run read-only probes), emitted just before it runs. Powers the full trail + debug view. |
| `output` | `{line}` | A streamed line of command stdout/stderr. |
| `approval_request` | `{request_id, kind, payload}` | A mutating command (or a `session_plan`) needs Approve/Reject. |
| `tool_result` | `{id, name, result}` | A tool finished. |
| `session_plan` | `{plan}` | A proposed plan (also an approval request). |
| `error` | `{message[, kind]}` | A recoverable error. `kind: "protocol_error"` marks a rejected malformed inbound frame. |
| `cancelled` | `{message}` | The in-flight run/turn was cancelled (Phase 16); its concurrency slot is freed and its subprocess reaped. Followed by `done`. |
| `done` | `{}` | The agent finished this turn. |
| `pong` | `{}` | Reply to a `ping`. |

**Client → server messages:**

| Type | Payload | Meaning |
|---|---|---|
| `user_message` | `{text}` | The user's chat input (starts a turn). |
| `approval` | `{request_id, approved}` | Approve/Reject a pending command/plan. |
| `cancel` | `{}` | Cancel this chat's in-flight run (Phase 16): frees its concurrency slot, reaps its subprocess. Idempotent. |
| `ping` | `{}` | Keepalive. |

---

## Agent tools

Every tool call is validated against its Pydantic input model before the handler runs
(determinism gate **a**); invalid arguments are returned to the model so it can self-correct.
"Class" is the security classification:

- **read-only** — auto-runs, no approval prompt;
- **approve** — mutating, requires the user to click Approve;
- **gate** — mutating *unless* a preview flag (`--dry-run`/`plan`/`--list-endpoints`)
  downgrades it to read-only.

### Sense & ground (read-only)

| Tool | Key inputs | What it does |
|---|---|---|
| `probe_environment` | `checks` (or `"all"`), `namespace` | One structured snapshot: container runtime, repos present, toolchain, venv, kind clusters, kube context/reachability, namespaces, and whether a stack is already running. **Always called first.** |
| `list_catalog` | `kinds`, `refresh` | Enumerate the valid specs / harnesses / workloads / scenarios that *actually exist* in the repo on disk, so the LLM can never name something invalid. |
| `read_repo_doc` | `path`, `max_bytes` | Read one doc/spec file from inside a read-only repo (path must resolve inside a repo; `..` blocked). |
| `fetch_key_docs` | `task`, `max_bytes_each` | Fetch the **live** content of the authoritative docs pinned in `knowledge/key_docs.yaml`, filtered by task. Called to ground the agent in the real procedure before planning a deploy. |

### Plan & pre-flight

| Tool | Class | Key inputs | What it does |
|---|---|---|---|
| `propose_session_plan` | approve | the `SessionPlan` (below) | Propose the structured, user-approved contract before any mutation. Enum fields checked against the live catalog. |
| `check_capacity` | read-only | `spec`, `overrides`, `enforce` | Capacity pre-flight ("will this fit?") via the benchmark repo's own planner. `enforce=True` tags shortfalls as deployment-halting errors. Interpreted with `knowledge/capacity.md`. |

### Prepare (mutating)

| Tool | Class | Key inputs | What it does |
|---|---|---|---|
| `ensure_repos` | approve | `repos`, `ref` | Clone `llm-d-benchmark`/`llm-d` if missing (URL-allowlisted; idempotent; never overwrites). |
| `run_setup` | approve | `use_uv`, `force` | Run `install.sh` in the benchmark repo to build its venv + verify tools. Required before any `llmdbenchmark` command. |
| `write_and_validate_config` | approve | `artifact_type`, `target_filename`, `content` | Write a generated workload/run config into the session workspace and validate it. (MVP uses stock profiles; rarely needed.) |

### Execute & orchestrate

| Tool | Class | Key inputs | What it does |
|---|---|---|---|
| `run_command` | gate | `argv`, `timeout` | Run any *other* allowlisted command given as an argv list — notably `kind create/delete cluster` and `install_prereqs.sh` (Docker + the kind binary). Prefer a dedicated tool when one fits. |
| `execute_llmdbenchmark` | gate | `subcommand` (`plan`/`standup`/`smoketest`/`run`/`teardown`/`results`/`experiment`), `spec`, `namespace`, `harness`, `workload`, `flags`, `extra` | The single local CLI runner. `plan`/`--dry-run`/`--list-endpoints` auto-run; `standup`/`run`/`teardown`/`experiment` require approval. `experiment` runs a full DoE sweep. |
| `orchestrate_benchmark_run` | approve | `namespace`, `spec`, `harness`, `workload`, `image`, `service_account`, `cpu`, `memory`, `active_deadline_seconds`, `max_attempts`, `watch`, `poll_interval`, `max_wait` | Run a benchmark as a **Kubernetes Job** the orchestrator manages end-to-end: submit → watch → stream logs → classify failures (OOM/timeout/eviction/unschedulable/image/run-error). Transient faults retry as fresh Jobs; deterministic faults never retry. Needs an orchestrator image. |

### Observe, parse & analyze (read-only)

| Tool | Key inputs | What it does |
|---|---|---|
| `observe_run_metrics` | `namespace`, `scope` (`pods`/`nodes`), `run_id`, `containers` | Live cluster CPU/memory via `kubectl top` *while a run is in flight* — a leading indicator of OOM/throttle. Requires the in-cluster metrics-server. (Distinct from `/metrics`.) |
| `locate_and_parse_report` | `results_dir`, `session_id` | Find the newest Benchmark Report from a completed run, validate it against the repo schema, and return a plain-language metric summary. |
| `compare_reports` | `sources` / `experiment_dir`, `labels`, `baseline_index` | Compare 2+ reports of the **same** harness side by side (an A/B, or a whole DoE sweep): per-metric deltas vs a baseline + the winner per metric. |
| `compare_harness_runs` | `sources` (2+), `labels` | Cross-harness comparison: contrast reports from **different** harnesses (e.g. inference-perf SLO vs guidellm throughput) against the same stack. Reports which metrics ≥2 harnesses both measured (cross-validate) with **no** cross-harness winner. Interpreted with `knowledge/multi_harness.md`. |
| `analyze_results` | `slo`, `sources` / `experiment_dir`, `labels` | SLO-aware filtering + goodput estimate + Pareto/DoE frontier. Pass the `SLOTargets` from the approved plan. Returns per-run SLO verdict + goodput estimate; for a sweep, the Pareto-optimal configs + SLO-feasible frontier. |

### History (read-only — never touches the cluster)

| Tool | Key inputs | What it does |
|---|---|---|
| `result_history` | `action` (`store`/`list`/`get`/`trend`/`delete`), `source`, `label`, `tags`, `spec`/`harness`/`workload`/`namespace`/`session_id`, `record_id`, `metric`, `filter_tag`/`filter_model` | Persist a validated report's summary across sessions and read trends. `store` validates first and is idempotent; `trend` returns one metric's time-series. Interpreted with `knowledge/history.md`. |

### Run lifecycle (read-only — stops work, starts none)

| Tool | Key inputs | What it does |
|---|---|---|
| `cancel_run` | `session_id` | Cancel a still-running background run/turn in **another** chat by its session id — frees the concurrency-cap slot it holds and reaps its subprocess (no orphaned process / leaked Job). Idempotent; refuses to cancel the run it is called from. Judgment on **when** to cancel is in `knowledge/run_lifecycle.md`. |

---

## The `SessionPlan` (determinism gate b)

The structured contract the agent proposes and the user approves before any deployment
([`app/validation/session_plan.py`](../app/validation/session_plan.py)). Every enum field is
cross-checked against the live catalog before the card is shown.

| Field | Type | Notes |
|---|---|---|
| `use_case_summary` | str | Restated user intent. |
| `goal_metrics` | list[str] | e.g. `["ttft","throughput"]`. |
| `slo` | `SLOTargets?` | Optional QoS targets (max TTFT/TPOT/ITL/request-latency ms, throughput floor tok/s, success-rate floor); later consumed by `analyze_results`. |
| `spec` | str | A spec name from the live catalog, e.g. `cicd/kind`. |
| `deploy_path` | `kind_sim`\|`guide`\|`gpu` | Default `kind_sim`. |
| `namespace` | str | RFC1123 label. |
| `harness` | str | A harness from the catalog, e.g. `inference-perf`. |
| `workload` | str | A workload profile, e.g. `sanity_random.yaml`. |
| `flags` | dict | Extra CLI flags. |
| `expected_steps` | list[str] | The plan the user reviews. |
| `est_duration_hint`, `reversible`, `notes` | — | Advisory fields. |

---

## Tool result shapes (common conventions)

Handlers return plain dicts. Common keys the agent loop understands:

- `{"error": "..."}` — a recoverable error; returned to the model (a tool never crashes the
  loop). Argument validation failures look like `{"error": "invalid arguments", "details": [...]}`.
- `{"rejected": true, "reason": "..."}` — the user declined a mutating command at the
  approval gate; the model is told to replan.
- `propose_session_plan` returns `{"approved": bool, "plan": {...}, "errors": [...]}`; on
  approval the loop records `session.approved_plan`.

Beyond those conventions, each tool returns a structured, facts-only payload (counts,
metrics from the validated report, classification kinds) — the *interpretation* of those
facts is the LLM's job, guided by `knowledge/`.

## Adding or widening a tool

- **A new tool:** add a Pydantic model in `app/tools/schemas.py`, a handler module under
  `app/tools/`, and a `ToolSpec` in `app/tools/registry.py` (with a description). The JSON
  Schema is emitted to the LLM automatically.
- **A new command the agent may run:** edit only `security/allowlist.yaml` (see its header
  for the worked recipe) — no Python change — and add a case to `tests/test_allowlist.py`.
  Judgment about *when* to use it goes in `knowledge/`, not in code.
