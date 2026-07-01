# Plan: llm-d Benchmarking Assistant Agent

> **Status — implemented & verified; grown well past the MVP.** All MVP steps below are
> done, and the project has since landed the full roadmap feature set (see
> [`FEATURES.md`](FEATURES.md) for the live inventory): a **Kubernetes-native
> benchmark orchestrator** (Job lifecycle, fault classification, retry/dead-letter, parallel
> sweeps), a **results analyzer** (goodput, SLO filtering, Pareto/DoE), **multi-harness
> comparison**, a **capacity pre-flight**, **cross-session result history + trends**,
> **Prometheus/Grafana observability**, and a **hardened image + one-command Helm
> deploy** with least-privilege RBAC (Kustomize path REMOVED 2026-07-02 — Helm is the single
> deploy mechanism, matching upstream helmfile). The agent now exposes a broad toolset, and the full
> technical documentation suite lives under [`docs/`](docs/) (architecture, API reference,
> deployment guide, user guide). See **[Implementation status](#implementation-status)** for
> the MVP record; the sections after it are the original design reference (kept as written).
>
> **Increment — agent-owned host bootstrap.** The agent installs the prerequisites
> `install.sh` does not (the Docker daemon + the kind binary) via the vetted
> `scripts/install_prereqs.sh`, creates/deletes the kind cluster (`kind create/delete
> cluster`), and reaches any allowlisted command through a generic `run_command` tool plus
> a `fetch_key_docs` doc-grounding tool — all approval-gated and widened purely through
> `security/allowlist.yaml` (the `project-script` runner invoke type runs the pinned
> installer; the allowlist grants no raw `apt`/`curl`/`sudo`).

## Implementation status

**Built:** the full MVP vertical — chat UI → agent loop → schema-validated, approval-gated
tools → real `llmdbenchmark` execution → validated Benchmark Report summary. The project
has since grown substantially; see [`FEATURES.md`](FEATURES.md) for the authoritative,
current feature inventory (the former `ROADMAP_V4.md` Phases 27-66 are now
merged, 57 & 58 deferred; remaining/deferred work is tracked in `FEATURES.md`).
The MVP step record below is preserved for tracking; the design sections after it are the
original design
reference (kept as written).

MVP plan steps (all 8 complete):
- 1. Scaffold + docs (`CLAUDE.md`, `plan.md`, `README.md`, `pyproject.toml`, `.env.example`, `.gitignore`, package tree) — done
- 2. Security core (`security/allowlist.yaml` + `app/security/allowlist.py` validator + `runner.py`; allowlist tests) — done
- 3. Read-only tools + validation (`probe_environment`, `list_catalog`, `read_repo_doc`, `locate_and_parse_report`; `app/validation/report.py`) — done
- 4. Provider + agent loop (`app/llm/`, `app/agent/{loop,session,prompt,events}.py`, registry + pydantic tool schemas, `SessionPlan`) — done
- 5. Mutating tools (`ensure_repos`, `run_setup`, `execute_llmdbenchmark`, `write_and_validate_config`) — done
- 6. Knowledge files (`knowledge/` playbooks + `usecase_to_profile.yaml`) — done
- 7. Chat UI + FastAPI (`ui/`, `app/main.py` WebSocket + approval round-trip + static serving) — done
- 8. Verify (`pytest tests/`; live checks) — done

**Design notes / findings during build (still relevant):**
- The repo's committed `br_v0_2_json_schema.json` is generated from pydantic
  (`extra="forbid"`) and is **stale vs its own example** (`session_performance`). So
  `validate_report` treats `additionalProperties` violations as non-fatal *deviations*
  and hard-fails only on structural errors. Report YAML is loaded with a loader that keeps
  ISO timestamps as strings (PyYAML→`datetime` otherwise breaks JSON-Schema string checks).
- `execute_llmdbenchmark` defaults a `run`'s `-r/--output` to the session workspace so the
  report is easy to locate.
- The allowlist deliberately extends beyond `llmdbenchmark` (to `install.sh --uv`,
  URL-restricted `git clone`, and read-only `docker`/`kind`/`kubectl`) so the quickstart is
  actually reachable — as agreed.

> **Note:** the MVP-era "deferred" items DoE/`experiment` sweeps, multi-harness & A/B
> comparison, capacity pre-flight, history, observability, and generated workloads have
> since shipped — see `FEATURES.md`. GPU / `llm-d/guides/*` deploy execution
> remains future work (path 2 is advisory-only per `knowledge/deploy_path_playbook.md`; the
> Kustomize/WVA guide knobs are tracked in `FEATURES.md`). The roadmap record (the former
> `ROADMAP_V4.md`) — its Phases 27-66 are now merged (57 & 58 deferred).

---

## Context

`llm-d-benchmark` is powerful but expert-only: to run a benchmark you must know the
`<spec, harness, workload>` triplet, the `llmdbenchmark` CLI flags, the deploy
prerequisites, and how to read multi-dimensional results. The goal of this project is a
**local, chat-based assistant agent** that lets a non-expert say *"benchmark a chat app
with 500 concurrent users"* and have the agent interview them, check preconditions (and
**not** redeploy if a stack is already running), deploy an llm-d stack if needed, run the
benchmark, and explain the results.

