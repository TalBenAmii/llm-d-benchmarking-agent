# Open Items — what's still not done, where it came from, and why it's hard

> **Date:** 2026-06-20 · **Companion to:** [`PROPOSAL_GAP_REPORT.md`](PROPOSAL_GAP_REPORT.md)
> (the proposal-vs-built audit) and the [`llm-d-benchmarking-agent-proposal.md`](../llm-d-benchmarking-agent-proposal.md)
> (the "north star").
>
> **What this file is.** The gap report answers *"what's missing?"* in a dense table. This file
> takes every item from that report that is **still not done today** and, for each one, answers
> three questions the table doesn't:
>
> 1. **Where did it come from?** — the exact proposal section it traces to, quoted; or, if it is
>    *not* a proposal line-item, why we need it anyway.
> 2. **Why is it actually hard?** — the concrete engineering problems you hit when you sit down to
>    *build* it.
> 3. **Why is it hard to *test*?** — the reason a green CI run can't prove it works, which is
>    usually the real reason it stayed on the shelf.
>
> The point is so that "we didn't do X" reads as *a considered decision with a cost behind it*,
> not as an oversight. **Nothing here is a core-capability hole** — every MVP item and almost
> every stretch goal already ships (see `FEATURES.md`). These are the edges: divergences-by-design,
> environment-gated items, an unmerged dev tool, and code-hygiene cleanups.

---

## How to read this

Each item is tagged with the same verdict legend the gap report uses, plus a "lineage" tag:

- 🔀 **DIVERGENT-BY-DESIGN** — built deliberately differently from the proposal's wording; the
  *intent* is met, the *mechanism* differs. "Doing it the proposal's way" would now mean
  *undoing* a working, safer design.
- 🟡 **PARTIAL** — the capability exists but not in the exact form the proposal describes.
- ⬜ **NOT DONE** — genuinely absent.
- **Lineage = PROPOSAL** — the item is a line in the original proposal (section cited).
- **Lineage = DERIVED** — the item is *not* in the proposal; it came from later work (mining the
  benchmark CLI surface, an internal audit, or a dev-productivity need). For these, the "why we
  need it" is spelled out.

---

# Part A — Open items that come straight from the proposal

These five are the proposal line-items that are still divergent, partial, or absent.

---

## A1 · G2 — Kubernetes **Watch API** for event-driven job monitoring
**Verdict:** 🔀 DIVERGENT-BY-DESIGN · **Lineage:** PROPOSAL

### Where it came from
This is one of the most explicit phrases in the proposal. Three sections demand it by name:
- **§3.3 (Benchmark Orchestrator → Job monitoring):** *"Watches Job status via the Kubernetes API
  (**Watch API for event-driven updates**). Streams logs in real-time. Detects OOM kills, timeouts,
  and pod evictions."*
- **§5.1 (MVP):** *"monitors Job completion via **Watch API**, and collects the universal Benchmark
  Report."* — note: the proposal puts Watch in the **MVP**, not a stretch goal.
- **§4 (Distributed Systems Relevance → Observability):** real-time reaction to distributed pod
  state is framed as a core distributed-systems learning objective.

### What we built instead
The orchestrator **polls**: `kubectl get jobs -l run-id=<id> -o json` inside an `asyncio.sleep`
loop (`app/orchestrator/controller.py:watch`). The event-driven *feel* the proposal wanted is
preserved at the UI layer — status transitions fire callbacks that push over the WebSocket — but
the underlying cluster signal is a poll, not a `watch` stream.

