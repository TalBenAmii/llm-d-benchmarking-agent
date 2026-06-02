# ROADMAP v4 — benchmark feature-coverage gaps

> **Living document.** Derived 1:1 from [`docs/BENCHMARK_FEATURE_COVERAGE.md`](docs/BENCHMARK_FEATURE_COVERAGE.md):
> every 🟡/⬜ row in the coverage catalog becomes exactly one phase here, and every phase
> closes exactly one gap row (drop nothing, invent nothing). Re-derive when the benchmark
> docs (and therefore the catalog) change.
>
> **Integration branch.** Worked on `feature/roadmap-v4` (integration branch off `main`;
> **never `main`** during the effort). Numbering **continues contiguously from v3** (which
> ended at Phase 26), so v4 spans **Phases 27-58**. A future `roadmap-v4-autopilot.js` can
> consume this file directly: each phase carries a GOAL / BUILD / ACCEPTANCE / HERMETIC-TEST
> skeleton.

## Status legend
`TODO` · `IN-PROGRESS` · `DONE` · `DEFERRED`

> **Thin code, thick agent** stays the law: mechanism in Python (flags, allowlist data,
> validation), *judgment* in `knowledge/` — never `if/elif` decision logic in Python.
> The two sibling repos stay **read-only**; allowlist widening is **data only**.

---

## Phase 27 — Default-enable benchmark `--monitoring` + surface `results.observability` — TODO
*Catalog ref: Area F — "Benchmark metrics collection (--monitoring)" (🟡, the headline gap). The activation half of the Phase 25 consumer.*

- **GOAL:** stop shipping empty `results.observability`. Activate the metrics *producer*
  (`--monitoring` / `metricsScrapeEnabled`) so the KV-cache hit rate, schedule delay
  (queue-depth proxy), GPU utilization, and replica/startup/EPP-log snapshots the report
  already knows how to parse actually appear — default ON, with a knowledge-driven opt-out
  for clusters lacking the Prometheus-operator CRDs.
- **BUILD:**
  - Add a `monitoring` flag to `ExecuteInput.flags` (`app/tools/schemas.py`).
  - Emit `--monitoring` / `--no-monitoring` in `build_argv` (`app/tools/execute.py`) for
    `standup`, `run`, and `experiment`.
  - Widen `security/allowlist.yaml` (DATA only) to permit `--monitoring`/`--no-monitoring`
    under those subcommands.
  - Default **ON**, with a knowledge-driven opt-out (emit `--no-monitoring` /
    `monitoring.installPrometheusCrds`) for Kind / CRD-less clusters, keyed on a
    `probe_environment` CRD check.
  - Surface the parsed `results.observability` metrics in the report / analysis summary,
    and document the procedure in `knowledge/observability.md` + `knowledge/results_interpretation.md`.
- **ACCEPTANCE:** a `run` / `standup` emits `--monitoring` (allowlist-approved); when scraping
  ran, `results.observability` KV-cache / GPU / queue metrics appear in the summary;
  `--no-monitoring` opt-out works on a CRD-less cluster; all decision logic lives in
  `knowledge/`, not Python.
- **HERMETIC-TEST:** assert `build_argv` emits `--monitoring`/`--no-monitoring` per
  subcommand; assert the allowlist permits exactly those flags; drive a fixture BR v0.2 with a
  populated `results.observability` through `summarize_report`/`analyze_results` and assert the
  metrics surface; assert the opt-out path is selected from a probed no-CRD environment.

## Phase 28 — First-class model override (-m/--models) — TODO
*Catalog ref: Area A — "Model list selection (-m/--models)" (🟡).*

- **GOAL:** let the agent select a model per standup rather than only via the chosen spec.
- **BUILD:** add a `models` field to `ExecuteInput` → emit `-m/--models` in `build_argv`
  (`app/tools/execute.py`); widen `security/allowlist.yaml` (DATA) for `-m`; keep model intent
  consistent with the capacity pre-flight (`CheckCapacityInput`); guidance in `knowledge/`.
- **ACCEPTANCE:** a standup can run with an explicit `-m` model not pinned by the spec; the
  capacity pre-flight sees the same model; catalog grounding still validates the name.
- **HERMETIC-TEST:** `build_argv` emits `-m`; allowlist permits it; capacity input mirrors the
  override.

## Phase 29 — Explicit cluster access (-k/--kubeconfig, URL, token) — TODO
*Catalog ref: Area A — "Cluster access / authentication" (🟡).*

