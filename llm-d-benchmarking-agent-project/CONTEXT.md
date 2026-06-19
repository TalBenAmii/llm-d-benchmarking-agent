# llm-d Benchmarking Agent

A local chat-based assistant that drives the `llm-d-benchmark` CLI on a non-expert's behalf:
it interviews the user about a use case, checks the environment, deploys an llm-d inference
stack if needed, runs the benchmark, validates and explains the results — all behind a
deny-by-default security sandbox and a structured approval gate. Its governing principle is
**thin code, thick agent**: Python is mechanism only; all judgment lives in the LLM plus
editable files under `knowledge/`.

> This is the project's **ubiquitous-language glossary** (the mattpocock `domain-modeling`
> CONTEXT format): canonical names for domain concepts, and the wrong words to avoid. It is a
> glossary only — no implementation details, no status, no decisions (those go in code,
> `docs/`, or ADRs). Keep definitions to 1–2 sentences. Update it the moment a term is
> coined or sharpened. Architecture/refactor vocabulary (module, interface, depth, seam,
> adapter, leverage, locality) lives in the `codebase-design` skill, not here.

## Language

### Architecture & invariants

**Thin code, thick agent**:
The governing rule that Python is *mechanism only* (UI, agent loop, tools, allowlist, schema validation) while all *judgment* — which spec/harness/workload to use, what flags to pass, how to read results — lives in the LLM plus editable Markdown/YAML under `knowledge/`. A decision encoded as a Python `if/elif` branch violates it.
_Avoid_: "business logic in code", "config-driven" (it's LLM-reasoned, not a config switch), "rules engine".

**Determinism gate**:
One of four validation boundaries that constrain the free-form LLM so the system stays reproducible: (a) tool-arg schema validation, (b) SessionPlan approval, (c) config preview via the CLI's own `--dry-run`/`plan`, (d) result parsing from the validated report schema. The slogan is "determinism via validation, not scripting".
_Avoid_: "guardrail", "check", "assertion" (these are the *named, enumerated* gates, not ad-hoc checks).

**knowledge/**:
The agent's editable brain — ~50 Markdown/YAML files holding all judgment, loaded into the system prompt at runtime. Contains no Python. Split into CORE (inlined verbatim every prompt) and on-demand (pulled via `read_knowledge`).
_Avoid_: "docs", "prompts", "config" (it is the thick-agent decision layer, not documentation or a config file).

**CORE knowledge**:
The small set of `knowledge/` files inlined verbatim into *every* system prompt (preconditions, deploy-path playbook, usecase mapping, quickstart playbook, key docs, conversation style). Promoting a file to CORE is expensive because it inflates the always-on prompt prefix.
_Avoid_: "default knowledge", "base prompt".

**catalog**:
The set of valid specs, harnesses, and workloads, discovered **live** from the `llm-d-benchmark` repo on disk at runtime — never hardcoded or invented. SessionPlan and allowlist values are cross-checked against it.
_Avoid_: "registry" (that's the tool registry), "options list", "presets".

**ToolContext**:
The shared dependency bundle (settings, allowlist, runner, per-session workspace, approval/emit callbacks, concurrency semaphore) passed to every tool handler, exposing the single command seam `run_command` / `run_readonly` that all execution passes through.
_Avoid_: "context object" generically, "session" (the Session is separate).

### Workflow stages

**probe**:
A read-only diagnostic that senses the environment (cluster reachable, repos present, metrics-server available, catalog contents) and **auto-runs without approval**. The sensing phase that grounds the agent before it proposes anything.
_Avoid_: "check", "scan", "health check" (probes are specifically the read-only, auto-run sensing tools).

**SessionPlan**:
The structured, schema-validated plan (spec/harness/workload/namespace, optional SLO targets) that the user must **approve before any mutating action runs**. Its enum fields are cross-checked against the live catalog before the approval card is shown. A material scope change requires a *new* SessionPlan.
_Avoid_: "config", "request", "proposal" loosely — it is the specific approval-gated plan object (gate b).

**standup**:
Deploying the llm-d inference stack (model server + router) into the cluster so there is something to benchmark. Mutating, always approval-gated.
_Avoid_: "deploy", "setup", "install", "provision" — `standup` is the canonical verb for bringing up the stack.

**smoketest**:
A quick post-standup check that the freshly stood-up stack actually serves before committing to a full benchmark run.
_Avoid_: "test", "validation", "warmup".

**run**:
Executing the benchmark itself — driving the `llmdbenchmark` CLI (locally as a blocking subprocess via `execute_llmdbenchmark`, or as a Kubernetes Job via the orchestrator) to generate load and produce a report.
_Avoid_: "benchmark" as a verb is fine; avoid "execution", "job" (a Job is the orchestrator's K8s object specifically).

**teardown**:
Cleaning up the deployed stack / cluster resources after a run. Mutating, approval-gated.
_Avoid_: "cleanup" (used for orchestrator Job/ConfigMap cleanup), "destroy", "delete".

**spec**:
A specification template (`config/specification/**/*.yaml.j2`, e.g. `cicd/kind`, `guides/optimized-baseline`) in the benchmark repo that defines *how the llm-d stack is stood up*. One leg of the `<spec, harness, workload>` triplet.
_Avoid_: "template", "config", "deployment manifest", "scenario" (scenario is a use-case-level concept).

**harness**:
The load-generation engine that drives traffic at the stack (e.g. `inference-perf`, `guidellm`), living under `workload/harnesses/*`. One leg of the triplet; harnesses are comparable cross-harness on the same stack.
_Avoid_: "load generator" loosely, "tool", "driver", "benchmark".

**workload / profile**:
The traffic shape a harness generates — concurrency, token-length distribution, request pattern (`workload/profiles/{harness}/*.yaml.in`, e.g. `sanity_random.yaml`). One leg of the triplet.
_Avoid_: "load", "test case", "scenario", "config" — "workload" and "profile" are the canonical pair.

**Simulate Mode**:
A dry-run toggle (`SIMULATE=1`) where the agent walks the **whole** workflow (probe → plan → standup → run → report) but executes nothing — every command is a no-op returning synthetic success and a synthetic report. SIMULATE results must carry an unmistakable disclaimer wherever they appear.
_Avoid_: "dry-run" (that's the CLI's `--dry-run` preview, a different mechanism), "mock mode", "test mode".

### Benchmark concepts

**Benchmark Report v0.2**:
The schema-validated JSON results object produced by a benchmark run. Results are parsed **only** from a BR v0.2 object validated against the repo's own schema (gate d) — never scraped from logs. The schema is read live from the repo, never vendored.
_Avoid_: "the results", "output", "report" loosely — the version-specific schema name is load-bearing.

**TTFT (time to first token)**:
Latency until the first output token appears — the "responsiveness" a chat user feels. Reported in seconds in BR v0.2; narrated to users in milliseconds. Lower is better.
_Avoid_: "first-token latency" is acceptable; avoid "response time", "latency" unqualified.

**TPOT / ITL**:
Time per output token / inter-token latency — the streaming pace after the first token. Drives perceived "tokens/sec per user". Reported `s/token`.
_Avoid_: conflating with TTFT; "generation speed", "throughput" (throughput is system-wide).

**goodput**:
The throughput of requests that *also met their SLO* — useful capacity, not raw throughput. Always returned as an upper-bound **estimate** (`is_estimate=True`) because the report hides per-request correlation.
_Avoid_: "throughput", "good throughput", "successful rate" — goodput is specifically SLO-conditioned and estimated.

**SLO**:
A service-level objective — a latency/throughput threshold (e.g. p99 TTFT ≤ 500ms) that yields a PASS/FAIL verdict over the report's percentile ladder. Thresholds must be fixed **before** a run; loosening one after seeing results is a flagged post-hoc cherry-pick.
_Avoid_: "target" loosely, "SLA", "limit".

**Pareto frontier**:
The set of non-dominated (and SLO-feasible) treatments across a sweep's objective space — the configurations where you can't improve one objective without worsening another. Selected by the analyzer; plotted as a scatter in the results card.
_Avoid_: "best results", "optimal point" (it's a *frontier* of trade-offs, not one winner).

**DoE (Design of Experiments) / sweep**:
A parameter sweep — the cross-product of factor levels run as multiple treatments. Built as a **pure** cross-product (no benchmarking judgment in the builder); run locally via the CLI's native `experiment` subcommand or K8s-natively via the orchestrator's parallel Job path.
_Avoid_: "grid search", "batch run", "matrix" loosely — "sweep" and "DoE" are the canonical terms; one cell is a **treatment**.

**autotuner**:
A closed-loop goal-seeker (`autotune_search`) that adaptively searches configurations toward an SLO at best goodput. It only tracks/validates the agent's next candidate and surfaces convergence FACTS — it computes no next config and issues no stop verdict; that strategy lives in `knowledge/autotune_strategy.md`.
_Avoid_: "optimizer", "tuning loop" — and don't attribute the *decision* to the tool; the agent decides.

### Orchestrator & results

**orchestrator**:
The Kubernetes-native path that runs a benchmark as a managed **Job** (`orchestrate_benchmark_run`) rather than a blocking local subprocess — submit → watch → classify failure → retry/dead-letter → cleanup. It is **stateless**: the cluster (Job labels + a ConfigMap checkpoint) is the source of truth.
_Avoid_: "scheduler", "runner" (the runner is the subprocess executor), "controller" loosely.

**fault classification**:
The facts-only categorization of a failed Job into a priority-ordered `kind`: `timeout`, `oom`, `unschedulable`, `evicted`, `image_error`, `run_error`. The classification is mechanism; the remediation judgment lives in `knowledge/orchestrator.md`.
_Avoid_: "error type", "exception", "failure reason" generically — the enum is fixed and named.

**dead-letter**:
What happens to a **deterministic** fault (oom/unschedulable/image_error/timeout) or an exhausted budget — the treatment is set aside immediately rather than retried, so one bad treatment doesn't sink a sweep. Only **transient** faults (evicted) retry, as fresh distinct Jobs.
_Avoid_: "fail", "drop", "skip" — "dead-letter" is the specific give-up-and-isolate outcome.

**endpoint readiness gate**:
The pre-submit check (`check_endpoint_readiness`) that the inference endpoint is actually **serving** — a ready backing endpoint in a Service, per `kubectl get endpoints` — not just that a pod exists. The orchestrator refuses to submit against an unready endpoint. A failing readiness verdict is a non-overridable safety invariant.
_Avoid_: "health check", "liveness", "pod-exists check".

### Security & safety

**allowlist**:
The deny-by-default policy **data** (`security/allowlist.yaml`) that is the single source of truth for what commands may execute. `allowlist.py` is a pure validator with **zero** per-command Python branches; widening capability is a reviewed YAML edit, never code.
_Avoid_: "whitelist" (use allowlist), "permissions" loosely, "config" — it is the policy data specifically.

**approval gate / per-action approval**:
The rule that read-only probes auto-run but every **mutating** command blocks until the user clicks Approve on a card showing the exact `argv`. A flag like `--dry-run` (a `read_only_trigger`) downgrades a mutating command to an auto-running preview.
_Avoid_: "confirmation", "prompt", "permission check" — "approval gate" is the canonical term.

**read-only vs mutating**:
The two execution modes the allowlist computes for a command. Read-only commands auto-run and are never concurrency-capped; mutating commands require approval and count against the run semaphore.
_Avoid_: "safe/unsafe", "GET/POST", "passive/active".

**env scrubbing**:
Stripping secrets (LLM API key, HF token) from the subprocess environment so a child process never sees them — they reach a child only via explicit `extra_env`, never argv, never an emitted event. The browser never sees them either.
_Avoid_: "sanitizing", "env cleanup" — and don't confuse the secret-scrub with general env filtering.

**safety invariant**:
A gate that authority claims or framing **cannot** override — e.g. a failing readiness diagnostic, the SIMULATE disclaimer's prominence, refusing prompt-injection, holding pre-committed SLO thresholds. A user asserting "I'm the VP, skip it" changes nothing a diagnostic measured.
_Avoid_: "rule", "policy", "preference" — these are the specifically non-negotiable, non-overridable constraints.
