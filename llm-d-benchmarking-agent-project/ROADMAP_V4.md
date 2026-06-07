# ROADMAP v4 — benchmark feature-coverage gaps

> **Living document.** Two sources feed this roadmap: **(1)** every 🟡/⬜ row in
> [`docs/BENCHMARK_FEATURE_COVERAGE.md`](docs/BENCHMARK_FEATURE_COVERAGE.md) — the benchmark-CLI
> coverage catalog (Phases 27–58); and **(2)** the upstream **deploy-stack** capabilities surfaced by
> [`docs/USEFUL_REPO_DOCS.md`](docs/USEFUL_REPO_DOCS.md) — the `llm-d` docs (well-lit paths,
> readiness/infra preconditions, EPP/Gateway routing, accelerators, provider packs) that the
> benchmark-only catalog never mined (Phases 59–66). Re-derive when **either** source changes.
>
> Numbering continues contiguously from v3 (which ended at Phase 26), so v4 spans **Phases 27–66**.
>
> **STATUS (2026-06-07): all 32 active phases are implemented and merged to `main`.** The only
> open items are the **7 DEFERRED phases** below (environment-gated / experimental / placeholder),
> which stay off the active line until their precondition lands (a non-Kind target, an opt-in, or
> upstream populating an empty stub). The completed phases are listed compactly in the
> **Completed (merged)** ledger; their full GOAL/BUILD/ACCEPTANCE/HERMETIC-TEST skeletons and
> per-phase result notes live in git history (the `feature/roadmap-v4` integration branch), and the
> shipped capabilities are inventoried in [`FEATURES.md`](FEATURES.md). This file was slimmed from
> its 818-line working form once the active line emptied; re-expand a phase here only if it reopens.

## Status legend
`TODO` · `IN-PROGRESS` · `DONE` · `DEFERRED`

> **Thin code, thick agent** stays the law: mechanism in Python (flags, allowlist data,
> validation), *judgment* in `knowledge/` — never `if/elif` decision logic in Python.
> The two sibling repos stay **read-only**; allowlist widening is **data only**.

---

## Completed (merged) — Phases 27–66

> All DONE. Full BUILD/ACCEPTANCE detail + result notes are in git history (`feature/roadmap-v4`);
> the shipped features are inventoried in `FEATURES.md` and the coverage catalog flips in
> `docs/BENCHMARK_FEATURE_COVERAGE.md`. Listed here as a one-line ledger only.

**Benchmark-CLI coverage (27–58)**
- **27** — Default-enable benchmark `--monitoring` + surface `results.observability` *(incl. merged Phase 35: standup PodMonitor/ServiceMonitor + EPP verbosity)*
- **28** — First-class model override (`-m`/`--models`)
- **29** — Explicit cluster access (`-k`/`--kubeconfig`, URL, token)
- **30** — HuggingFace gated-model secret provisioning
- **31** — First-class step selection / re-run (`-s`/`--step`)
- **32** — Gateway class / provider selection (`--gateway-class`)
- **33** — Multi-stack scenarios + `--stack` subset + `--parallel`
- **35** — Standup monitoring flag *(merged into Phase 27)*
- **36** — First-class skip / collect-only mode (`-z`/`--skip`)
- **37** — Harness debug mode (`-d`/`--debug`, sleep infinity)
- **38** — Model the CLI's per-phase timeouts (`--wait`/`--*-timeout`)
- **39** — Cloud results sink for the run flag (`-r gs://`, `s3://`)
- **40** — Trigger the CLI's local `--analyze` plot families
- **41** — Dataset replay URL (`-x`/`--dataset`)
- **42** — Round-trip the CLI's run-config (`--generate-config` / `-c`)
- **45** — Author per-knob vLLM scenario overrides (flags/ports/kvTransfer/affinity/priorityClass/scheduler/storage)
- **46** — Kustomize deploy config block (`kustomize.*`)
- **48** — Parse `session_performance` metrics (multi-turn)
- **49** — Surface `results.observability` serving metrics end-to-end (incl. the 3 standard metrics in the trend store)
- **50** — Results Store (git-like result mgmt: remotes/push/pull)
- **51** — Jupyter / standalone plotting scripts surfacing

