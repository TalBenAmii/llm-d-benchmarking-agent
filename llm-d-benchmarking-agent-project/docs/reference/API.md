# API & Tool Reference

Two interfaces: the **HTTP/WebSocket API** the browser (or any client) speaks to the
backend, and the **agent tool surface** — the schema-validated tools that are the LLM's
*entire* set of actions. The tool input schemas are defined in the
[`app/tools/schemas/`](../../app/tools/schemas/) package (the single source of truth, emitted to
the LLM as JSON Schema); the registry + descriptions live in
[`app/tools/registry.py`](../../app/tools/registry.py).

---

## HTTP endpoints (`app/main.py`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve the chat UI (`ui/index.html`). |
| `GET` | `/static/*` | Static UI assets. |
| `GET` | `/healthz` | **Liveness** (minimal): `{ok: true}` — process is up and serving; no dependency checks. K8s `livenessProbe` target. |
| `GET` | `/readyz` | **Readiness** (Phase 16): `{ready, self_check:{checks:[…]}}` with per-component status (provider configured, repos present, runner ok, workspace writable). `200` when ready, `503` when not. K8s `readinessProbe` target. |
| `GET` | `/metrics` | Prometheus text exposition of the agent + orchestrator metrics (content-type `text/plain; version=0.0.4`). Scrape target. |
| `GET` | `/api/provider` | The active LLM provider + model, for the composer badge. Includes `switchable` (true only for the agent-SDK provider), the current `effort`, and the switchable `models` list (`{id,label,efforts}` from `app/llm/model_catalog.py`) the picker offers. |
| `GET` | `/api/sessions` | Recent chats for the sidebar (summaries, newest first). |
| `DELETE` | `/api/sessions/{id}` | Delete a saved chat; `404` if unknown. |
| `DELETE` | `/api/namespaces/{namespace}` | Delete a whole sidebar folder — every chat in one namespace at once (the `no_namespace` sentinel removes chats with no namespace). Returns `{deleted, count}`; `404` if the folder is empty. |
| `GET` | `/api/sessions/{id}/artifact?path=` | Serve one image artifact (e.g. a run's latency/throughput PNG) from a session's gitignored workspace dir. Read-only, image suffixes only, path hardened against `..` traversal. |
| `GET` | `/api/history?tag=&model=` | Stored historical results (summaries, newest first) + the list of trendable metrics. Each record with a provenance bundle also carries its `bundle_id` + `session_id`. |
| `GET` | `/api/history/trend?metric=&tag=&model=` | Time-series of one metric across stored results (values + the metric's better-direction; no verdict). |
| `GET` | `/api/sessions/{id}/bundle/{bundle_id}` | One reproducibility provenance bundle's JSON (for the UI's Reproduce / Export affordances). Path hardened against `..` traversal in either id. |
| `GET` | `/api/sessions/{id}/bundle/{bundle_id}/report-card.html` | Download a **self-contained** HTML report card for a provenance bundle (results + full provenance + copy-paste command; zero external assets). `Content-Disposition: attachment`. |
| `POST` | `/api/sessions/{id}/share` | **Share a chat via link.** Mint a read-only public link — an *immutable snapshot* of the chat's transcript taken now (a still-pending approval gate is filtered out). Returns `{token, url}`, where `url` is an absolute public link when `SHARE_BASE_URL` is set (shareable off-host) and a relative `/share/{token}` path otherwise (the browser resolves it against its own origin). Owner-only (auth-gated); `404` unknown chat, `400` if there's nothing to share yet. |
| `GET` | `/api/share/{token}` | **Public** read-only transcript of a shared conversation (`{title, created_at, shared_at, items, usage}`; the owning session id is withheld). Reachable **without** Bearer auth — the unguessable token *is* the credential. `404` for a malformed/unknown/revoked token. |
| `GET` | `/share/{token}` | **Public** read-only viewer **page** — serves the SPA shell; the client detects the path, fetches `/api/share/{token}`, and renders the snapshot read-only (no WebSocket, no composer, no sidebar). |
| `GET` | `/api/share/{token}/page.html` | **Public** self-contained, offline `.html` **export** (downloaded as an attachment) — the SPA + the frozen snapshot inlined into ONE dependency-free file (no external assets, no network on open). Host it on *any* static host or open it from disk; the agent is never involved. `404` for a malformed/unknown/revoked token. |
| `DELETE` | `/api/share/{token}` | Revoke a share link: delete its snapshot so the link stops working. Returns `{deleted, token}`. Owner-only (auth-gated); `404` if already gone. |

> **Share-link auth.** Only the `GET` viewer routes (`/share/{token}`, `/api/share/{token}`) bypass the optional Bearer auth (the token is the bearer secret); minting (`POST`) and revoking (`DELETE`) stay auth-gated. The snapshot exposes only the transcript the owner chose to share — never the session list, a live session, or secrets.
>
> **Sharing with someone off-host.** Two ways, depending on whether you want to expose the running app:
> - **Static-file export (no exposure) — recommended.** A shared chat is just a self-contained `.html` (the SPA + frozen snapshot inlined). Grab the file from the dialog's **Download file** button (or `GET /api/share/{token}/page.html`) and drop it on any static host — GitHub Pages, object storage, a Netlify drop — or just send it directly; the agent is never reachable.
> - **Expose the live app.** Reach it over a public URL (e.g. a `cloudflared tunnel --url http://localhost:8000` quick tunnel, or a deployment) — opening the app via that URL already makes links public, or set `SHARE_BASE_URL` to mint absolute links from a localhost session. Because this exposes the *whole* agent, set `AUTH_TOKEN` first: it locks the agent (401 without the token) while share links keep working for viewers (the GET-bypass above).

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
| `ready` | `{session_id, resumed, running, running_elapsed_ms, resume:{incremental,cur_seq}, usage, context_window, auto_approve, model_override, effort_override}` | Connection established; seeds THIS chat's per-session state on connect/reload/switch. `running`+`running_elapsed_ms` flag & time a still-in-flight background turn; `resume.incremental` says the client's cached view was patched (vs. a full `history` rebuild); `usage`+`context_window` re-seed the token/context meters; `auto_approve` re-seeds the toggle; `model_override`/`effort_override` echo the picker's per-chat pick (each may be `null`). |
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
| `usage` | `{turn:{input,output,cache_read,cache_write,calls,total}, session:{input,output,cache_read,total}}` | **Real** provider token usage. A per-turn event emitted on every LLM call (the live UI counter ticks up): `turn.*` are running totals for the in-progress turn, `session.*` the running session totals. |
| `suggestions` | `{chips:[{label,prompt}]}` | Start-of-chat suggestion chips, emitted **once** on a brand-new connection (never on resume), right after `ready`. A connection-lifecycle frame, not a turn event. |
| `resource_stats` | `{available, namespace?, rows?, note?}` | Live cluster CPU/memory for the running benchmark's pods, streamed by the backend resource poller at a fixed interval during a run. Zero LLM cost (never enters the message stream). |
| `done` | `{}` | The agent finished this turn. |
| `pong` | `{}` | Reply to a `ping`. |

**Client → server messages:**

| Type | Payload | Meaning |
|---|---|---|
| `user_message` | `{text}` | The user's chat input (starts a turn). |
| `approval` | `{request_id, approved}` | Approve/Reject a pending command/plan. |
| `cancel` | `{}` | Cancel this chat's in-flight run (Phase 16): frees its concurrency slot, reaps its subprocess. Idempotent. |
| `set_model` | `{model[, effort]}` | Switch this chat's Anthropic model + reasoning effort (the composer model picker). Validated against the served catalog (`app/llm/model_catalog.py`); a bad selection is rejected (`error` `kind:"protocol_error"`, prior pick kept). Per-session ephemeral override, applied at the next turn. **Agent-SDK provider only.** |
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
| `read_knowledge` | `name`, `section` | Load the full text of one of the agent's on-demand `knowledge/` guides by topic (e.g. `read_knowledge('capacity')`), or — with optional `section` — just one named markdown section of it. The system prompt inlines the core guides and indexes the rest; this pulls one in before interpreting that kind of result or decision. Unknown name → returns the valid topics. A guide too large for the tool-result budget is clamped to a leading preview, and the result then names the `dropped_sections` (the headings past the cut) to re-fetch with `section=`. |
| `read_repo_doc` | `path`, `max_bytes` | Read one doc/spec file from inside a read-only repo (path must resolve inside a repo; `..` blocked). |
| `fetch_key_docs` | `task`, `max_bytes_each` | Fetch the **live** content of the authoritative docs pinned in `knowledge/key_docs.yaml`, filtered by task. Called to ground the agent in the real procedure before planning a deploy. |
| `search_knowledge` | `query`, `limit`, `include_repo_docs` | Keyword/topic search across the agent's `knowledge/` guides + the curated upstream repo-doc index when you don't know the exact basename — deterministic lexical ranking (no model call), returning the best guides + a snippet and a ready `read_knowledge(...)`/`read_repo_doc(...)` load hint. Reach for it at a troubleshooting / "how do I…" moment. |
| `advise_accelerators` | `namespace` | Accelerator / CPU-inferencing pre-flight ("can my hardware run this?"): reads each node's advertised resources via `kubectl get nodes -o json` and reports which accelerator key it advertises (`nvidia.com/gpu` / amd / gaudi / tpu / Intel XPU) vs CPU-only, plus per-node cpu/memory. Facts only, no verdict; complements `check_capacity`. |
| `discover_stack` | `endpoint_url`, `kubeconfig`, `context`, `filter_type` | Optional richer environment capture: trace the live llm-d stack behind an OpenAI-compatible endpoint via `llm-d-discover` (its own read-only RBAC + secret redaction), write a BR-v0.2 `{scenario:{stack:[…]}}` capture into the session workspace, and return structured stack facts (component/model/role counts, parallelism). |

### Plan & pre-flight

| Tool | Class | Key inputs | What it does |
|---|---|---|---|
| `propose_session_plan` | approve | the `SessionPlan` (below) | Propose the structured, user-approved contract before any mutation. Enum fields checked against the live catalog. |
| `inspect_workload_profile` | read-only | `profile`, `harness` | **Preview what a workload profile actually sends** before running it: locates the profile under the read-only benchmark repo, parses the YAML, and returns a normalized, auditable summary across the differing harness layouts — token shape (input/output length distribution; prefix reuse), load shape (rate/concurrency/QPS, sweep stages, durations), and prompt/dataset source — each field tagged with the raw key it came from. Facts only; *which* workload to pick is the LLM's judgment (`knowledge/welllit_path_advisor`/`sweep_playbook`). |
| `estimate_run_duration` | read-only | `profile`, `harness` | Rough **pre-run wall-clock estimate** for a workload profile, computed from its load shape (sum of inference-perf sweep-stage durations; or guidellm `max_seconds` × rate stages; or request-count / mean rate). Always returns the `basis`, the stated `assumption`, and `approximate=True` (excludes standup/warmup/teardown); returns `estimable=False` and says what's missing rather than inventing a number. |
| `check_capacity` | read-only | `spec`, `overrides`, `enforce` | Capacity pre-flight ("will this fit?") via the benchmark repo's own planner. `enforce=True` tags shortfalls as deployment-halting errors. Interpreted with `knowledge/capacity.md`. |
| `check_endpoint_readiness` | read-only | `namespace`, `spec`, `probe_cli_endpoints` | Endpoint-readiness pre-flight before benchmarking: reads `kubectl get endpoints` for a *ready backing endpoint* (corroborated by the CLI's read-only `run --list-endpoints`). The orchestrator gates on this so it never benchmarks an unready stack. Interpreted with `knowledge/orchestrator.md`. |
| `generate_doe_experiment` | read-only | `name`, `run_factors`, `setup_factors`, `run_constants`, `setup_constants`, `harness`, `profile`, `target_filename` | Author a DoE experiment YAML: cross-products agent-chosen *factors × levels* into the full treatments matrix, writes it into the session workspace (never the read-only repos), and validates it structurally against the repo's experiment-example format. *Which* factors/levels to sweep is the LLM's judgment, grounded in `knowledge/sweep_playbook.md`. |
| `convert_guide_to_scenario` | read-only | `name`, `env`, `sources`, `scenario`, `harness`, `profile`, `source_ref` | Author a benchmark scenario from an arbitrary llm-d deployment guide — **workspace-only** (unlike upstream's `convert-guide`, never writes into the read-only repos). Resolve the guide's Helm/kustomize config to the `LLMDBENCH_*` env map, grounded in `knowledge/convert_guide`. |

### Prepare (mutating)

| Tool | Class | Key inputs | What it does |
|---|---|---|---|
| `ensure_repos` | approve | `repos`, `ref` | Clone `llm-d-benchmark`/`llm-d` if missing (URL-allowlisted; idempotent; never overwrites). |
| `run_setup` | approve | `use_uv`, `force` | Run `install.sh` in the benchmark repo to build its venv + verify tools. Required before any `llmdbenchmark` command. |
| `write_and_validate_config` | approve | `artifact_type`, `target_filename`, `content` | Write a generated workload/run config into the session workspace and validate it. (MVP uses stock profiles; rarely needed.) |
| `provision_hf_secret` | approve | `namespace`, `name` | Create/update the cluster's HuggingFace token Secret (default `llm-d-hf-token`) so a gated-model standup can pull weights — the follow-on to `check_capacity`'s gated-access pre-flight. The HF token stays backend-only (read from the backend `HF_TOKEN` env by the vetted script; never an input, never in argv or logs). |

### Execute & orchestrate

| Tool | Class | Key inputs | What it does |
|---|---|---|---|
| `run_shell` | gate | `command`, `timeout` | Run an arbitrary shell command (`bash -lc`) — notably `kind create/delete cluster` and `install_prereqs.sh`. Read-only commands auto-run; mutating ones prompt. Prefer a dedicated tool when one fits. |
| `execute_llmdbenchmark` | gate | `subcommand` (`plan`/`standup`/`smoketest`/`run`/`teardown`/`results`/`experiment`), `spec`, `namespace`, `harness`, `workload`, `flags`, `extra` | The single local CLI runner. `plan`/`--dry-run`/`--list-endpoints` auto-run; `standup`/`run`/`teardown`/`experiment` require approval. `experiment` runs a full DoE sweep. |
| `orchestrate_benchmark_run` | approve | `namespace`, `spec`, `harness`, `workload`, `image`, `service_account`, `cpu`, `memory`, `active_deadline_seconds`, `max_attempts`, `watch`, `poll_interval`, `max_wait` | Run a benchmark as a **Kubernetes Job** the orchestrator manages end-to-end: submit → watch → stream logs → classify failures (OOM/timeout/eviction/unschedulable/image/run-error). Transient faults retry as fresh Jobs; deterministic faults never retry. Needs an orchestrator image. |
| `orchestrate_sweep` | approve | `namespace`, `treatments` (each `{name, spec?, harness?, workload?, command?, cpu?, memory?}`), `spec`/`harness`/`workload` (defaults), `image`, `service_account`, `cpu`, `memory`, `scheduling`, `active_deadline_seconds`, `max_parallel`, `max_attempts`, `poll_interval`, `max_wait`, `sweep_id`, `checkpoint`, `require_ready_endpoint` | Run N DoE treatments as **parallel Kubernetes Jobs** under a `max_parallel` concurrency cap against one stood-up stack — each its own retry/dead-letter Job (a persistently-failing treatment dead-letters without sinking the rest). Progress is checkpointed to a cluster ConfigMap, so a re-call with the returned `sweep_id` + same treatments **resumes** (completed treatments skipped). The proposal's parallel-treatment scheduling; the parallel counterpart to `execute_llmdbenchmark(subcommand="experiment")`'s sequential DoE. Needs an orchestrator image. `knowledge/orchestrator.md`. |
### Observe, parse & analyze (read-only)

| Tool | Key inputs | What it does |
|---|---|---|
| `observe_run_metrics` | `namespace`, `scope` (`pods`/`nodes`), `run_id`, `containers` | Live cluster CPU/memory via `kubectl top` *while a run is in flight* — a leading indicator of OOM/throttle. Requires the in-cluster metrics-server. (Distinct from `/metrics`.) |
| `locate_and_parse_report` | `results_dir`, `session_id` | Find the newest Benchmark Report from a completed run, validate it against the repo schema, and return a plain-language metric summary. |
| `compare_reports` | `sources` / `experiment_dir`, `labels`, `baseline_index` | Compare 2+ reports of the **same** harness side by side (an A/B, or a whole DoE sweep): per-metric deltas vs a baseline + the winner per metric. |
| `compare_harness_runs` | `sources` (2+), `labels` | Cross-harness comparison: contrast reports from **different** harnesses (e.g. inference-perf SLO vs guidellm throughput) against the same stack. Reports which metrics ≥2 harnesses both measured (cross-validate) with **no** cross-harness winner. Interpreted with `knowledge/multi_harness.md`. |
| `analyze_results` | `slo`, `sources` / `experiment_dir`, `labels` | SLO-aware filtering + goodput estimate + Pareto/DoE frontier. Pass the `SLOTargets` from the approved plan. Returns per-run SLO verdict + goodput estimate; for a sweep, the Pareto-optimal configs + SLO-feasible frontier. |
| `aggregate_runs` | `results_prefix`, `harness`, `stack`, `run_ids` (≥2), `output_name` | Cross-run aggregation for repeated runs of the **same** benchmark: runs the benchmark repo's own `docs/analysis/aggregate_runs.py` over an existing results dir and writes `aggregated_summary.{txt,json}` into the session workspace, returning per-metric mean/std/min/max (run-to-run variance). |

### History (read-only — never touches the cluster)

| Tool | Key inputs | What it does |
|---|---|---|
| `result_history` | `action` (`store`/`list`/`get`/`trend`/`delete`), `source`, `label`, `tags`, `spec`/`harness`/`workload`/`namespace`/`session_id`, `record_id`, `metric`, `filter_tag`/`filter_model` | Persist a validated report's summary across sessions and read trends. `store` validates first and is idempotent; `trend` returns one metric's time-series. Interpreted with `knowledge/history.md`. |

### Reproducibility (read-only — git reads + a workspace write; no cluster mutation)

| Tool | Key inputs | What it does |
|---|---|---|
| `export_run_bundle` | `source`, `namespace`, `spec`/`harness`/`workload`/`model`/`slo`, `label`, `attach_to_history` | Capture a **provenance bundle** for a *validated* run: both read-only repo SHAs (+ dirty flags), the exact resolved run-config, an env snapshot, the knowledge hash, the agent version, and the schema-validated report digest + summary. Refuses an unvalidated report; never fabricates a SHA (an empty repo → `unavailable`). Returns `bundle_id` + a copy-paste `regenerate_command` + a `dirty` flag. Interpreted with `knowledge/reproducibility.md`. |
| `reproduce_run` | `bundle_id` | Read a saved bundle and return a **rerun proposal** (spec/harness/workload/namespace/slo + run-config path + the dry-run-first sequence + dirty/unavailable caveat). Emits **no** mutating command — the agent then drives `propose_session_plan` → `--dry-run` → the approval-gated `-c` replay. Reuses the existing gates; adds no new mutation path. |

### Run lifecycle (read-only — stops work, starts none)

| Tool | Key inputs | What it does |
|---|---|---|
| `cancel_run` | `session_id` | Cancel a still-running background run/turn in **another** chat by its session id — frees the concurrency-cap slot it holds and reaps its subprocess (no orphaned process / leaked Job). Idempotent; refuses to cancel the run it is called from. Judgment on **when** to cancel is in `knowledge/run_lifecycle.md`. |

### Conversation / UX (read-only — renders chat UI, no cluster/repo access)

| Tool | Key inputs | What it does |
|---|---|---|
| `suggest_next_steps` | `options` (each `{label, prompt}`), optional lead-in | Offer concrete next steps (the agent chooses how many fit — up to 6) as **clickable buttons** instead of a prose "want me to…?". The UI renders them as floating suggestion pills; clicking one submits its `prompt` as the user's next message. The agent's **turn-ending** discretionary follow-up — **not** an approval gate (mutations still go through `propose_session_plan`/`run_shell`). Offer cadence: `knowledge/conversation_style.md`. |

---

## The `SessionPlan` (determinism gate b)

The structured contract the agent proposes and the user approves before any deployment
([`app/validation/session_plan.py`](../../app/validation/session_plan.py)). Every enum field is
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

- **A new tool:** add a Pydantic model in the `app/tools/schemas/` package, a handler module under
  `app/tools/`, and a `ToolSpec` in `app/tools/registry.py` (with a description). The JSON
  Schema is emitted to the LLM automatically.
- **A new command the agent may run:** edit only `security/allowlist.yaml` (see its header
  for the worked recipe) — no Python change — and add a case to `tests/test_allowlist.py`.
  Judgment about *when* to use it goes in `knowledge/`, not in code.