### Why it's hard to *build* the proposal's way
1. **It collides with project security rule #5.** Our entire safety model is "every cluster call is
   an allowlisted `kubectl` argv with `shell=False`, deny-by-default, env-scrubbed." A true Watch
   stream means either (a) a long-lived `kubectl get --watch` subprocess whose stdout we parse
   forever — which breaks the "one argv → one bounded result → exit" contract the allowlist
   validator and approval gate assume — or (b) importing a Python K8s client (that's G3, below),
   which bypasses the allowlist entirely. Neither fits without re-architecting the security seam.
2. **Watch streams are not "fire and forget."** A correct Watch implementation has to handle
   `resourceVersion` bookmarks, `410 Gone` re-list-and-rewatch on expiry, connection drops, and
   idle-timeout reconnect. That's a genuine little state machine. Polling has none of it: each tick
   is a fresh, stateless, idempotent read.
3. **The functional requirement is already met.** Polling detects the same terminal/failure
   conditions (OOM, timeout, eviction) the proposal lists — it just notices them a few seconds
   later. For benchmark jobs that run for minutes, sub-second latency on the status edge buys
   nothing.

### Why it's hard to *test*
- A Watch stream is **inherently temporal and connection-stateful**, so a hermetic unit test has to
  fake not just "the job is now Failed" but the *delivery* of that event — including the nasty paths
  (re-watch after `410`, dropped connection mid-stream). Our test suite runs against a **fake
  in-process kube client**; faking a poll is one method returning canned JSON, faking a Watch is a
  whole async event-source mock with replay/expiry semantics.
- The code comment on the polling loop says it plainly: *"simpler and more robust… trivially
  testable against a fake."* That testability is the feature.

### Why it wasn't done until today
It's **not pending — it's a closed decision.** The proposal's *intent* (notice job lifecycle
changes and react) is delivered; only the *mechanism* differs, and switching to Watch would mean
trading a trivially-testable, allowlist-compatible design for a stateful one that fights the
security model. Recommendation in the gap report (§6): **document the deliberate choice and move
on** — low value to revisit.

---

## A2 · G3 — Official **Kubernetes Python client** (`kubernetes` / `kr8s`)
**Verdict:** 🔀 DIVERGENT-BY-DESIGN · **Lineage:** PROPOSAL

### Where it came from
- **§7 (Technology Stack → Kubernetes Client):** *"**kubernetes Python client (official) or kr8s**
  for async Watch support."* The proposal names the library directly.

### What we built instead
No `kubernetes`, `kr8s`, or `kubernetes_asyncio` import exists anywhere in the tree. Every cluster
interaction shells out to **allowlisted `kubectl`** via `app/orchestrator/kube.py`.

### Why we did *not* want the proposal's mechanism
This is the same root cause as G2, stated as a standalone tech-stack choice. A Python K8s client
would issue API calls **in-process**, which means it sits **outside**:
- the **deny-by-default allowlist** (`security/allowlist.yaml` — data, not code),
- the **per-action approval gate** (every mutation needs explicit UI approval), and
- the **subprocess env scrub** (rule #6 — secrets never leak into a child process).

Routing K8s through a library would mean re-implementing all three guardrails *inside* the client
wrapper, and every PR touching cluster code would have to be re-audited for "did this accidentally
make a mutating call that skipped approval?" The `kubectl`-argv approach makes that **impossible by
construction**: a mutation is literally a different argv, and the validator can see it.

### Why it's hard to *build* safely
- You'd have to wrap the client so that **every** mutating verb (`create`, `delete`, `patch`,
  `apply`, scale) is intercepted and pushed through the same approval flow `kubectl` calls use
  today — re-deriving the allowlist semantics in Python, which is exactly the "decision logic in
  Python" that rule #3 forbids.
- Auth/config surface explodes: in-cluster service-account vs. local kubeconfig vs. context
  switching all become library config instead of "whatever `kubectl` is already pointed at."