- **GOAL:** target a remote cluster instead of relying only on the ambient kube context.
- **BUILD:** model `kubeconfig` / `cluster.url` / `cluster.token` on `ExecuteInput`; emit
  `-k`/URL where the CLI accepts them; keep tokens backend-only + scrubbed (`app/config.py:child_env`);
  widen `security/allowlist.yaml` (DATA); judgment in `knowledge/preconditions.md`.
- **ACCEPTANCE:** a non-default kubeconfig/URL is threaded into the CLI call; the token never
  reaches the browser or logs.
- **HERMETIC-TEST:** `build_argv` emits `-k`/URL; secret-scrub test asserts the token is absent
  from emitted env + command events.

## Phase 30 — HuggingFace gated-model secret provisioning — TODO
*Catalog ref: Area A — "HuggingFace token / gated-model auth" (🟡).*

- **GOAL:** make gated-model standups work, not just the capacity lookup.
- **BUILD:** surface `huggingface.enabled` + provision the cluster HF secret as an
  approval-gated mutating step (via allowlisted `kubectl`); keep the token backend-only;
  judgment ("when is a gated model in scope") in `knowledge/`.
- **ACCEPTANCE:** a gated-model plan provisions the HF secret before standup; the token is
  never exposed; non-gated flows are unchanged.
- **HERMETIC-TEST:** the secret-provision command is approval-gated + allowlisted; scrub test
  asserts the token never appears in events.

## Phase 31 — First-class step selection / re-run (-s/--step) — TODO
*Catalog ref: Area A — "Standup/run step list and re-run individual steps (-s/--step)" (🟡, currently only via extra passthrough).*

- **GOAL:** promote step selection from the raw `extra` passthrough to a modeled, advisory flag.
- **BUILD:** add a `step` field to `ExecuteInput.flags` → emit `-s/--step` (incl. ranges like
  `3-5`, `5,7`) in `build_argv`; widen `security/allowlist.yaml` (DATA); add step-list guidance
  to `knowledge/` so the agent can re-run a single failed step.
- **ACCEPTANCE:** the agent can re-run a step range as a modeled flag (not via `extra`).
- **HERMETIC-TEST:** `build_argv` emits `-s` with a range; allowlist permits it.

## Phase 32 — Gateway class / provider selection (--gateway-class) — TODO
*Catalog ref: Area A — "Gateway class / provider selection (istio/agentgateway/gke/epponly)" (⬜).*

- **GOAL:** let the agent choose the gateway provider instead of inheriting it from the spec.
- **BUILD:** model `gateway.className` → emit `--gateway-class` in `build_argv`; widen
  `security/allowlist.yaml` (DATA) with the provider enum (istio/agentgateway/gke/epponly);
  add provider-selection guidance to `knowledge/` (when to pick which).
- **ACCEPTANCE:** a standup can override the gateway provider; the choice is grounded in
  knowledge, not Python branches.
- **HERMETIC-TEST:** `build_argv` emits `--gateway-class` from the enum; allowlist validates it.

## Phase 33 — Multi-stack scenarios + --stack subset + --parallel — TODO
*Catalog ref: Area A — "Multi-stack scenarios (N models behind one gateway)" (⬜).*

- **GOAL:** target a subset of stacks and cap per-pool parallelism for multi-stack specs.
- **BUILD:** model `--stack NAME[,NAME...]` + `--parallel` → emit in `build_argv`; widen
  `security/allowlist.yaml` (DATA); add multi-stack run guidance to `knowledge/`.
- **ACCEPTANCE:** the agent can run/target one stack of a multi-stack spec and cap parallelism.
- **HERMETIC-TEST:** `build_argv` emits `--stack`/`--parallel`; allowlist permits them.

## Phase 34 — Workload Variant Autoscaler (WVA) enablement (-u/--wva) — TODO
*Catalog ref: Area A/WVA — "WVA enablement" (🟡; OpenShift-only, out of the kind/CPU MVP).*

- **GOAL:** toggle WVA, tune HPA/VA knobs, and interpret the WVA smoketests.
- **BUILD:** model `-u/--wva` (+ `wva.controller/variantAutoscaling/hpa.*`) → emit in
  `build_argv`; widen `security/allowlist.yaml` (DATA); add the OpenShift gate, the 8 WVA
  smoketest interpretations, and teardown semantics to `knowledge/welllit_path_advisor.yaml` +
  `knowledge/`. **DEFERRED if the platform is Kind/CPU** (WVA is OpenShift-only).