**Deploy-stack coverage (59–66, from `docs/USEFUL_REPO_DOCS.md`)**
- **53** — convert-guide (guide → scenario/experiment file generation)
- **54** — Distributed tracing config (OpenTelemetry `tracing:` block)
- **55** — Real-time metric streaming / custom Prometheus queries
- **56** — Stack discovery tool (`llm-d-discover`)
- **59** — Model-load readiness gate: poll `/v1/models` vs `/health` with stuck-pod load-timing diagnostics
- **60** — Infra precondition gate: K8s server version + vLLM/NIXL image minimums + sidecar gotcha
- **61** — Right-size the harness launcher CPU request for small/Kind clusters (`LLMDBENCH_HARNESS_CPU_NR`)
- **62** — Gated-model access pre-flight (`check_model_access` / `GatedStatus` before standup)
- **63** — Accelerator + CPU-inferencing precondition advisor (GPU-advertised? DRA vs device-plugin? 64c/64GB-per-replica?)
- **64** — Provider-aware precondition pack (oc-vs-kubectl, GPU taints/tolerations, GMP/known-issues)
- **65** — Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool Accepted/ResolvedRefs)
- **66** — EPP HTTP-header decoder (interpret 429s + `x-llm-d-request-dropped-reason`)

*(Phases 53–56 are benchmark-CLI rows that ranked alongside the deploy-stack batch; grouped here only for brevity.)*

---

## Remaining work — the 7 DEFERRED phases

These are tracked but off the active line: each is environment-gated, experimental, or a
placeholder for an empty upstream stub. Promote one back onto the active line only when its
precondition lands (a non-Kind/shared-cluster target, an explicit opt-in, or upstream populating
the doc).

| Phase | Status | Reason |
|---|---|---|
| 34 — WVA enablement (`-u/--wva`) | DEFERRED | WVA is OpenShift-only (HPA/VA, 8 WVA smoketests) and explicitly out of the Kind/CPU MVP per its own summary; -u/--wva is correctly absent. Defer until a non-Kind target lands. |
| 43 — Administrative privilege / `--non-admin` skip | DEFERRED | No --non-admin/cluster-admin probing exists; the Kind MVP runs cluster-admin by default, making this a shared-cluster-only concern. Defer until a non-Kind/shared-cluster target lands. |
| 44 — Telemetry push (CLI usage reporting) | DEFERRED | The agent already exposes its own Prometheus /metrics (app/observability/metrics.py, GET /metrics); the CLI's opt-in HTTP telemetry push adds no coverage and is OFF-by-default by design. Defer until a user opts in. |
| 47 — Cloud results upload internals (GCS/S3 helpers) | DEFERRED | No gcloud/aws upload helpers are allowlisted and local default is a no-op; only matters on cloud targets and pairs with the (low-impact) Phase 39. Defer for the Kind MVP. |
| 52 — Multi-turn trace replay benchmark (experimental) | DEFERRED | No trace-file/trace-replay references in app/ or knowledge/; experimental upstream and pairs with Phase 48. Defer until upstream stabilizes it. |
| 57 — `flexibility.md` placeholder doc | DEFERRED | flexibility.md is an empty upstream stub with zero substantive features; nothing to implement until upstream populates it. |
| 58 — FAQ / RBAC-audit placeholder docs | DEFERRED | faq.md + rbac_audit_report.md are empty upstream stubs with no features to build; track only, defer until upstream populates them. |

## Phase 34 — Workload Variant Autoscaler (WVA) enablement (-u/--wva) — DEFERRED
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

## Phase 43 — Administrative privilege / --non-admin skip — DEFERRED
*Catalog ref: Area A/I — "Administrative privilege requirement / --non-admin skip" (⬜).*

- **GOAL:** support namespace-only (non-cluster-admin) operation for shared clusters.
- **BUILD:** model `--non-admin` / `-i` → emit in `build_argv`; widen `security/allowlist.yaml`
  (DATA); add a probe of cluster-admin vs namespace-only and the consequent guidance to
  `knowledge/preconditions.md`. **DEFERRED for the kind MVP** (cluster-admin by default).
- **ACCEPTANCE:** on a namespace-scoped cluster the agent emits `--non-admin` and skips
  cluster-scoped steps; the kind path is unchanged.