### Why it's hard to *test*
- The current tests mock at the **subprocess boundary** (a fake runner returns canned `kubectl`
  output). With a Python client you mock at the **API-object boundary** — a far larger, version-
  coupled surface (the `kubernetes` client's models track the cluster API version). Test fixtures
  would be heavier and more brittle.

### Why it wasn't done until today
**Deliberate and final.** The gap report's recommendation (§6) is blunt: **"Don't — it breaks the
security model. Keep kubectl."** This is High-effort *and* risky for negative value.

---

## A3 · G4 — **Configuration Explorer** Pareto-visualization integration
**Verdict:** 🟡 PARTIAL · **Lineage:** PROPOSAL

### Where it came from
- **§3.4 (Results Analyzer → DOE analysis):** *"identifies Pareto-optimal configurations across the
  treatment matrix… **Integrates with the existing Configuration Explorer's Pareto visualization.**"*
- **§5.2 (Stretch Goals):** *"Integration with Configuration Explorer: Use the Capacity Planner to
  pre-validate configurations before benchmark execution."*

### What's done vs. what's missing
- ✅ **The Capacity Planner half is integrated.** `app/tools/capacity.py` shells into the upstream
  `llmdbenchmark.utilities.capacity_validator.run_capacity_planner` for pre-flight feasibility
  checks — exactly the §5.2 stretch ask.
- 🟡 **The Configuration Explorer's *visualization* is not wired in.** Our Pareto/DoE frontier is
  **agent-authored** (`app/validation/analysis.py`, `app/tools/compare.py`, plus the browser
  scatter-plot card), not a re-use of the upstream explorer's own plotting output.

### Why it's hard to *build* the proposal's way
1. **The upstream "visualization" is a notebook/library artifact, not a service.** Re-using *its*
   plot means either importing its plotting stack (matplotlib/plotly figures generated server-side)
   and shipping rendered images into a chat UI, or scraping its output format. Either way we'd be
   coupling our results panel to an upstream module whose API is not a stable contract — and rule #7
   says read repo truth at runtime, not vendor copies, so we'd be chasing its changes.
2. **We already render the same insight, natively and interactively.** The browser draws its own
   Pareto-front scatter with SLO shading from our parsed Benchmark-Report data. Embedding a static
   upstream figure next to it would be *redundant and less interactive* — a worse UX for duplicated
   effort.
3. **Impedance mismatch:** the explorer's viz expects *its* config-space coordinates; our frontier
   is computed over *our* treatment matrix + goodput/SLO filter. Mapping one onto the other is real
   glue with little payoff.

### Why it's hard to *test*
- Testing "we correctly embedded an upstream visualization" means asserting on a **rendered figure**
  (an image or a plotly JSON blob) produced by code we don't own and don't control the version of.
  That's a snapshot test against a moving upstream target — brittle by nature. Our own Pareto logic,
  by contrast, is tested on parsed numeric data with deterministic frontier assertions.

### Why it wasn't done until today
The **intent** of §3.4/§5.2 — "give the user a capacity pre-flight *and* a Pareto/cost-optimal
view" — is **met**; only the *specific upstream-viz reuse* is absent, and reusing it would add
coupling and redundancy for no new user-visible capability. Classified as **optional polish**
(gap report §6: "Low–Medium effort; our own Pareto cards already cover the user need").

---

## A4 · G1 (residual) — A **mutating** orchestrator REST API (`POST /api/jobs`)
**Verdict:** 🔀 DIVERGENT-BY-DESIGN (read half shipped; write half intentionally not) · **Lineage:** PROPOSAL

### Where it came from
- **§3.3** frames the orchestrator as a service that **submits / monitors / manages** jobs.
- **§7 (Tech Stack → API Framework):** *"**FastAPI for the orchestrator REST/WebSocket API**."*
- **§4 (API Design):** clean programmatic interfaces between agent, orchestrator, and analyzer.

### What's done vs. what's intentionally not
- ✅ **The cheap, read-only slice shipped** (HEAD `6626a77`): `GET /api/jobs?namespace=…&session_id=…&sweep_id=…`
  (`main.py::list_orchestrated_jobs`) mirrors run state for non-chat clients by reusing
  `BenchmarkOrchestrator.reconstruct()`. It never mutates and degrades softly when no cluster is
  reachable. A programmatic client **can** now poll run state without driving the LLM.
- 🔀 **The mutating API is deliberately *not* built:** there is no `POST /api/jobs` /
  `DELETE /api/jobs/{id}` to *submit* or *stop* a run outside the chat. Submitting and stopping stay
  **approval-gated through the chat** (`orchestrate_benchmark_run`, `orchestrate_sweep`,
  `manage_orchestrated_runs`).

### Why we didn't build the write API
This is the "**thin code, thick agent**" thesis (rule #3) colliding with the proposal's more
service-shaped vision. The product is a **chat assistant**: its public surface is the chat
WebSocket, and *every mutation is meant to pass an LLM-reasoned SessionPlan + human approval first*.
A raw `POST /api/jobs` would let a client **submit a cluster-mutating benchmark job while bypassing
the approval gate and the plan-validation step** — punching a hole straight through the safety model
for the sake of an interface the product thesis doesn't call for.

### Why it's hard to *build* safely
- You can't just expose the orchestrator's submit method over HTTP; you'd have to **re-create the
  approval/SessionPlan handshake in a request/response (or callback) shape**, decide how a headless
  client "approves" a mutation, and re-apply the allowlist + env-scrub on that path. That's a second,
  parallel safety pipeline to build and keep in sync with the chat one.
- Auth becomes a real question: the chat path is gated by the UI session; a public mutating API
  needs its own authn/z story (who is allowed to spend GPU?), which the project intentionally hasn't
  taken on.

### Why it's hard to *test*
- The read mirror is easy to test (stateless reconstruct → assert JSON). A mutating API needs tests
  for the **approval-bypass-prevention** invariant — i.e., proving a client *cannot* start a job
  without approval — which is a security property, not a happy-path assertion, and the surface to
  cover (auth, concurrency, partial failure) is large.

### Why it wasn't done until today
**Intentional, by thesis.** The harmless read slice was worth doing and is done; the mutating API
stays chat-only on purpose. Gap report §6 marks G1 **DONE** for the read mirror and explicitly keeps
the write API out of scope.

---

## A5 · G5 — **Upstream contribution PR** + **final live GPU demo**
**Verdict:** ⬜ / 🟡 NOT DONE (mostly out-of-scope for *code*) · **Lineage:** PROPOSAL

### Where it came from
- **§5.3 (Final Deliverables):** *"**If quality is sufficient**: upstream contribution to
  llm-d-benchmark as an agent module PR. Students credited as authors."* — explicitly conditional.
- **§10 (Open-Source Contribution Path):** the agent *"can be submitted as a new module in the
  llm-d-benchmark repository."*
- **§6 (Timeline, weeks 10–14):** integration testing on a real GPU cluster, then *"Final
  presentation and **live demo**."*

### Two distinct sub-items
**(a) The upstream PR.** The agent is a **standalone project in its own repo**
(`origin = github.com/TalBenAmii/llm-d-benchmarking-agent`); it *wraps* the upstream CLI but is not
a module *inside* `llm-d-benchmark/`. The proposal itself gates this on *"if quality is sufficient /
if applicable."*

**(b) The live GPU demo.** Eight GPU-only well-lit paths are catalogued in
`knowledge/welllit_path_advisor.yaml` and *would* submit to a real GPU cluster if one were
configured — but only the `cicd/kind` **CPU-sim** path is actually exercised today.

### Why it's hard — and why it's mostly *not a code problem*
1. **(a) is a process + politics gap, not an engineering one.** Contributing upstream means
   restructuring this repo to live as a submodule under `llm-d-benchmark/` (which is **READ-ONLY**
   to us by rule #1 — we literally cannot edit it from here), reconciling our packaging/CI with
   theirs, and going through a **maintainer review cycle** on an external project's timeline. None of
   that is blocked by missing code; it's blocked by ownership and a review queue we don't control.
2. **(b) is hardware-gated.** A "live demo on a lab GPU cluster" needs an actual multi-GPU cluster.
   The dev box is **WSL2 + a single 8 GB Blackwell laptop GPU** (see `docs/GPU_CLUSTER_RUNBOOK.md`).
   The eight GPU paths stay **advisory** until real hardware lands — the code to drive them exists,
   but it has never been *exercised end-to-end on GPUs*.

### Why it's hard to *test*
- You cannot hermetically test "a live GPU demo" — by definition it needs the GPU cluster the dev
  environment doesn't have. The closest we get is the **CPU-sim path on Kind**, which exercises the
  whole workflow shape but never touches a GPU scheduler, real KV-cache behavior, or
  GPU-utilization metrics. So the GPU paths are *structurally* correct and *unverified against real
  silicon* — a gap a green CI run can never close.
- The upstream PR "test" is a human review, not an assertion.

### Why it wasn't done until today
**External / hardware-gated, and partly conditional in the proposal's own wording.** This is a
*course deliverable* (presentation + demo + optional contribution), not a missing capability of the
codebase. It stays advisory until (a) a GPU lab cluster is available and (b) a maintainer review
cycle is pursued.

---

# Part B — Built but not shipped (internal, started-and-unfinished)

This is the one item that genuinely *exists as working code* but a user can't reach it yet.

---

## B1 · T1 — `graph_query`, a graphify-backed **code-navigation tool**: built + tested, never merged
**Verdict:** ⬜ NOT REACHABLE (complete on a branch) · **Lineage:** DERIVED (not a proposal item)

### What it is
A complete tool that lets the agent answer structured "where is X defined / what calls Y / path
between A and B" questions over the codebase using the `graphify` code-nav graph. It lives on branch
`worktree-graphify-runtime-tool` (commit `6e8321b`) and adds: `app/tools/graph.py`, a `GraphQueryInput`
schema, a registry entry, a `config.py` `graph_index_path`, an **allowlisted `graphify` executable**,
`knowledge/graph_query.md`, and **15 tests**. It is **absent from `main`.**

### Why we need it (it's *not* in the proposal)
The proposal never mentions code navigation — this is a **developer-productivity** capability, the
partial answer to the open todo question *"do we have LSP integration in python?"* The answer: not
LSP, but a structured **code-graph retrieval** tool was built so the agent can reason about the
codebase via graph queries instead of raw grep. We want it because it makes the agent better at
self-referential / maintenance tasks and matches the project's existing dev-time `graphify` usage.

### Why it isn't merged — the actual problem
- **It's based on a stale commit.** The branch forked from `823ad8d` (Jun 7), now **far behind**
  `main`. It needs a **rebase** before it can merge cleanly — and `main` has moved a lot since
  (tool count went 34→37, schemas/registry churned), so the rebase is where conflicts and a
  re-verification of the 15 tests live.
- **Merging adds a new allowlisted executable (`graphify`)**, which touches the security surface —
  so the rebase isn't purely mechanical; the allowlist entry and the tool's argv contract have to be
  re-checked against the current `security/allowlist.yaml`.

### Why testing it is non-trivial
- The 15 tests assume a **graph index exists at `graph_index_path`**. That index is a *build
  artifact* (`graphify-out/`), so the tests are coupled to a generated, machine-specific fixture —
  which has to be regenerated/validated after the rebase, and kept hermetic so CI doesn't depend on
  a live `graphify` run.

### Why it wasn't done until today
It was **deliberately deferred** — the work is done, but landing it means a non-trivial rebase onto a
much-changed `main` plus a security-surface re-check, and it's a dev convenience rather than a
user-facing requirement, so it lost every prioritization contest. The path forward is explicit:
**rebase the branch and merge — do not re-author the tool** (gap report §7.2 T1).

---

# Part C — Implemented but gated OFF by default (not really "undone" — listed for honesty)

These are **complete and tested**, but a user can't *trigger* them without flipping an env flag.
They're included so the inventory is honest, but **none is a missing capability** — each gate is
intentional, fail-loud behavior, not abandonment.

| Item | Gate (default) | Why gated · why it can't be "always on" |
|---|---|---|
| **`run_resilience_drill`** (chaos / restart-durability machinery: `restart.py`, `prove_restart_recovery`) | `CHAOS_ENABLED=false` | A chaos drill **deliberately kills/restarts** the orchestrator to prove stateless recovery. You never want that auto-runnable in a normal session. Even when enabled it runs only against an **in-process fake cluster** (`_DrillKubeClient`) — so the durability proof is real but hermetic. *Testing problem:* proving "recovers from a crash" requires *causing* a crash mid-run and asserting reconstruct-from-cluster — only safe behind a hard gate. |
| **`run_shell`** (arbitrary-bash escape hatch) | `UNRESTRICTED_TOOLS=false` | This is a **power-user hole through the allowlist** — when on, it drops `shell=False` and runs arbitrary bash with no per-command allowlist. Default-off is the whole point; on-by-default would void rule #5. *Testing problem:* you can't meaningfully unit-test "runs arbitrary commands" without either neutering it or risking the host. |
| **`orchestrate_benchmark_run` / `orchestrate_sweep`** (real cluster Jobs) | refuse on empty `ORCHESTRATOR_IMAGE` | Submitting a real benchmark Job needs a built container image; the `Dockerfile` + Helm/Kustomize supply it **in a real deploy**, but local dev has no image, so the tool **fails loud** rather than submit a broken Job. Correct behavior, not a gap. *Testing problem:* end-to-end exercise needs a real image + cluster; tests use the fake kube client + CaptureRunner instead. |

**Lineage note:** the parallel-sweep machinery behind `orchestrate_sweep` *is* a proposal item
(§5.2 stretch + §4 "parallel job scheduling with configurable concurrency") — but it is now
**CLOSED/shipped** (gap report G7), just image-gated for local dev. The chaos drill and `run_shell`
are **DERIVED** (project-added robustness/escape-hatch features the proposal never asked for).

---

# Part D — Dead / orphaned code (cleanups, not features)
**Verdict:** ⬜ (orphaned) · **Lineage:** DERIVED — none of these is a proposal item; they're **code hygiene**

None of these is "a feature we owe the user." They're **started-then-abandoned fragments** the
internal reachability audit (gap report §7.3) surfaced. We list them because "not done" honestly
includes "we left some dead ends in the tree." Each needs **removal or wiring**, and the reason none
is done yet is the same: **low user impact, so it never out-prioritized real work** — and removing
code carries its own small risk (you must prove nothing reaches it before deleting).

| Orphan | Where | Problem · why it lingers |
|---|---|---|
| **Dead tool input fields** | `export_run_bundle.session_id` (`schemas.py:828`); `advise_accelerators.namespace` (`schemas.py:43`) | The LLM can *set* these, but nothing consumes them (`build_bundle()` has no `session_id` param; the namespace field's own description says "unused… reserved"). *Risk in fixing:* either wire them through (real behavior change) or remove them (schema change the prompt/knowledge may reference). Testing the removal means proving no prompt path emits them. |
| **Dead route** | `GET /api/sessions/{sid}/bundle/{bundle_id}` raw-JSON (`main.py:569`) | The UI only ever fetches the `.html` sibling, so the raw-JSON route is unreached despite its docstring. *Why it lingers:* harmless, and deleting a route needs a check that no external client depends on it. |
| **Dead module** | `app/packaging/assets.py:58-77` (`required_rbac_rules`, `deploy_dir`, `helm_chart_dir`, `kustomize_base_dir`) | Only its own unit test imports it; no route/tool/prompt reaches it. *Why it lingers:* it *looks* load-bearing (RBAC/deploy paths), so deleting it safely needs confidence it's truly orphaned — exactly the kind of verification that gets deprioritized. |
| **Dead event constant** | `SESSION_PLAN = "session_plan"` (`app/agent/events.py:98`) | Never emitted — the SessionPlan rides the `approval_request` event's `kind` field instead. Trivial to remove; just never urgent. |
| **Orphan knowledge** | `knowledge/sim_integration.md`; `knowledge/benchmark_feature_coverage.md` | Loadable via the generic index but **never deliberately cued**. The sim-honesty rule *should* be injected with the `SIMULATE_NOTE` (`prompt.py:150`) but isn't — so it's missing **exactly when it matters** (a real correctness edge, not just cleanup). Nothing routes capability questions to the coverage doc. *Why it lingers:* fixing the cue means touching prompt-assembly, which is byte-stability-sensitive (prompt-cache). |
| **Orphan dev file** | `ui/preview.html` (served at `/static/preview.html`) | A card-layout fixture driven by `window.__LLMD_PREVIEW__`, **linked from nowhere**. Intentional dev fixture, not a user feature — kept on purpose, flagged so it's not mistaken for reachable UI. |

The only one with **user-facing correctness weight** is the `sim_integration.md` cue (a SIMULATE-mode
honesty rule that never surfaces); the rest are pure tidy-ups.

---

# Part E — Deferred ROADMAP_V4 phases (7 of them)
**Verdict:** ⬜ DEFERRED · **Lineage:** DERIVED — **not** proposal line-items

### Where these came from (since they're *not* in the proposal)
The proposal describes the *agent*; these seven phases come from a **separate exercise**: mining the
**full surface of the upstream `llmdbenchmark` CLI** (the `benchmark-catalog-gap` skill) to find
every CLI feature the agent doesn't yet wrap. So "why we need them" isn't "the proposal asked" — it's
**"to be a complete front-end to the benchmark tool, the agent should eventually expose these CLI
capabilities too."** Each is **environment-gated, experimental, or an empty upstream stub** — which
is precisely why each is deferred rather than built. (Source: `ROADMAP_V4.md` §"Remaining work".)

| Phase | What it is | Why we'd want it | Why it's hard to build *and* test → why deferred |
|---|---|---|---|
| **34 — WVA enablement** (`-u/--wva`) | Toggle the Workload Variant Autoscaler, tune HPA/VA knobs, interpret its 8 smoketests | Completeness: WVA is a real llm-d autoscaling path a user on the right platform would want the agent to drive | **OpenShift-only.** WVA needs HPA/VA controllers that **don't exist on Kind/CPU** — the dev environment. *Build:* needs an OpenShift gate + 8 smoketest interpretations in `knowledge/`. *Test:* you can't hermetically exercise an OpenShift-only autoscaler on Kind; the most a test can do is assert `build_argv` *emits* `-u/--wva` and the allowlist permits it — it can never prove the smoketests actually pass. **Deferred until a non-Kind target lands.** |
| **43 — `--non-admin` skip** | Namespace-only (non-cluster-admin) operation for shared clusters | Lets the agent run on a **shared cluster** where the user lacks cluster-admin — a real enterprise scenario | The Kind MVP runs **cluster-admin by default**, so there's no non-admin context to probe locally. *Build:* needs a cluster-admin-vs-namespace probe + skip-the-cluster-scoped-steps logic. *Test:* faking "you don't have cluster-admin" hermetically is possible (assert `build_argv` emits `--non-admin` from a probed non-admin context) but proves only the flag plumbing, not real RBAC behavior. **Deferred until a shared-cluster target lands.** |
| **44 — Telemetry push** (`--telemetry-enabled`) | Opt-in HTTP usage reporting from the CLI | Some orgs want CLI usage telemetry pushed to their own endpoint | **Adds zero coverage today** — the agent already exposes its **own** Prometheus `/metrics`. *Build:* model the flag (DATA-only allowlist widen), keep endpoints backend-only, add a privacy note. *Test:* default-emits-no-flag + opt-in-emits-flag + an env-scrub test on the endpoint — all doable, but for a feature **nobody has opted into**. **Deferred until a user opts in.** |
| **47 — Cloud results upload** (GCS/S3) | `gcloud storage cp` / `aws s3 cp` upload helpers for a cloud results sink | Pushing benchmark artifacts to object storage on cloud targets | Local default is a **no-op**; only matters on cloud targets. *Build:* allowlist the upload helpers as approval-gated mutating actions, reuse the CLI's `cloud_upload.py`. *Test:* assert the upload command is approval-gated + allowlisted and the local path stays a no-op — but you **can't hermetically test a real `gs://`/`s3://` round-trip** without cloud credentials and a bucket. **Deferred for the Kind MVP.** |
| **52 — Multi-turn trace replay** (`--trace-file`) | Replay a JSONL conversation trace; report TTFT by turn-bucket | A realistic multi-turn benchmark mode | **Experimental upstream** — the feature isn't stable in the CLI yet, and there are no trace-replay references in `app/` or `knowledge/`. *Build:* model the invocation + parse the by-turn report. *Test:* a fixture trace → parsed by-turn report is doable, but pinning behavior to an **upstream feature that may still change** invites churn. **Deferred until upstream stabilizes it.** |
| **57 — `flexibility.md`** | Track an empty upstream placeholder doc | Doc-completeness: don't silently drop a doc upstream will populate | **Nothing to build** — it's an empty upstream stub ("To be populated."). *Test:* n/a; the catalog re-derivation guards against drift. **Deferred until upstream writes it.** |
| **58 — FAQ / RBAC-audit docs** | Track two empty upstream placeholder docs (`faq.md`, `rbac_audit_report.md`) | Same doc-completeness reason as 57 | **Empty stubs with no features.** Same as above — track only. **Deferred until upstream populates them.** |

**Common thread:** every one of these is blocked on a **precondition the dev environment can't
satisfy** (OpenShift, a shared cluster, a cloud bucket, an explicit opt-in) or on **upstream not
being ready** (experimental feature / empty doc). That's why none was built: there is literally
nothing to verify green against today. Each is promoted back onto the active line the moment its
precondition lands.

---

# Summary — the whole "not done" list at a glance

| # | Item | Verdict | Lineage | Why it's still open (one line) |
|---|---|---|---|---|
| A1 | G2 — Watch API | 🔀 Divergent | Proposal §3.3/§5.1/§4 | Polling meets the need + is trivially testable; Watch fights the allowlist/approval model. |
| A2 | G3 — K8s Python client | 🔀 Divergent | Proposal §7 | A library bypasses the deny-by-default allowlist + approval + env-scrub. **Don't.** |
| A3 | G4 — Config Explorer Pareto viz | 🟡 Partial | Proposal §3.4/§5.2 | Capacity Planner *is* reused; our own Pareto cards already cover the user need. |
| A4 | G1 — mutating REST API | 🔀 Divergent | Proposal §3.3/§7/§4 | Read mirror shipped; a write API would bypass the chat approval gate (agent-first thesis). |
| A5 | G5 — upstream PR + GPU demo | ⬜/🟡 | Proposal §5.3/§10/§6 | External review cycle + GPU-cluster hardware the dev box lacks; partly conditional in the proposal. |
| B1 | T1 — `graph_query` dev tool | ⬜ (built, unmerged) | DERIVED | Done on a stale branch; needs a rebase onto a much-changed `main` + security re-check. |
| C  | Gated features (chaos / `run_shell` / image-gated orchestrate) | ✅ but OFF | mixed | Complete + tested; intentionally gated, not undone. |
| D  | Dead/orphan code (6 spots) | ⬜ orphan | DERIVED | Code hygiene; low impact so never prioritized (only the SIM-cue has correctness weight). |
| E  | ROADMAP phases 34/43/44/47/52/57/58 | ⬜ deferred | DERIVED | Each blocked on a precondition the dev env can't satisfy, or on upstream not being ready. |

**Bottom line:** of everything "not done," only **B1 (graph_query)** is finished code waiting on a
merge, and only **A3 / D's SIM-cue** are genuine (small) capability/correctness gaps worth closing
for the agent's own users. Everything else is either **divergent-by-design** (A1, A2, A4 — undoing
them would *weaken* the system), **external/hardware-gated** (A5, most of E), or **complete-but-gated**
(C). That's why none was done until today — not oversight, but each one's cost vs. value, written out.