- **ACCEPTANCE:** on an OpenShift target the agent can enable WVA and read its smoketests; on
  Kind it advises that WVA is out of scope (knowledge-driven).
- **HERMETIC-TEST:** `build_argv` emits `-u/--wva`; allowlist permits it; the OpenShift gate is
  honored from a probed platform.

## Phase 35 — Standup monitoring flag (PodMonitor/ServiceMonitor + EPP verbosity) — TODO
*Catalog ref: Area A/F — "Standup monitoring flag" (🟡). Rides on Phase 27's activation.*

- **GOAL:** ensure the standup-level `--monitoring` (PodMonitor/ServiceMonitor creation + EPP
  verbosity) is explicitly modeled and surfaced, including the `--no-monitoring` escape.
- **BUILD:** confirm the Phase 27 `monitoring` flag is wired into `standup` specifically;
  expose `monitoring.podmonitor.enabled` / `monitoring.installPrometheusCrds` guidance in
  `knowledge/observability.md`.
- **ACCEPTANCE:** a standup creates PodMonitor/ServiceMonitor when monitoring is on, and skips
  cleanly with `--no-monitoring` on CRD-less clusters.
- **HERMETIC-TEST:** standup `build_argv` emits the monitoring flag; CRD-less opt-out selected
  from a probed environment.

## Phase 36 — First-class skip / collect-only mode (-z/--skip) — TODO
*Catalog ref: Area A/C — "Skip experiment execution / collect existing results (-z/--skip)" (🟡, via extra passthrough only).*

- **GOAL:** promote collect/analyze-only from `extra` to a modeled flag.
- **BUILD:** add a `skip` field to `ExecuteInput.flags` → emit `-z/--skip` in `build_argv`;
  widen `security/allowlist.yaml` (DATA); document the collect-only flow in `knowledge/`.
- **ACCEPTANCE:** the agent can re-collect/analyze existing results without re-running the load.
- **HERMETIC-TEST:** `build_argv` emits `-z`; allowlist permits it.

## Phase 37 — Harness debug mode (-d/--debug, sleep infinity) — TODO
*Catalog ref: Area A/C — "Harness debug mode" (⬜).*

- **GOAL:** support the interactive sleep-infinity debug pod within the approval-gated flow.
- **BUILD:** model `-d/--debug` → emit in `build_argv`; widen `security/allowlist.yaml` (DATA)
  as an approval-gated mutating action; add debug-workflow guidance to `knowledge/` (and the
  boundary: interactive in-pod exec stays a manual, user-driven step).
- **ACCEPTANCE:** the agent can launch a debug harness pod (approval-gated) and explain how to
  exec into it, without driving the interactive shell itself.
- **HERMETIC-TEST:** `build_argv` emits `-d`; the debug launch is approval-gated + allowlisted.

## Phase 38 — Model the CLI's per-phase timeouts (--wait/--*-timeout) — TODO
*Catalog ref: Area A — "Harness wait / data-access / deploy timeouts" (🟡; today governed only at the runner/orchestrator layer).*

- **GOAL:** thread the CLI's own per-phase timeout flags instead of relying solely on the
  runner/orchestrator deadlines.
- **BUILD:** model `-s/--wait`, `--wait-timeout`, `--data-access-timeout`,
  `--*-deploy-timeout`, `--pvc-bind-timeout` on `ExecuteInput.flags` → emit in `build_argv`;
  widen `security/allowlist.yaml` (DATA); reconcile with the existing `timeout_s` policy +
  `active_deadline_seconds` so the two layers don't fight; guidance in `knowledge/`.
- **ACCEPTANCE:** a slow-deploy scenario can set a longer CLI deploy timeout; the runner
  deadline still bounds the whole process.
- **HERMETIC-TEST:** `build_argv` emits the timeout flags; allowlist permits them; runner
  deadline still applies.

## Phase 39 — Cloud results sink for the run flag (-r gs://, s3://) — TODO
*Catalog ref: Area A/C — "Run results destination / cloud upload (-r/--output)" (🟡; gs/s3 deliberately not allowlisted).*