- **HERMETIC-TEST:** `build_argv` emits `--non-admin`; allowlist permits it; the skip is chosen
  from a probed non-admin context.

## Phase 44 — Telemetry push (queue-based async usage reporting) — DEFERRED
*Catalog ref: Area A/F — "Telemetry push" (⬜; off by default upstream).*

- **GOAL:** optionally enable the CLI's telemetry push for organizations that want it.
- **BUILD:** model `--telemetry-enabled` / `--telemetry-provider=http` (+
  `LLMDBENCH_TELEMETRY_*`) → emit in `build_argv`; widen `security/allowlist.yaml` (DATA),
  default OFF; keep endpoints/keys backend-only; guidance + privacy note in `knowledge/`.
  **DEFERRED unless a user explicitly opts in** (the agent ships its own `/metrics`).
- **ACCEPTANCE:** telemetry is OFF by default; a user can opt in; endpoints stay backend-only.
- **HERMETIC-TEST:** default emits no telemetry flag; opt-in emits it; scrub test on the endpoint.

## Phase 47 — Cloud results upload internals (GCS/S3 helpers) — DEFERRED
*Catalog ref: Area H — "Cloud results upload internals (cloud_upload.py)" (⬜; pairs with Phase 39).*

- **GOAL:** support the upload-side mechanics (`gcloud storage cp` / `aws s3 cp`) for the
  cloud results sink.
- **BUILD:** widen `security/allowlist.yaml` (DATA) to permit the upload helpers as
  approval-gated mutating actions; reuse the CLI's `cloud_upload.py` rather than reimplement;
  guidance in `knowledge/`.
- **ACCEPTANCE:** when a `gs://`/`s3://` sink is opted in, the upload helper runs
  (approval-gated); local stays default and is a no-op.
- **HERMETIC-TEST:** upload command is approval-gated + allowlisted; local path is a no-op.

## Phase 52 — Multi-turn trace replay benchmark (experimental) — DEFERRED
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

## Phase 57 — flexibility.md placeholder doc — DEFERRED  *(doc-completeness; empty upstream stub)*
*Catalog ref: Area I — "flexibility.md (placeholder doc)" (⬜; an empty upstream stub "To be populated.").*

- **GOAL:** track the empty upstream stub so it can't silently become a dropped doc.
- **BUILD:** none until upstream populates it. **DEFERRED — zero substantive features today.**
- **ACCEPTANCE:** re-run the catalog when upstream populates `docs/flexibility.md`; promote any
  real feature it documents into a new gap row + phase.
- **HERMETIC-TEST:** n/a (no feature to cover); the catalog re-derivation guards against drift.

## Phase 58 — FAQ / RBAC-audit placeholder docs — DEFERRED  *(doc-completeness; empty upstream stubs)*
*Catalog ref: Area I — "FAQ / RBAC-audit placeholder docs" (⬜; empty upstream stubs).*

- **GOAL:** track `docs/faq.md` + `util/rbac_audit_report.md` so they can't become dropped docs.
- **BUILD:** none until upstream populates them. **DEFERRED — empty stubs with no features.**
- **ACCEPTANCE:** re-run the catalog when upstream populates these; promote any real feature
  into a new gap row + phase.
- **HERMETIC-TEST:** n/a; catalog re-derivation guards against drift.

---

## Autonomous execution rules (self-imposed)
- **Branching:** `feature/roadmap-v4` was the integration branch (off `main`). Each phase was
  developed on a `feature/roadmap-v4-pN-<slug>` branch in an isolated worktree and merged in
  after its full-suite gate was green. **`main` was never touched** during the effort.
- **Tests:** pytest only, hermetic. No long/real benchmark runs, no GPU, no live cluster. The
  orchestrator is validated with the fake kube client + the CaptureRunner harness.
- **Allowlist widening is DATA only** (`security/allowlist.yaml`); no per-command Python.
- **Thin code, thick agent:** mechanism in Python, judgment in `knowledge/`.
- **Docs:** update `docs/BENCHMARK_FEATURE_COVERAGE.md` (flip the closed row's emoji) +
  this file's phase STATUS at the end of every phase (when a DEFERRED phase reopens and ships).