This plan sets up that project as a **new, self-contained codebase** in the existing
`llm-d-benchmarking-agent-project/` folder. The two repos (`llm-d`, `llm-d-benchmark`)
are **read-only context** — we never modify them; the agent clones them if missing.

**Guiding philosophy (user's explicit constraint): thin code, thick agent.** We code only
the *mechanism* — UI, agent loop, tool implementations, a security allowlist, and
file-format validation. All *judgment* — which spec/harness/workload, what flags, how to
interpret results — lives in the LLM plus **editable knowledge files**, never in Python
branching. Determinism comes from **JSON-schema-validated handoffs** at every boundary,
not from scripted logic.

**First milestone (MVP):** drive the `llm-d-benchmark` **quickstart** (local kind cluster,
CPU-only simulated engine) end-to-end. "Regular"/GPU deployment via the `llm-d/guides/*`
comes later.

### Locked decisions (from user)
- **Backend:** Python + **FastAPI** (serves UI, hosts agent loop, shells out to the venv CLI). Chosen partly so we can read the repo's own Pydantic/JSON schemas for validation.
- **LLM provider:** **configurable** — both Anthropic (native tool-calling) and OpenAI-compatible (incl. self-hosted vLLM/llm-d endpoints). API key lives **only** in the backend.
- **Execution sandbox:** **deny-by-default allowlist + per-action approval.** Read-only probes auto-run; every mutating command needs a click-to-approve in the UI. Commands run as argv lists with `shell=False` (no shell string → no injection).

---

## Architecture in one picture

```
Browser chat UI (HTML/JS/CSS)
   │  WebSocket (stream tokens, command output, approval cards)
   ▼
FastAPI backend  ── agent loop ──►  LLM API (Anthropic | OpenAI-compatible)
   │                                   │ emits schema-validated tool calls
   │  the ONLY things the LLM can do = 8 tools, each schema-validated:
   │     probe_environment · list_catalog · read_repo_doc            (read-only, auto-run)
   │     ensure_repos · run_setup · write_and_validate_config        (mutating, approve)
   │     execute_llmdbenchmark · locate_and_parse_report             (gated runner / read)
   ▼
security allowlist (deny-by-default) ──► subprocess argv, shell=False, repo .venv
   ▼
llmdbenchmark CLI  ─stand up→ benchmark→ Benchmark Report v0.2 (validated against repo schema)
```

The four determinism gates: (a) tool-call args validated vs tool schema; (b) a structured
**SessionPlan** the user approves before any mutation; (c) any generated config validated
via the CLI's own `--dry-run`/`plan`/`--generate-config`; (d) results parsed from the
schema-valid **Benchmark Report**, never scraped from logs.

---

## Workspace layout (for CLAUDE.md)

```
<repo-root>/                  # monorepo checkout (any path / clone location)
├── llm-d/                    # guide repo — READ-ONLY context (deploy guides, later milestone)
├── llm-d-benchmark/          # benchmark repo — READ-ONLY; provides `llmdbenchmark` CLI + its .venv
└── llm-d-benchmarking-agent-project/   # THE ONLY folder we write code in
```

---

## Reuse — do NOT reinvent these (read at runtime, don't vendor)

| What | Path | Used for |
|---|---|---|
| CLI entry point | `llm-d-benchmark/pyproject.toml` → `llmdbenchmark = "llmdbenchmark.cli:cli"` | the gated runner target |
| Spec catalog | `llm-d-benchmark/config/specification/**/*.yaml.j2` (`cicd/kind`, `guides/optimized-baseline`, …) | `list_catalog`, allowlist enum source |
| Harness catalog | `llm-d-benchmark/workload/harnesses/*` (inference-perf, guidellm, vllm-benchmark, inferencemax, nop) | `list_catalog` |
| Workload profiles | `llm-d-benchmark/workload/profiles/{harness}/*.yaml.in` | `list_catalog` |
| **Benchmark Report v0.2 JSON Schema** | `llm-d-benchmark/llmdbenchmark/analysis/benchmark_report/br_v0_2_json_schema.json` (+ `schema_v0_2.py`, `br_v0_2_example.yaml`) | results validation (determinism gate d) |
| Safe preview / config gen | CLI `plan`, `run --dry-run`, `run --generate-config`, `run --list-endpoints` | validate-before-execute; existing-stack probing |
| Install/bootstrap | `llm-d-benchmark/install.sh` (`--uv` fetches python3.11, builds `.venv`) | `run_setup` |

**Rule:** read these from the repo at runtime. The *only* schemas we author are our own
tool-I/O schemas and the `SessionPlan`. If a repo path can't be resolved at startup, fail
loudly — never silently fall back to a stale copy.

---

## Project file layout to create (under `llm-d-benchmarking-agent-project/`)

```
README.md                    # run instructions, architecture, safety model
CLAUDE.md                    # workspace structure + critical rules (repos read-only; no decision logic in Python)
plan.md                      # living design doc (this plan, folded in — the artifact the user asked for)
pyproject.toml               # deps: fastapi, uvicorn[standard], pydantic v2, jsonschema, pyyaml, anthropic, openai, httpx
.env.example                 # LLM_PROVIDER, ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENAI_BASE_URL, MODEL, REPOS_DIR, WORKSPACE_DIR
.gitignore                   # workspace/, .env, __pycache__, *.log

app/                         # FastAPI backend — THIN (mechanism only)
  main.py                    # app: serve ui/ static, mount /ws (WebSocket) + /api; lifespan loads config+knowledge
  config.py                  # pydantic Settings from env; resolves the two sibling repo paths
  llm/provider.py            # provider abstraction chat(messages, tools); + anthropic_provider.py, openai_provider.py
  agent/loop.py              # control loop: assemble prompt → LLM → validate args → approval-gate → stream → feed back
  agent/session.py           # SessionState (resumable), pending-approval registry, workspace/sessions/<id>/state.json
  agent/prompt.py            # system prompt = knowledge files + LIVE catalog snapshot + tool schemas (prompt-cached)
  agent/events.py            # typed WS events: assistant_text, tool_call, approval_request, output_chunk, tool_result, error
  tools/registry.py          # name → (json schema, handler, classification[auto|approval])
  tools/schemas.py           # Pydantic models = source of truth for tool I/O; emit JSON Schema to the LLM
  tools/probe.py             # probe_environment, list_catalog, read_repo_doc, locate_and_parse_report
  tools/repos.py             # ensure_repos (clone if missing), run_setup (install.sh/venv)
  tools/config_artifact.py   # write_and_validate_config (writes ONLY into workspace; MVP-stubbed)
  tools/execute.py           # execute_llmdbenchmark — the single gated CLI runner
  security/allowlist.py      # pure validator over security/allowlist.yaml (NO per-command knowledge in code)
  security/runner.py         # subprocess: argv list, shell=False, cwd pinned to repo, env scrubbed, stream + timeout
  validation/report.py       # load BR v0.2 schema FROM repo at runtime; validate parsed report
  validation/session_plan.py # SessionPlan pydantic model (the user-approved contract)
  knowledge/loader.py        # read knowledge/*.md|*.yaml; resolve repo "see-also" references

security/allowlist.yaml      # DECLARATIVE deny-by-default policy (the safety data file)

knowledge/                   # EDITABLE thick-agent brain (NO Python decision logic)
  quickstart_playbook.md     # MVP happy path as steps, referencing the repo's authoritative files
  usecase_to_profile.yaml    # heuristics: use-case phrasing → harness/workload hints (data, not if/elif)
  deploy_path_playbook.md    # kind/sim vs guides/* vs GPU; defers to repo specs as source of truth
  preconditions.md           # which probes matter; existing-stack "don't redeploy" decision rules
  results_interpretation.md  # translate BR v0.2 fields (TTFT/TPOT/throughput) to plain language
  glossary.md                # non-expert terms

ui/index.html · ui/app.js · ui/styles.css   # chat + streamed output + Approve/Reject cards
workspace/                   # GITIGNORED runtime scratch: sessions/, optional repos/ clone target
tests/                       # test_allowlist.py, test_schemas.py, test_report_validation.py, test_catalog.py
```

**Structural intent:** every file containing *judgment* is under `knowledge/`; everything
under `app/` is mechanism. That split *is* the thin-code/thick-agent rule.

---

## The 8 tools (the agent's entire action surface)

| Tool | Class | Purpose |
|---|---|---|
| `probe_environment` | read-only | One-shot precondition snapshot: container runtime, repos present, tools, kind clusters, kube context, namespaces/pods, **stack_running** (endpoints `/health` + `/v1/models`), venv. Reuses CLI `run --list-endpoints`. |
| `list_catalog` | read-only | Enumerate specs/harnesses/workloads/scenarios **from disk** so the LLM can only ever name things that exist. |
| `read_repo_doc` | read-only | Read an authoritative repo doc/spec on demand (path must resolve inside a repo root; blocks `..`). |
| `ensure_repos` | approve | Clone missing repos (URL allowlisted to `github.com/llm-d/...`). Partial clone → report, never delete a sibling. |
| `run_setup` | approve | `./install.sh --uv`; returns venv path, python version, missing tools (idempotent, re-runnable). |
| `write_and_validate_config` | approve | Materialize a generated workload/run config **into workspace only**, validate via CLI `--dry-run`/`--generate-config`. MVP-stubbed (stock profiles). |
| `execute_llmdbenchmark` | **gate** | The single CLI runner. Input = `{subcommand, spec, namespace, harness, workload, flags}`; builds argv → allowlist → runner. `plan`/`--dry-run`/`--list-endpoints` auto-run; `standup`/`run`/`teardown` require approval. Streams stdout over WS. |
| `locate_and_parse_report` | read-only | Find the run's report, validate against BR v0.2 schema, return a non-expert `summary` computed from the **validated object**. |

Sense (`probe`) → know options (`list_catalog`/`read_repo_doc`) → prepare (`ensure_repos`/
`run_setup`) → constrain generated artifacts (`write_and_validate_config`) → act, gated
(`execute_llmdbenchmark`) → observe via schema (`locate_and_parse_report`). The *sequence*
is the LLM's job, guided by `knowledge/`.

---

## Security allowlist model

`security/allowlist.yaml` is **deny-by-default**, mirroring exactly the verified CLI
surface. `app/security/allowlist.py` is a pure validator (no embedded command knowledge):

1. `argv[0]` must be a known executable (`docker`, `kind`, `kubectl`, `git`, `install.sh`, `llmdbenchmark`), else deny.
2. `argv[1]` must be an allowed subcommand, else deny.
3. Every remaining token = allowed flag, a constrained flag value (regex/enum), or an allowed positional; unknown → deny.
4. Value constraints: namespace RFC1123 regex; `spec`/`harness` cross-checked against the **live catalog** (LLM can't invent one).
5. Effective mode = subcommand mode + conditional overrides (`run --list-endpoints`, any `--dry-run`, `plan` → read-only).

Allowlisted set (MVP): `docker info/version/ps`, `kind get`, `kubectl config/cluster-info/get`,
`git clone(llm-d repos)/status/rev-parse`, `install.sh --uv`, and
`llmdbenchmark {plan,standup,smoketest,run,teardown,results}` with quickstart flags
(`--spec -p -l -w -t --skip-smoketest -n/--dry-run --list-endpoints -U -r`).

**Approval wiring:** a mutating tool call registers a pending action (UUID + literal argv +
classification) and emits `approval_request`; the UI shows an Approve/Reject card with the
exact argv; on Approve the backend **re-validates** then runs (defense in depth); on Reject
the tool returns `{rejected, reason}` so the agent replans. No shell, ever; subprocess env
is scrubbed so the LLM key / HF token never reach child processes.

---

## SessionPlan (the user-approved contract, determinism gate b)

Before any standup the agent proposes and the user approves:
`use_case_summary, goal_metrics[], spec (∈catalog), deploy_path, namespace (RFC1123),
harness (∈catalog), workload (∈catalog), flags{}, expected_steps[], est_duration_hint,
reversible, notes`. Every enum field is cross-checked against the live catalog before the
card is shown. This converts a fuzzy chat into an inspectable contract.

---

## Edge cases the agent must handle (detection → recovery)

Repo missing → `ensure_repos` clone · partial clone → report, don't delete · docker down /
socket perms → explain remediation, never sudo, re-probe · kind cluster / namespace already
exists → probe endpoints; **reuse if healthy (no redeploy)**, else offer gated teardown ·
install.sh partial failure → return failed tool, idempotent re-run · python <3.11 → suggest
`--uv` · gated HF model 401/403 → ask for token via backend env (N/A for kind sim) · pods
Pending/OOMKilled → parse events, report shortfall, suggest lighter profile · invalid LLM
tool args → structured error + bounded retry, then ask user · non-existent spec/harness →
reject with valid options · user rejects mid-flow → acknowledge + replan · report missing →
try CLI `results` + workspace glob, else show stderr tail, **never fabricate metrics** ·
report schema-invalid → refuse to summarize, show validation errors · backend restart
mid-standup → re-probe, reconcile from cluster (cluster is source of truth), resume.

---

## Implementation order

1. **Scaffold + docs:** create `CLAUDE.md` (workspace structure + the read-only/thin-code rules), `plan.md` (this plan), `README.md`, `pyproject.toml`, `.env.example`, `.gitignore`.
2. **Security core first:** `security/allowlist.yaml` + `app/security/allowlist.py` + `runner.py` + `tests/test_allowlist.py` (injection/denial cases). This is the safety foundation; build and test it before anything can execute.
3. **Read-only tools + validation:** `probe_environment`, `list_catalog`, `read_repo_doc`, `locate_and_parse_report`; `validation/report.py` against the repo's BR v0.2 schema; `tests/test_catalog.py`, `tests/test_report_validation.py` (validate `br_v0_2_example.yaml`).
4. **Provider + agent loop:** `llm/provider.py` (+ Anthropic adapter first), `agent/loop.py`, `prompt.py`, `session.py`, `events.py`; tool registry + Pydantic schemas with validate/retry and approval gating.
5. **Mutating tools:** `ensure_repos`, `run_setup`, `execute_llmdbenchmark`.
6. **Knowledge files:** `quickstart_playbook.md`, `preconditions.md`, `results_interpretation.md`, `glossary.md`, `usecase_to_profile.yaml`.
7. **UI:** `ui/index.html` + `app.js` + `styles.css` — chat, streamed output, approval cards.
8. **Wire MVP end-to-end** and run the verification below.

---

## MVP scope

**In:** Anthropic provider; the 8 tools (config_artifact stubbed); allowlist for the
quickstart set; the 4 knowledge files; chat UI with streaming + approval. The agent picks
`spec=cicd/kind, harness=inference-perf, workload=sanity_random.yaml` **via the playbook**
(not hardcoded logic), confirms a SessionPlan, then probe → ensure_repos → run_setup →
standup → smoketest → run → validated report → plain-language summary → offer teardown.

**Deferred:** OpenAI-compatible provider polish (interface exists); `llm-d/guides/*` + GPU
deploy; DoE/`experiment` sweeps; multi-harness & A/B comparison; cloud output stores;
bespoke generated workloads; auth/multi-user; persistence beyond local workspace.

---

## Verification (end-to-end)

1. **Unit:** `pytest tests/` — allowlist rejects injection/unknown flags & classifies modes; `list_catalog` enumerates real repo dirs; `br_v0_2_example.yaml` validates against the repo schema; tool I/O round-trips.
2. **Safety:** confirm a crafted "malicious" tool call (`kubectl delete`, shell metachars, non-llm-d clone URL, unknown flag) is **denied**; confirm a mutating command does **not** run without an explicit Approve.
3. **Dry-run loop (no cluster):** start backend (`uvicorn app.main:app`), open the UI, ask *"benchmark a tiny chat model on my laptop"*; verify the agent probes, proposes a SessionPlan, and that `plan`/`--dry-run` auto-run while `standup` waits for approval.
4. **Full quickstart (real kind):** approve through standup → smoketest → run; verify streamed output, a located+validated Benchmark Report, and a correct plain-language summary (compare metrics against the report file). Then approve `teardown` and confirm cleanup.
5. **Existing-stack guard:** with the kind stack already up, start a new session; verify the agent detects `stack_running`, **does not redeploy**, and offers to benchmark the running stack.

> Note: the project folder currently holds only `llm-d-benchmarking-agent-proposal.md`; everything above is net-new. The user wrote ".claude.md" — I'll use the conventional `CLAUDE.md` filename (what Claude Code auto-loads); flag if you'd prefer the literal lowercase name.