- **GOAL:** let users with a bucket send run results to GCS/S3 instead of local-only.
- **BUILD:** keep local default; widen `security/allowlist.yaml` (DATA) to permit
  `gs://`/`s3://` destinations on `-r/--output` (guarded, opt-in); add a "do you have a
  bucket?" elicitation to `knowledge/`.
- **ACCEPTANCE:** a user can opt into a `gs://`/`s3://` results sink; the default stays local.
- **HERMETIC-TEST:** `build_argv` emits `-r gs://...`; allowlist permits the cloud scheme only
  when opted in; local stays the default.

## Phase 40 — Trigger the CLI's local --analyze plot families — TODO
*Catalog ref: Area A/C/E — "Local analysis after collection (--analyze)" (🟡).*

- **GOAL:** generate the CLI's optional workstation matplotlib plot families (per-request
  distributions, session-lifecycle, Prometheus time-series) in addition to the harness PNGs.
- **BUILD:** model `--analyze` → emit in `build_argv`; widen `security/allowlist.yaml` (DATA);
  surface the generated plot families alongside the harness charts via the artifact endpoint;
  guidance in `knowledge/analysis.md`.
- **ACCEPTANCE:** `--analyze` runs and the extra plot families are surfaced in the UI; the
  agent's own SLO/goodput/Pareto math is unchanged.
- **HERMETIC-TEST:** `build_argv` emits `--analyze`; allowlist permits it; the artifact lister
  surfaces the new PNG families from a fixture results dir.

## Phase 41 — Dataset replay URL (-x/--dataset) — TODO
*Catalog ref: Area A — "Dataset replay URL (-x/--dataset)" (⬜).*

- **GOAL:** support replaying a real dataset instead of only synthetic workload profiles.
- **BUILD:** model `-x/--dataset` (+ `REPLACE_ENV_LLMDBENCH_RUN_DATASET_DIR`) → emit in
  `build_argv`; widen `security/allowlist.yaml` (DATA); add dataset-vs-synthetic guidance to
  `knowledge/`.
- **ACCEPTANCE:** the agent can run a dataset-replay workload; synthetic profiles still work.
- **HERMETIC-TEST:** `build_argv` emits `-x`; allowlist permits it.

## Phase 42 — Round-trip the CLI's run-config (--generate-config / -c) — TODO
*Catalog ref: Area A — "Generate / reuse a run config YAML (--generate-config / -c)" (🟡).*

- **GOAL:** use the CLI's own `--generate-config` / `-c` reuse mechanism (in addition to the
  agent's in-workspace `write_and_validate_config`).
- **BUILD:** model `--generate-config` + `-c/--config` → emit in `build_argv`; widen
  `security/allowlist.yaml` (DATA); store/reuse the generated config under `--workspace`;
  guidance in `knowledge/`.
- **ACCEPTANCE:** the agent can generate a run-config with the CLI and replay it via `-c`.
- **HERMETIC-TEST:** `build_argv` emits `--generate-config` then `-c`; allowlist permits both.

## Phase 43 — Administrative privilege / --non-admin skip — TODO
*Catalog ref: Area A/I — "Administrative privilege requirement / --non-admin skip" (⬜).*

- **GOAL:** support namespace-only (non-cluster-admin) operation for shared clusters.
- **BUILD:** model `--non-admin` / `-i` → emit in `build_argv`; widen `security/allowlist.yaml`
  (DATA); add a probe of cluster-admin vs namespace-only and the consequent guidance to
  `knowledge/preconditions.md`. **DEFERRED for the kind MVP** (cluster-admin by default).
- **ACCEPTANCE:** on a namespace-scoped cluster the agent emits `--non-admin` and skips
  cluster-scoped steps; the kind path is unchanged.
- **HERMETIC-TEST:** `build_argv` emits `--non-admin`; allowlist permits it; the skip is chosen
  from a probed non-admin context.

## Phase 44 — Telemetry push (queue-based async usage reporting) — TODO
*Catalog ref: Area A/F — "Telemetry push" (⬜; off by default upstream).*

- **GOAL:** optionally enable the CLI's telemetry push for organizations that want it.
- **BUILD:** model `--telemetry-enabled` / `--telemetry-provider=http` (+
  `LLMDBENCH_TELEMETRY_*`) → emit in `build_argv`; widen `security/allowlist.yaml` (DATA),
  default OFF; keep endpoints/keys backend-only; guidance + privacy note in `knowledge/`.
  **DEFERRED unless a user explicitly opts in** (the agent ships its own `/metrics`).
- **ACCEPTANCE:** telemetry is OFF by default; a user can opt in; endpoints stay backend-only.
- **HERMETIC-TEST:** default emits no telemetry flag; opt-in emits it; scrub test on the endpoint.

## Phase 45 — Author per-knob vLLM scenario overrides — TODO
*Catalog ref: Area B — "vLLM tuning knobs (command gen, flags, ports, KV-transfer, accelerator, affinity, storage, scheduling)" (🟡; today only via DoE sweeps / capacity knobs).*

- **GOAL:** let the agent author finer vLLM/scheduling/storage scenario edits beyond the
  parallelism/memory knobs already in capacity + DoE.
- **BUILD:** extend the in-workspace config authoring (`app/tools/config_artifact.py`) to set
  `vllmCommon.flags.*`, `servicePort/port`, `kvTransfer.*`, `affinity.*`, `priorityClassName`,
  `schedulerName`, `ephemeralStorage`, `networkResource`; validate via the CLI `--dry-run`/plan;
  WHICH knobs to set lives in `knowledge/` (the repos stay read-only — author into the session
  workspace, never the spec).
- **ACCEPTANCE:** the agent can produce a validated scenario with custom vLLM/scheduling knobs;
  the determinism gate (plan/--dry-run) passes.
- **HERMETIC-TEST:** authored config sets the knobs + passes structural validation against the
  repo's example shape; no write into the read-only repo.

## Phase 46 — Kustomize deploy config block (kustomize.*) — TODO
*Catalog ref: Area H — "Kustomize deploy method config block (-t kustomize)" (🟡; only the bare method is allowlisted).*

- **GOAL:** author the kustomize config block (guideName/repoPath/repoRef/patches/overlays/
  extraHelmValues/guideVariableOverrides), not just select `-t kustomize`.
- **BUILD:** extend config authoring to emit the `kustomize.*` block into the session
  workspace; thread `--llmd-repo-path`; validate via plan/--dry-run; guidance in
  `knowledge/deploy_path_playbook.md`.
- **ACCEPTANCE:** the agent can author + validate a kustomize-method scenario with a guide +
  patches; the determinism gate passes.
- **HERMETIC-TEST:** authored kustomize block validates against the example shape; `-t kustomize`
  still allowlisted.

## Phase 47 — Cloud results upload internals (GCS/S3 helpers) — TODO
*Catalog ref: Area H — "Cloud results upload internals (cloud_upload.py)" (⬜; pairs with Phase 39).*

- **GOAL:** support the upload-side mechanics (`gcloud storage cp` / `aws s3 cp`) for the
  cloud results sink.
- **BUILD:** widen `security/allowlist.yaml` (DATA) to permit the upload helpers as
  approval-gated mutating actions; reuse the CLI's `cloud_upload.py` rather than reimplement;
  guidance in `knowledge/`.
- **ACCEPTANCE:** when a `gs://`/`s3://` sink is opted in, the upload helper runs
  (approval-gated); local stays default and is a no-op.
- **HERMETIC-TEST:** upload command is approval-gated + allowlisted; local path is a no-op.

## Phase 48 — Parse session_performance metrics (multi-turn) — TODO
*Catalog ref: Area E — "session_performance metrics (multi-turn sessions)" (⬜).*

- **GOAL:** parse and surface the `results.session_performance` stats block for multi-turn
  inference-perf workloads (session_rate, session_duration, events/tokens per session).
- **BUILD:** extend `app/validation/report.py` to mechanically extract
  `results.session_performance` (gracefully `None` when absent); surface in the summary +
  `analyze_results`; field-name discovery as DATA in `knowledge/standard_metrics.yaml` (thin
  code / thick agent); never fabricate.
- **ACCEPTANCE:** a multi-turn BR v0.2 report surfaces session metrics; single-turn reports are
  unchanged (no fabrication).
- **HERMETIC-TEST:** fixture multi-turn BR v0.2 surfaces session_performance; a single-turn
  report yields `None` without error.

## Phase 49 — Surface results.observability serving metrics end-to-end — TODO
*Catalog ref: Area E/F — "Standard resource/serving metrics from results.observability" (🟡). Consumer ships; depends on Phase 27 producer activation.*

- **GOAL:** ensure the KV-cache hit rate / schedule delay / GPU utilization the consumer
  already parses are visibly surfaced once Phase 27 populates `results.observability`.
- **BUILD:** wire the existing `_extract_standard_metric` output into the report card +
  `analyze_results` summary + the history/trend store; keep them informational Pareto
  objectives (out of dominance); document interpretation in `knowledge/results_interpretation.md`.
- **ACCEPTANCE:** with monitoring on (Phase 27), the serving metrics appear in the summary,
  the report card, and the trend store; goodput/SLO/Pareto dominance is unchanged.
- **HERMETIC-TEST:** populated `results.observability` fixture surfaces the metrics across
  summary/analysis/history; dominance unaffected.

## Phase 50 — Results Store (git-like result mgmt: remotes/push/pull) — TODO
*Catalog ref: Area E — "Results Store (init/remote/status/add/push/ls/pull)" (🟡; the need is met by the agent's own history store).*

- **GOAL:** optionally interoperate with the CLI's git-like result store (remotes + push/pull
  to GCS) for teams that share results that way.
- **BUILD:** model the `results` store subcommands (`init`/`remote`/`status`/`add`/`push`/`ls`/
  `pull`) → emit in `build_argv`; widen `security/allowlist.yaml` (DATA) as approval-gated;
  bridge to the agent's existing `result_history`; guidance in `knowledge/history.md`.
  **DEFERRED unless a user wants the CLI's remote store** (the local history store covers the need).
- **ACCEPTANCE:** a user can publish/pull results via the CLI store; the local history store is
  unchanged.
- **HERMETIC-TEST:** `build_argv` emits the store subcommands; push/pull are approval-gated +
  allowlisted.

## Phase 51 — Jupyter / standalone plotting scripts surfacing — TODO
*Catalog ref: Area E — "Jupyter analysis notebook / standalone plotting scripts" (⬜).*

- **GOAL:** point users at (and optionally drive) the interactive notebook / experimental
  plotting scripts without making them part of the automated flow.
- **BUILD:** add guidance + the artifact paths to `knowledge/analysis.md` (the notebook lives
  in the read-only repo); optionally allowlist the standalone plot scripts as read-only
  generators against a results dir. **DEFERRED if it's purely exploratory** (the agent already
  surfaces the harness PNGs + does its own analysis).
- **ACCEPTANCE:** the agent can explain / point at the notebook and (optionally) run a
  standalone plot script against a results dir.
- **HERMETIC-TEST:** if scripted, the plot script is allowlisted read-only against a fixture
  results dir.

## Phase 52 — Multi-turn trace replay benchmark (experimental) — TODO
*Catalog ref: Area C — "Multi-turn trace replay benchmark (experimental)" (⬜; pairs with Phase 48).*

- **GOAL:** support the experimental trace-replay benchmark (`--trace-file` JSONL,
  TTFT-by-turn-buckets report).
- **BUILD:** model the trace-replay invocation → allowlist (DATA) the experimental script as
  approval-gated; parse the TTFT-by-turn-buckets output (pairs with Phase 48
  session_performance); guidance in `knowledge/`. **DEFERRED while it stays experimental upstream.**
- **ACCEPTANCE:** the agent can run a trace replay against a JSONL trace and surface the
  by-turn report.
- **HERMETIC-TEST:** the replay invocation is allowlisted; a fixture trace yields a parsed
  by-turn report.

## Phase 53 — convert-guide (guide → scenario/experiment file generation) — TODO
*Catalog ref: Area C — "convert-guide skill" (⬜; constrained by the read-only-repo rule).*

- **GOAL:** generate a benchmark scenario/experiment file from an arbitrary llm-d guide.
- **BUILD:** author `ai.<name>.sh` / `ai.<name>.yaml` from a guide URL/path **into the session
  workspace** (never the read-only repo), using the `LLMDBENCH_*` mappings as DATA in
  `knowledge/`; validate via plan/--dry-run. **DEFERRED / scoped** because conversion canonically
  writes into the read-only benchmark repo; the agent's variant must write to the workspace only.
- **ACCEPTANCE:** the agent can produce a validated workspace-local scenario from a guide; no
  write into the read-only repo.
- **HERMETIC-TEST:** generated scenario validates against the example shape; asserts no write
  outside the workspace.

## Phase 54 — Distributed tracing config (OpenTelemetry tracing: block) — TODO
*Catalog ref: Area F — "Distributed tracing config" (⬜).*

- **GOAL:** let advanced users configure a scenario `tracing:` block (endpoint, sampling rate,
  service names) for an external OTel backend.
- **BUILD:** author the `tracing:` block into the workspace scenario; validate via plan/--dry-run;
  guidance in `knowledge/observability.md` (note: the benchmark configures, never collects,
  traces — collection is the user's OTel backend).
- **ACCEPTANCE:** the agent can author a validated `tracing:` block; the limitation (config only)
  is explained.
- **HERMETIC-TEST:** authored tracing block validates against the example shape.

## Phase 55 — Real-time metric streaming / custom Prometheus queries — TODO
*Catalog ref: Area F — "Real-time metric streaming / custom Prometheus queries" (⬜; explicitly unimplemented upstream).*

- **GOAL:** track the upstream feature and provide the agent's best-available equivalent.
- **BUILD:** document in `knowledge/observability.md` that benchmark metric streaming is
  upstream-unimplemented; point at the agent's live coverage (`observe_run_metrics` via
  `kubectl top` + real-time pod log streaming) as the substitute. **DEFERRED until upstream
  implements it.**
- **ACCEPTANCE:** the agent answers "can you stream live benchmark metrics?" honestly and
  offers `kubectl top` + log streaming instead.
- **HERMETIC-TEST:** a knowledge assertion that the feature is upstream-unimplemented + the
  substitute is named.

## Phase 56 — Stack discovery tool (llm-d-discover) — TODO
*Catalog ref: Area I — "Stack discovery tool (llm-d-discover)" (⬜).*

- **GOAL:** optionally invoke the standalone stack-discovery tool (URL → live stack config,
  BR-v0.2 output) for richer environment capture than the agent's endpoint probing.
- **BUILD:** model the `llm-d-discover <url> --output-format benchmark-report` invocation →
  allowlist (DATA) as read-only (it has its own env-var redaction + read-only RBAC); feed its
  BR-v0.2 output into the report path; guidance in `knowledge/`. **DEFERRED — the agent's
  endpoint/readiness probing covers the practical need.**
- **ACCEPTANCE:** the agent can run discovery and consume its BR-v0.2 output; endpoint probing
  still works as the default.
- **HERMETIC-TEST:** the discovery invocation is allowlisted read-only; a fixture BR-v0.2 output
  flows through the report path.

## Phase 57 — flexibility.md placeholder doc — TODO  *(doc-completeness; DEFERRED)*
*Catalog ref: Area I — "flexibility.md (placeholder doc)" (⬜; an empty upstream stub "To be populated.").*

- **GOAL:** track the empty upstream stub so it can't silently become a dropped doc.
- **BUILD:** none until upstream populates it. **DEFERRED — zero substantive features today.**
- **ACCEPTANCE:** re-run the catalog when upstream populates `docs/flexibility.md`; promote any
  real feature it documents into a new gap row + phase.
- **HERMETIC-TEST:** n/a (no feature to cover); the catalog re-derivation guards against drift.

## Phase 58 — FAQ / RBAC-audit placeholder docs — TODO  *(doc-completeness; DEFERRED)*
*Catalog ref: Area I — "FAQ / RBAC-audit placeholder docs" (⬜; empty upstream stubs).*

- **GOAL:** track `docs/faq.md` + `util/rbac_audit_report.md` so they can't become dropped docs.
- **BUILD:** none until upstream populates them. **DEFERRED — empty stubs with no features.**
- **ACCEPTANCE:** re-run the catalog when upstream populates these; promote any real feature
  into a new gap row + phase.
- **HERMETIC-TEST:** n/a; catalog re-derivation guards against drift.

---

## Autonomous execution rules (self-imposed)
- **Branching:** `feature/roadmap-v4` is the integration branch (off `main`). Each phase is
  developed on a `feature/roadmap-v4-pN-<slug>` branch in an isolated worktree and merged in
  after its full-suite gate is green. **`main` is never touched** during the effort.
- **Tests:** pytest only, hermetic. No long/real benchmark runs, no GPU, no live cluster. The
  orchestrator is validated with the fake kube client + the CaptureRunner harness.
- **Allowlist widening is DATA only** (`security/allowlist.yaml`); no per-command Python.
- **Thin code, thick agent:** mechanism in Python, judgment in `knowledge/`.
- **Docs:** update `docs/BENCHMARK_FEATURE_COVERAGE.md` (flip the closed row's emoji) +
  this file's phase STATUS at the end of every phase.
