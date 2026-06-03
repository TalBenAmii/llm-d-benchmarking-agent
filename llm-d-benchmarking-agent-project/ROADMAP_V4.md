# ROADMAP v4 — benchmark feature-coverage gaps

> **Living document.** Two sources now feed this roadmap: **(1)** every 🟡/⬜ row in
> [`docs/BENCHMARK_FEATURE_COVERAGE.md`](docs/BENCHMARK_FEATURE_COVERAGE.md) — the benchmark-CLI
> coverage catalog (Phases 27–58); and **(2)** the upstream **deploy-stack** capabilities surfaced by
> [`docs/USEFUL_REPO_DOCS.md`](docs/USEFUL_REPO_DOCS.md) — the 137 `llm-d` docs (well-lit paths,
> readiness/infra preconditions, EPP/Gateway routing, accelerators, provider packs) that the
> benchmark-only catalog never mined (Phases 59–66). Re-derive when **either** source changes.
>
> **Revised 2026-06-03** (see *Changes in this revision* below): re-assessed all phases against the
> current implemented state, added **8 verified new phases (59–66)** that increase deploy-side
> coverage, **reprioritized everything by coverage impact** (see *Execution order* below), and
> deferred the environment-gated / experimental / placeholder phases out of the active line.
>
> **Integration branch.** Worked on `feature/roadmap-v4` (integration branch off `main`;
> **never `main`** during the effort). Numbering **continues contiguously from v3** (which
> ended at Phase 26), so v4 spans **Phases 27-66**. A future `roadmap-v4-autopilot.js` can
> consume this file directly — **work the phases in the Execution-order ranking, not by number** —
> each phase carries a GOAL / BUILD / ACCEPTANCE / HERMETIC-TEST skeleton.

## Status legend
`TODO` · `IN-PROGRESS` · `DONE` · `DEFERRED`

> **Thin code, thick agent** stays the law: mechanism in Python (flags, allowlist data,
> validation), *judgment* in `knowledge/` — never `if/elif` decision logic in Python.
> The two sibling repos stay **read-only**; allowlist widening is **data only**.

---

## Execution order (priority-ranked by coverage impact)

> Work top-down. **P1** = highest coverage impact (de-risks the real-cluster/Kind standup path, or
> unblocks a whole metric/observability chain); **P2** = in-MVP access/authoring gaps; **P3** =
> environment-gated, experimental, or already substituted by an existing agent capability.
> Phases with status `DEFERRED` are tracked in the table below but are off the active line.

| Rank | Phase | Tier | Why (coverage impact) |
|---:|---|:--:|---|
| 1 | **27** — Default-enable benchmark --monitoring + surface results.observability (incl. merged Phase 35 standup PodMonitor/ServiceMonitor + EPP verbosity) | P1 | Headline open gap: no --monitoring in allowlist/build_argv so the producer that fills results.observability never fires; unblocks the entire observability chain (49) and now also carries the standup-side monitoring wiring. |
| 2 | **61** — Right-size the harness launcher CPU request for small/Kind clusters (LLMDBENCH_HARNESS_CPU_NR) | P1 | Default CPU_NR=16 silently FailedSchedules the launcher pod on a single-node Kind cluster; setting it to the probed node's capacity turns a silent Pending into a successful run on the core MVP path. |
| 3 | **59** — Model-load readiness gate: poll /v1/models vs /health with stuck-pod load-timing diagnostics | P1 | Upgrades deploy-and-wait from pod-presence to true serving-readiness, distinguishing 'still loading weights (wait)' from 'wedged' — the single most common opaque standup failure for non-experts. |
| 4 | **62** — Gated-model access pre-flight (check_model_access / GatedStatus) before standup | P1 | Read-only HF-token/gated check up front converts a mid-deploy 'can't pull this gated model' failure into an immediate, exact go/no-go message. |
| 5 | **60** — Infra precondition gate: K8s server version + vLLM/NIXL image minimums + sidecar gotcha | P1 | Honest go/no-go BEFORE a long real-cluster standup (K8s>=1.29, sidecar Init:0/1 gotcha on <=1.28, vLLM 0.10.0+/NIXL 0.5.0+) instead of an opaque failure minutes in. |
| 6 | **49** — Surface results.observability serving metrics end-to-end (narrowed: add the 3 standard metrics to the trend store) | P1 | Surfacing is largely done; only remaining slice is history.py _TREND_METRICS (verified 0/8 are standard) — depends on Phase 27 populating results.observability, so it must rank right after 27. |
| 7 | **45** — Author per-knob vLLM scenario overrides (flags/ports/kvTransfer/affinity/priorityClass/scheduler/storage) | P1 | High-impact authoring gap: directly widens what configurations users can express beyond the coarse spec, with write_and_validate_config already in place to validate them. |
| 8 | **63** — Accelerator + CPU-inferencing precondition advisor (GPU-advertised? DRA vs device-plugin? 64c/64GB-per-replica?) | P1 | Adds the accelerator-availability dimension check_capacity lacks: detect nvidia/amd/gaudi/tpu vs CPU-only and warn that a REAL CPU replica needs 64c+64GB, grounded in upstream truth. |
| 9 | **28** — First-class model override (-m/--models) | P1 | -m/--models is absent from allowlist/build_argv so model choice is only implicit via the spec; modeling it makes 'benchmark this specific model' a first-class, high-frequency MVP action. |
| 10 | **66** — EPP HTTP-header decoder (interpret 429s + x-llm-d-request-dropped-reason) | P2 | Turns opaque 'some requests failed' into 'rejected-saturated — at admission capacity, lower concurrency' by decoding EPP SLO/drop-reason headers a benchmark actually encounters. |
| 11 | **65** — Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool Accepted/ResolvedRefs) | P2 | Extends the readiness gate to the Gateway-API control plane used by GKE/Istio/agentgateway: surfaces the distinct 'pods up but Gateway PROGRAMMED:False, no traffic' not-ready state. |
| 12 | **64** — Provider-aware precondition pack (oc-vs-kubectl, GPU taints/tolerations, GMP/known-issues) | P2 | Adapts commands to the detected provider (oc on OpenShift, nvidia.com/gpu tolerations on DOKS/GKE/AKS, L40S taints) to unstick common Pending/PROGRAMMED-False failures. |
| 13 | **48** — Parse session_performance metrics (multi-turn) | P2 | Confirmed zero session_performance/session_rate references in report.py/analysis.py — a clean, in-MVP parser addition for multi-turn workloads (pairs with the deferred trace-replay Phase 52). |
| 14 | **30** — HuggingFace gated-model secret provisioning | P2 | Pre-flight reads HF config (Phase 6) but no approval-gated mutating step provisions the cluster HF secret, so gated-model standups don't actually work — natural follow-on to the 62 gated pre-flight. |
| 15 | **41** — Dataset replay URL (-x/--dataset) | P2 | -x/--dataset (+RUN_DATASET_DIR) is unmodeled; only synthetic profiles are driven today, so real-dataset replay is a genuine workload-coverage gap. |
| 16 | **29** — Explicit cluster access (-k/--kubeconfig, URL, token) | P2 | -k/--kubeconfig/url/token aren't modeled anywhere, so the agent can only target the ambient kubeconfig; modeling them enables remote-cluster targeting beyond the local Kind. |
| 17 | **46** — Kustomize deploy config block (kustomize.*) | P2 | execute.py only threads -t <method>; no kustomize.* block (guideName/repoPath/patches/overlays/--llmd-repo-path) is authored — an open deploy-path authoring gap. |
| 18 | **53** — convert-guide (guide -> scenario/experiment file generation) | P2 | DOE authoring exists, but generating a full scenario/experiment file FROM a guide URL via LLMDBENCH_* mappings into the session workspace is unimplemented — a real authoring gap. |
| 19 | **36** — First-class skip / collect-only mode (-z/--skip) | P2 | No -z/--skip in allowlist/build_argv; modeling it enables re-collect/re-analyze without re-running load, only reachable via raw passthrough today. |
| 20 | **31** — First-class step selection / re-run (-s/--step) | P2 | No -s/--step flag; re-running a single failed step is only possible via raw extra passthrough — modeling it makes targeted re-runs first-class. |
| 21 | **33** — Multi-stack scenarios + --stack subset + --parallel | P3 | Partially done (--parallel allowlisted, -j emitted); the --stack NAME[,NAME] subset remains unmodeled but is low impact for the single-stack Kind MVP. |
| 22 | **38** — Model the CLI's per-phase timeouts (--wait/--*-timeout) | P3 | Partially done (two timeouts allowlisted, none emitted); full per-phase timeout threading is low impact since runner/orchestrator deadlines already cover most cases. |
| 23 | **42** — Round-trip the CLI's run-config (--generate-config / -c) | P3 | Agent already authors/validates configs via write_and_validate_config + --dry-run/plan, so round-tripping the CLI's own run-config is a low-impact nice-to-have. |
| 24 | **39** — Cloud results sink for the run flag (-r gs://, s3://) | P3 | Opt-in widening of -r to gs:///s3:// (currently restricted to local output_dir); low impact and pairs with the DEFERRED cloud-upload internals Phase 47. |
| 25 | **40** — Trigger the CLI's local --analyze plot families | P3 | Agent already renders per-run harness PNG charts inline and has a rich analyzer; the extra matplotlib plot families are largely redundant — low priority. |
| 26 | **54** — Distributed tracing config (OpenTelemetry tracing: block) | P3 | Authoring a validated scenario tracing: block is legitimate but low impact for the Kind/CPU MVP, which has no external OTel collector. |
| 27 | **37** — Harness debug mode (-d/--debug, sleep infinity) | P3 | Niche developer-facing sleep-infinity debug pod whose interactive in-pod exec stays manual; low value vs the metrics/model/skip gaps. |
| 28 | **32** — Gateway class / provider selection (--gateway-class) | P3 | --gateway-class is absent from allowlist/build_argv; gateway provider is inherited from the spec, low impact on the Kind/CPU MVP. |
| 29 | **55** — Real-time metric streaming / custom Prometheus queries | P3 | Upstream streaming is unimplemented and the agent already substitutes via observe_run_metrics (kubectl top) + live pod-log streaming — nothing to build until upstream ships it. |
| 30 | **56** — Stack discovery tool (llm-d-discover) | P3 | check_endpoint_readiness/probe_environment already cover practical readiness/discovery on Kind, making llm-d-discover modeling a redundant nice-to-have. |
| 31 | **50** — Results Store (git-like result mgmt: remotes/push/pull) | P3 | The agent's own history store (app/storage/history.py + result_history) already covers cross-session result management; the upstream store remains low-impact. |
| 32 | **51** — Jupyter / standalone plotting scripts surfacing | P3 | Per-run harness PNG charts are already rendered inline; the upstream exploratory notebooks add little, so this stays a low-priority surfacing nice-to-have. |

### Deferred / merged (tracked, off the active line)
| Phase | Status | Reason |
|---|---|---|
| 35 — Standup monitoring flag (PodMonitor/ServiceMonitor + EPP verbosity) | DONE — merged → 27 | Merged into Phase 27. The standup-level PodMonitor/ServiceMonitor + EPP-verbosity wiring is now a sub-deliverable of the single --monitoring activation rather than a separately-tracked phase, so it is removed from the active list (no standalone rank). |
| 34 — WVA enablement (`-u/--wva`) | DEFERRED | WVA is OpenShift-only (HPA/VA, 8 WVA smoketests) and explicitly out of the Kind/CPU MVP per its own summary; -u/--wva is correctly absent. Defer until a non-Kind target lands. |
| 43 — Administrative privilege / `--non-admin` skip | DEFERRED | No --non-admin/cluster-admin probing exists; the Kind MVP runs cluster-admin by default, making this a shared-cluster-only concern. Defer until a non-Kind/shared-cluster target lands. |
| 44 — Telemetry push (CLI usage reporting) | DEFERRED | The agent already exposes its own Prometheus /metrics (app/observability/metrics.py, GET /metrics); the CLI's opt-in HTTP telemetry push adds no coverage and is OFF-by-default by design. Defer until a user opts in. |
| 47 — Cloud results upload internals (GCS/S3 helpers) | DEFERRED | No gcloud/aws upload helpers are allowlisted and local default is a no-op; only matters on cloud targets and pairs with the (low-impact) Phase 39. Defer for the Kind MVP. |
| 52 — Multi-turn trace replay benchmark (experimental) | DEFERRED | No trace-file/trace-replay references in app/ or knowledge/; experimental upstream and pairs with Phase 48. Defer until upstream stabilizes it. |
| 57 — `flexibility.md` placeholder doc | DEFERRED | flexibility.md is an empty upstream stub with zero substantive features; nothing to implement until upstream populates it. |
| 58 — FAQ / RBAC-audit placeholder docs | DEFERRED | faq.md + rbac_audit_report.md are empty upstream stubs with no features to build; track only, defer until upstream populates them. |

## Changes in this revision (2026-06-03)

- Phase 35 MERGED into Phase 27: the standup-side PodMonitor/ServiceMonitor + EPP-verbosity wiring is now an explicit sub-deliverable of the single --monitoring activation (its summary already said it 'rides on Phase 27'), so it is no longer tracked separately.
- Phase 27 confirmed still-open and pinned at rank 1 (P1): grep for 'monitoring' in security/allowlist.yaml and app/tools/execute.py returns nothing, so the producer that populates results.observability is never activated — the headline gap in knowledge/benchmark_feature_coverage.md.
- 8 VERIFIED NEW phases (59-66) added; all source docs confirmed present on disk. Veins: (59) llm-d/docs/readiness-probes.md, (60) llm-d/docs/infrastructure.md, (61) llm-d-benchmark/docs/resource_requirements.md, (62) llm-d-benchmark/llmdbenchmark/utilities/README.md, (63) llm-d/docs/accelerators/README.md, (64) llm-d/docs/infra-providers/openshift/README.md, (65) llm-d/guides/prereq/gateways/gke.md, (66) llm-d/docs/api-reference/epp-http-headers.md.
- Four NEW high-impact gates (59 model-load readiness via /v1/models vs /health, 60 infra precondition go/no-go, 61 right-size HARNESS_CPU_NR for Kind, 62 gated-model access pre-flight) placed in P1 — they directly de-risk the real-cluster/Kind standup path that the existing metric-family phases assume already succeeds.
- Dependency chain enforced: Phase 27 (producer, rank 1) ranks ahead of Phase 49 (consumer); Phase 49 is NARROWED to its only remaining slice — add the 3 standard results.observability metrics to history.py _TREND_METRICS (verified 0/8 current trend metrics are standard) — and explicitly depends on 27.
- Pair-phases kept adjacent where both active: 48 (parse session_performance, confirmed zero references in report.py/analysis.py) sits next to its DEFERRED partner 52 in ranking context; 39 (cloud results sink) is noted as pairing with the DEFERRED 47.
- Marked DEFERRED (verdict 'defer', out of Kind/CPU MVP or experimental/placeholder): 34 (WVA, OpenShift-only), 43 (--non-admin, shared-cluster only), 44 (CLI telemetry push, agent already exposes /metrics), 47 (cloud upload helpers, cloud-target only — pairs with 39), 52 (multi-turn trace replay, experimental upstream — pairs with 48), 57 (flexibility.md empty stub), 58 (faq.md/rbac_audit empty stubs). No phase was marked DONE — all re-assessed existing phases that were not deferred remain genuinely open TODOs.
- Reprioritized DOWN into P3: 37 (-d/--debug niche sleep-infinity pod), 38 (per-phase timeouts, partially done — runner deadlines already cover most), 40 (--analyze plot families, redundant with inline PNG charts), plus the low-impact authoring/discovery nice-to-haves 32, 42, 50, 51, 54, 55, 56.
- Tiering rule applied: high-impact deploy-path/observability/metric-family work in P1; medium in-MVP access/authoring gaps in P2; environment-gated, experimental, or redundant-with-existing-agent-capability work in P3.
- COMPLETENESS verified: every existing phase 27-58 appears once — either in priority_order (active) or in existing_updates as DEFERRED (34,43,44,47,52,57,58); Phase 35 accounted for via merge_into 27 (no standalone rank, noted here). Nothing silently dropped.

> **Provenance.** Produced by the `roadmap-v4-refresh` agent workflow (74 agents): mined 53 upstream
> capabilities from `docs/USEFUL_REPO_DOCS.md`, proposed 22 new phases, and kept the **8** that
> survived a 2-lens adversarial check (already-covered? + constraint/source-valid?). The skeptic pass
> hit 11 StructuredOutput tooling failures, so a handful of lower-impact candidates were dropped
> conservatively (precision over recall) and can be recovered by re-running the Verify stage. The
> overturned “done/defer” claims (kept as open after a skeptic showed the gap is still real): 50, 51, 55, 56.


---

## Phase 27 — Default-enable benchmark `--monitoring` + surface `results.observability` — DONE
*Catalog ref: Area F — "Benchmark metrics collection (--monitoring)" (🟡, the headline gap). The activation half of the Phase 25 consumer.*

> **Result (2026-06-03):** Shipped. Added a subcommand-aware `monitoring` flag to `ExecuteInput.flags`
> + `build_argv` (`--monitoring` for standup/run/experiment/plan; `--no-monitoring` only for standup,
> matching upstream argparse), allowlisted those flags per subcommand (DATA-only), and added a read-only
> `prometheus_crds` probe (`_probe_prometheus_crds` detecting PodMonitor/ServiceMonitor CRDs) so the
> on/off + CRD opt-out judgment lives in `knowledge/observability.md` + `knowledge/results_interpretation.md`,
> not Python. Phase 35 (standup PodMonitor/ServiceMonitor + EPP verbosity) folded in as a sub-deliverable.
> Suite **692 passed / 20 skipped** (+26 from the 666 baseline; new `tests/test_monitoring_activate.py`).
> ruff + mypy clean. Unblocks Phase 49 (the `results.observability` trend-metrics consumer).

> **Revision note (2026-06-03):** now also carries the standup-side monitoring wiring — **Phase 35
> (PodMonitor/ServiceMonitor + EPP verbosity) is merged in here** as a sub-deliverable. Ranked **#1
> (P1)**: still fully open (no `--monitoring` in `security/allowlist.yaml` or `app/tools/execute.py`),
> and it unblocks the Phase 49 consumer.

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

## Phase 28 — First-class model override (-m/--models) — DONE
*Catalog ref: Area A — "Model list selection (-m/--models)" (🟡).*

**RESULT (2026-06-03):** Shipped. A top-level `models` field on `ExecuteInput` threads through `execute_llmdbenchmark` into `build_argv` (`app/tools/execute.py`), emitting `-m <id>` only when present — `-m` is the one short form valid across standup/plan/run/experiment (upstream uses `--models` on standup/plan/experiment, `--model` on run). `security/allowlist.yaml` (DATA) gains a value-pinned, metachar-screened `model_id` constraint plus the `-m`/`--models`/`--model` flagspecs under those four subcommands. Model lockstep with the capacity pre-flight (pass the SAME id to `check_capacity` so it sizes + gated-checks the identical model) is knowledge, not Python: new `knowledge/model_override.md` + `knowledge/capacity.md` cross-link; no on-disk model catalog and no value `if/elif`. Hermetic `tests/test_model_override.py` asserts `-m` is emitted per subcommand, the allowlist permits + value-pins it and refuses injection, and the standup id + `check_capacity` override resolve to the IDENTICAL plan_config path. Also de-flaked a pre-existing full-suite-only race in `tests/test_concurrency.py` (target the teardown gate by `tool_call_id` instead of an arbitrary first pending key) — no assertion weakened. Suite after merge into `feature/roadmap-v4`: **776 passed / 20 skipped / 0 failed** (5 consecutive clean full runs); ruff + mypy clean.

- **GOAL:** let the agent select a model per standup rather than only via the chosen spec.
- **BUILD:** add a `models` field to `ExecuteInput` → emit `-m/--models` in `build_argv`
  (`app/tools/execute.py`); widen `security/allowlist.yaml` (DATA) for `-m`; keep model intent
  consistent with the capacity pre-flight (`CheckCapacityInput`); guidance in `knowledge/`.
- **ACCEPTANCE:** a standup can run with an explicit `-m` model not pinned by the spec; the
  capacity pre-flight sees the same model; catalog grounding still validates the name.
- **HERMETIC-TEST:** `build_argv` emits `-m`; allowlist permits it; capacity input mirrors the
  override.

## Phase 29 — Explicit cluster access (-k/--kubeconfig, URL, token) — DONE
*Catalog ref: Area A — "Cluster access / authentication" (🟡).*

**RESULT (2026-06-04):** Shipped. A top-level `kubeconfig` field on `ExecuteInput` (`app/tools/schemas.py`)
emits `-k <path>` after every subcommand via `build_argv` (`app/tools/execute.py`) to target a NON-DEFAULT
kubeconfig FILE — a plain, non-secret, allowlist-pinned path (no `..` traversal; `security/allowlist.yaml`
widened DATA-only). The remote-by-URL+TOKEN route stays BACKEND-ONLY: `flags.cluster_url`/`flags.cluster_token`
ride the same scrubbed `child_env` overlay as `LLMDBENCH_HARNESS_CPU_NR` (forwarded as
`LLMDBENCH_CLUSTER_URL`/`LLMDBENCH_CLUSTER_TOKEN`), so the SECRET token never crosses argv/allowlist, a `command`
event, or a log (mirrors the HF_TOKEN non-leak). Judgment (WHEN/WHICH cluster) lives in
`knowledge/preconditions.md`. 30 new hermetic tests (`tests/test_cluster_access.py`), no live cluster/network.
Suite after merge into `feature/roadmap-v4`: **968 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** target a remote cluster instead of relying only on the ambient kube context.
- **BUILD:** model `kubeconfig` / `cluster.url` / `cluster.token` on `ExecuteInput`; emit
  `-k`/URL where the CLI accepts them; keep tokens backend-only + scrubbed (`app/config.py:child_env`);
  widen `security/allowlist.yaml` (DATA); judgment in `knowledge/preconditions.md`.
- **ACCEPTANCE:** a non-default kubeconfig/URL is threaded into the CLI call; the token never
  reaches the browser or logs.
- **HERMETIC-TEST:** `build_argv` emits `-k`/URL; secret-scrub test asserts the token is absent
  from emitted env + command events.

## Phase 30 — HuggingFace gated-model secret provisioning — DONE
*Catalog ref: Area A — "HuggingFace token / gated-model auth" (🟡).*

**RESULT (2026-06-03):** Shipped. New approval-gated mutating tool `provision_hf_secret` (`app/tools/hf_secret.py`,
registered in `app/tools/registry.py`, `ProvisionHfSecretInput` in `app/tools/schemas.py`) materializes the cluster
HF-token Secret (`llm-d-hf-token`) a gated-model standup needs — the follow-on to the Phase 62 gated-access pre-flight.
The token stays BACKEND-ONLY: a vetted `scripts/provision_hf_secret.py` (allowlisted `project-script`, committed 0755)
reads `HF_TOKEN` from the already-scrubbed child env and runs the upstream `kubectl create secret … --dry-run=client -o
yaml | kubectl apply -f -` shape over its OWN `shell=False` subprocess, so the token never crosses the allowlist/argv
or reaches a command event/log (a raw `kubectl create secret` is deliberately NOT allowlisted). Judgment (not-for-public
/ lacks-access-needs-request) lives in `knowledge/capacity.md`. 22 hermetic tests (`tests/test_hf_secret.py`), incl. two
real-runner-exec tests; no live cluster/network/GPU. Suite after merge into `feature/roadmap-v4`: **857 passed / 20
skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** make gated-model standups work, not just the capacity lookup.
- **BUILD:** surface `huggingface.enabled` + provision the cluster HF secret as an
  approval-gated mutating step (via allowlisted `kubectl`); keep the token backend-only;
  judgment ("when is a gated model in scope") in `knowledge/`.
- **ACCEPTANCE:** a gated-model plan provisions the HF secret before standup; the token is
  never exposed; non-gated flows are unchanged.
- **HERMETIC-TEST:** the secret-provision command is approval-gated + allowlisted; scrub test
  asserts the token never appears in events.

## Phase 31 — First-class step selection / re-run (-s/--step) — DONE
*Catalog ref: Area A — "Standup/run step list and re-run individual steps (-s/--step)" (🟡, currently only via extra passthrough).*

- **GOAL:** promote step selection from the raw `extra` passthrough to a modeled, advisory flag.
- **BUILD:** add a `step` field to `ExecuteInput.flags` → emit `-s/--step` (incl. ranges like
  `3-5`, `5,7`) in `build_argv`; widen `security/allowlist.yaml` (DATA); add step-list guidance
  to `knowledge/` so the agent can re-run a single failed step.
- **ACCEPTANCE:** the agent can re-run a step range as a modeled flag (not via `extra`).
- **HERMETIC-TEST:** `build_argv` emits `-s` with a range; allowlist permits it.
- **RESULT (DONE):** shipped a modeled `flags["step"]` (step-list grammar `N / N-M / comma`)
  emitting `-s <spec>` in `build_argv` for standup/smoketest/run/teardown; value-pinned by a new
  `step_list` allowlist regex (`^[0-9]+([,-][0-9]+)*$`) on all four subcommands; added
  `knowledge/step_select.md` (per-phase step numbering + when to re-run). `-s` does NOT change a
  command's mode, so re-running a mutating step stays approval-gated. Merged into feature/roadmap-v4
  with the Phase 29 cluster-access flag-list union preserved; full suite **1031 passed / 20 skipped**
  (+63 new tests), ruff + mypy clean.

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

## Phase 35 — Standup monitoring flag (PodMonitor/ServiceMonitor + EPP verbosity) — DONE *(merged into Phase 27)*
*Catalog ref: Area A/F — "Standup monitoring flag" (🟡). Rides on Phase 27's activation.*

> **Revision note (2026-06-03):** **merged into Phase 27** — the standup-level
> PodMonitor/ServiceMonitor + EPP-verbosity wiring is now an explicit sub-deliverable of the single
> `--monitoring` activation, not a separately-tracked phase. The detail below is retained for the
> implementer.

- **GOAL:** ensure the standup-level `--monitoring` (PodMonitor/ServiceMonitor creation + EPP
  verbosity) is explicitly modeled and surfaced, including the `--no-monitoring` escape.
- **BUILD:** confirm the Phase 27 `monitoring` flag is wired into `standup` specifically;
  expose `monitoring.podmonitor.enabled` / `monitoring.installPrometheusCrds` guidance in
  `knowledge/observability.md`.
- **ACCEPTANCE:** a standup creates PodMonitor/ServiceMonitor when monitoring is on, and skips
  cleanly with `--no-monitoring` on CRD-less clusters.
- **HERMETIC-TEST:** standup `build_argv` emits the monitoring flag; CRD-less opt-out selected
  from a probed environment.

## Phase 36 — First-class skip / collect-only mode (-z/--skip) — DONE
*Catalog ref: Area A/C — "Skip experiment execution / collect existing results (-z/--skip)" (🟡, via extra passthrough only).*

- **GOAL:** promote collect/analyze-only from `extra` to a modeled flag.
- **BUILD:** add a `skip` field to `ExecuteInput.flags` → emit `-z/--skip` in `build_argv`;
  widen `security/allowlist.yaml` (DATA); document the collect-only flow in `knowledge/`.
- **ACCEPTANCE:** the agent can re-collect/analyze existing results without re-running the load.
- **HERMETIC-TEST:** `build_argv` emits `-z`; allowlist permits it.
- **RESULT:** shipped the modeled `flags.skip` key — `build_argv` emits a bare `-z` on `run`,
  `-z`/`--skip` are allowlisted as `read_only_trigger` (collect-only, so it auto-runs) on `run`
  alone, and `knowledge/collect_only.md` documents WHEN to re-collect without re-running load.
  New hermetic suite `tests/test_collect_only.py` (+11 tests). Full suite **1042 passed / 20
  skipped / 0 failed**; ruff + mypy clean.

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

## Phase 41 — Dataset replay URL (-x/--dataset) — DONE
*Catalog ref: Area A — "Dataset replay URL (-x/--dataset)" (⬜).*

- **GOAL:** support replaying a real dataset instead of only synthetic workload profiles.
- **BUILD:** model `-x/--dataset` (+ `REPLACE_ENV_LLMDBENCH_RUN_DATASET_DIR`) → emit in
  `build_argv`; widen `security/allowlist.yaml` (DATA); add dataset-vs-synthetic guidance to
  `knowledge/`.
- **ACCEPTANCE:** the agent can run a dataset-replay workload; synthetic profiles still work.
- **HERMETIC-TEST:** `build_argv` emits `-x`; allowlist permits it.
- **RESULT:** shipped the modeled `flags.dataset` key — `build_argv` emits `-x <url>` ONLY on
  `run`/`experiment` (the two subcommands upstream accepts it on), `-x`/`--dataset` are
  allowlisted with a `dataset_url` value constraint (http(s)/hf/gs/s3 scheme or bare path) on
  both `run` and `experiment`, and `knowledge/dataset_replay.md` documents WHEN to replay vs stay
  synthetic. No env var set — the CLI derives `LLMDBENCH_RUN_DATASET_DIR/_FILE` from the URL.
  New hermetic suite `tests/test_dataset_replay.py` (+21 tests). Full suite **1063 passed / 20
  skipped / 0 failed**; ruff + mypy clean.

## Phase 42 — Round-trip the CLI's run-config (--generate-config / -c) — TODO
*Catalog ref: Area A — "Generate / reuse a run config YAML (--generate-config / -c)" (🟡).*

- **GOAL:** use the CLI's own `--generate-config` / `-c` reuse mechanism (in addition to the
  agent's in-workspace `write_and_validate_config`).
- **BUILD:** model `--generate-config` + `-c/--config` → emit in `build_argv`; widen
  `security/allowlist.yaml` (DATA); store/reuse the generated config under `--workspace`;
  guidance in `knowledge/`.
- **ACCEPTANCE:** the agent can generate a run-config with the CLI and replay it via `-c`.
- **HERMETIC-TEST:** `build_argv` emits `--generate-config` then `-c`; allowlist permits both.

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

## Phase 45 — Author per-knob vLLM scenario overrides — DONE
*Catalog ref: Area B — "vLLM tuning knobs (command gen, flags, ports, KV-transfer, accelerator, affinity, storage, scheduling)" (🟡; today only via DoE sweeps / capacity knobs).*

> **RESULT (Phase 45):** Extended `app/tools/config_artifact.py` to author per-knob vLLM
> scenario overrides via DOTTED upstream field paths (`vllmCommon.flags.*`, `vllmCommon.kvTransfer.*`,
> `vllmCommon.kvEvents.*`, `vllmCommon.priorityClassName/ephemeralStorage/networkResource`,
> `affinity.*`, `schedulerName`) into the session workspace (repos stay read-only), validated via
> the CLI plan/`--dry-run` determinism gate. Added `knowledge/vllm_overrides.md`, allowlist
> `model_id`/spec-file rules, and registry/schema wiring. New hermetic suite
> `tests/test_scenario_overrides.py` (26 tests). Suite: 802 passed, 20 skipped, 0 failed
> (ruff + mypy clean).

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

## Phase 48 — Parse session_performance metrics (multi-turn) — DONE
*Catalog ref: Area E — "session_performance metrics (multi-turn sessions)" (⬜).*

> **Result (2026-06-03):** `app/validation/report.py` now mechanically extracts
> `results.session_performance.sessions` via a catalog-driven `extract_session_performance`
> (field discovery as DATA in `knowledge/standard_metrics.yaml`), surfaces it on the report
> summary and per-run in `analyze_results` (`app/tools/analyze.py`); single-turn reports yield
> `None` with no fabrication. `knowledge/results_interpretation.md` gained a multi-turn section.
> New hermetic suite `tests/test_session_performance.py` (multi-turn surfacing, single-turn None,
> catalog discovery, non-fatal validation deviation, analyze end-to-end). Suite: 879 passed,
> 20 skipped, 0 failed; ruff + mypy clean.

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

## Phase 49 — Surface results.observability serving metrics end-to-end — DONE
*Catalog ref: Area E/F — "Standard resource/serving metrics from results.observability" (🟡). Consumer ships; depends on Phase 27 producer activation.*

> **Result (2026-06-03):** Added the 3 §3.4 standard serving metrics — KV-cache hit rate,
> GPU utilization, and schedule-delay (queue-depth proxy) — to the trend store
> (`app/storage/history.py` `_TREND_METRICS`) at their nested `standard_metrics.<key>.value`
> stat path, labelled informationally (never affect Pareto dominance) and absent on non-monitoring
> runs. New tests in `tests/test_history.py`; knowledge docs updated. Suite: 806 passed, 20 skipped,
> 0 failed; ruff + mypy clean.

> **Revision note (2026-06-03):** **narrowed** — surfacing is largely done; the only remaining slice
> is adding the 3 standard `results.observability` metrics to the trend store (`app/storage/history.py`
> `_TREND_METRICS` — verified 0/8 current trend metrics are standard). Ranked **#6 (P1)**, directly
> after its Phase 27 producer.

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

## New phases from `docs/USEFUL_REPO_DOCS.md` (deploy-stack coverage) — added 2026-06-03

> These 8 phases close coverage on the upstream **deploy/runtime** side (readiness, infra
> preconditions, accelerators, gateway/EPP routing) that the benchmark-CLI catalog never reached.
> Each cites its source doc and obeys the same thin-code/thick-agent + read-only-repos rules.

## Phase 59 — Model-load readiness gate: poll `/v1/models` vs `/health` with stuck-pod load-timing diagnostics — DONE
*Source: docs/readiness-probes.md (via docs/USEFUL_REPO_DOCS.md). Closes the deploy-and-wait blind spot: today the Phase 24 gate only confirms pod/endpoint *presence*, so a Running-but-NotReady model server still loading weights is indistinguishable from a wedged one — this adds true serving-readiness classification before a benchmark is launched.*

> **Result (2026-06-03):** Shipped the serving-readiness classifier in `app/orchestrator/readiness.py` + `app/tools/readiness.py`: parses `kubectl get pods` JSON (readiness conditions / `restartCount` / age by 8000/8200 role-port) and a tightly-constrained GET-only `curl` probe enum'd to `{/v1/models, /health}` on in-namespace `*.svc` URLs (added to `security/allowlist.yaml`); the loading-vs-broken JUDGMENT lives in the new `knowledge/readiness_probes.md`, not Python. `/health` 200 + `/v1/models` 503 ⇒ "still loading weights (keep waiting)"; refused/crash-looping ⇒ "wedged/broken". Full suite green: **723 passed, 20 skipped, 0 failed** (ruff + mypy clean).

- **GOAL:** stop benchmarking against a server that isn't actually serving yet. Extend the endpoint-readiness gate from "pod exists / endpoint present" to TRUE model-serving readiness, and tell the user *why* a `Running` pod is still `NotReady`: classify it as **"still loading weights (legitimate — keep waiting)"** vs **"wedged/broken (stop waiting)"** by distinguishing `GET /v1/models` (serving-ready, the startup/readiness probe) from `GET /health` (process-alive, the liveness probe) and reading pod readiness conditions by role-port (8000 prefill / 8200 decode).
- **BUILD:** mechanism in Python — extend the readiness analyzer (`app/tools/` endpoint-readiness path, the Phase 24 `EndpointReadiness` struct in `app/validation/`) to parse the already-allowlisted `kubectl get pods/endpoints -o json` for pod readiness conditions / `restartCount` / age, and add a tightly-constrained `curl` entry to `security/allowlist.yaml` as **read-only DATA** — `value_constraints` restrict it to `GET` against an in-namespace service URL on the model-server ports (8000/8200) and to the path **enum** `{/v1/models, /health}` only (no other host/port/path/verb). Fold both signals into a new field on the `EndpointReadiness` verdict struct (e.g. `serving_readiness`). All **JUDGMENT** — what "stuck loading" vs "broken" means, how long `failureThreshold * periodSeconds` legitimately permits (the doc's `failureThreshold: 60` startup budget), and that **`/health` passes but `/v1/models` 503s ⇒ weights still loading** — lives in a new `knowledge/readiness_probes.md`. No `if/elif` decision logic in Python; the repos stay read-only and any captured probe/diagnostic output is written into the session workspace only.
- **ACCEPTANCE:** when a model-server pod is `Running` but `NotReady`, the agent reports the loading-vs-broken verdict (and the recommended wait/stop action) before any benchmark is submitted; the verdict is driven by `knowledge/readiness_probes.md`, not Python branches; the `curl` probe is permitted *only* for `GET` on ports 8000/8200 at `/v1/models` or `/health` against an in-namespace svc URL, and is rejected for any other verb/port/path/host.
- **HERMETIC-TEST:** feed canned `kubectl get pods` JSON fixtures (Running+NotReady with low `restartCount` & young age vs high `restartCount`/crash-looping) plus canned `curl` bodies (`200` with a model list, `503`, connection-refused) into the analyzer and assert the classification: `/health` 200 + `/v1/models` 503 ⇒ "still loading weights"; `/health` refused or high restartCount ⇒ "wedged/broken"; both 200 ⇒ serving-ready. Assert the allowlist validator permits `curl -X GET <svc>:8000/v1/models` and `:8200/health` but rejects a `POST`, an off-enum path (e.g. `/v1/completions`), and a non-svc host. No GPU, no live cluster, no real benchmark.

## Phase 60 — Infra precondition gate: K8s server version + vLLM/NIXL image minimums + sidecar gotcha — DONE
*Source: docs/infrastructure.md (via docs/USEFUL_REPO_DOCS.md). Closes the coverage gap between the agent's pod/endpoint readiness probing and the doc's HARD standup preconditions (K8s server version + vLLM/NIXL/UCX image minimums), turning an opaque Init:0/1 stall minutes into a long real-cluster standup into an honest up-front go/no-go.*

> **RESULT (merged into feature/roadmap-v4):** Shipped a `cluster_preconditions` read-only probe in `probe_environment` (`app/tools/probe.py`): a read-only `kubectl version --output json` parsed into `cluster_info.server_version` `{major, minor}` plus the spec's pinned vLLM/NIXL/UCX/NVSHMEM `{repository, tag}` image tags parsed off the rendered scenario YAML — FACTS only, no version-comparison `if/elif` in Python. The thresholds (K8s ≥1.29, 1.33+ for sidecars, the ≤1.28 Init:0/1 gotcha, vLLM 0.10.0+ / NIXL 0.5.0+ / UCX 0.19.0+ / NVSHMEM 3.3.9+) and verdict bands live as DATA in new `knowledge/infrastructure_preconditions.yaml` (+ prose in `knowledge/preconditions.md`); the LLM reasons over the table. `schemas.py` field documents the new probe (additively, alongside the Phase 28/45 model-override + vLLM-knob entries — both preserved). Hermetic `tests/test_infra_preconditions.py` (10 tests) feeds canned 1.27/1.29/1.33 `kubectl version` output + canned image tags through a fake runner and asserts the extracted facts and the knowledge thresholds; no live cluster. Full suite **818 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** give an honest go/no-go BEFORE a long real-cluster standup. On K8s 1.27 the user hears "the sidecar-based P/D guide will get stuck in Init:0/1 — upgrade to 1.33+ or pick a non-sidecar path" (and that vLLM <0.10.0 / NIXL <0.5.0 / UCX <0.19.0 image tags are below the tested minimums) instead of a baffling failure after the standup has already burned real time.
- **BUILD:** mechanism in Python (facts only) — extend `probe.probe_environment` (`app/tools/probe.py`) with a read-only `kubectl version -o json` (already allowlisted as `kubectl version { mode: read_only }` in `security/allowlist.yaml`; widen the `version` subcommand's `--output` value-ref DATA only if `json` isn't already permitted) parsed into `cluster_info.server_version` `{major, minor}`, plus parse the vLLM/NIXL/UCX/NVSHMEM image tags out of the rendered spec; surface both as plain facts on the probe schema field (`app/tools/schemas.py`). NO version-comparison `if/elif` in Python — the thresholds (K8s ≥1.29, 1.33+ recommended for sidecars, the Init:0/1 gotcha on ≤1.28, vLLM 0.10.0+ / NIXL 0.5.0+ / UCX 0.19.0+ / NVSHMEM 3.3.9+) and the which-combo-can-run + when-to-warn rules live as DATA in a new `knowledge/infrastructure_preconditions.yaml` (with prose tie-in from `knowledge/preconditions.md`); the LLM reasons over that table. Repos stay read-only — nothing is written into `llm-d/`; any captured probe artifact is authored into the session workspace only.
- **ACCEPTANCE:** before a real-cluster standup the agent reports the probed K8s server `major.minor` and the spec's image tags, and (reasoning from `knowledge/infrastructure_preconditions.yaml`, not Python) issues the right verdict: on 1.27 → "sidecar P/D won't init, upgrade to 1.33+ or pick a non-sidecar path"; on 1.29 → "runs, but 1.33+ recommended for full sidecar support"; on 1.33 → green; below-minimum vLLM/NIXL/UCX tags are flagged. The decision logic lives entirely in `knowledge/`, not in `app/`.
- **HERMETIC-TEST:** feed canned `kubectl version -o json` output for 1.27 / 1.29 / 1.33 plus canned rendered image tags through `probe_environment` (fake runner, no live cluster) and assert the extracted facts (`cluster_info.server_version` major.minor + the parsed vLLM/NIXL/UCX tags) match; assert `knowledge/infrastructure_preconditions.yaml` lists the thresholds (K8s 1.29 / 1.33 / 1.28-sidecar-gotcha, vLLM 0.10.0, NIXL 0.5.0, UCX 0.19.0); assert the allowlist still permits `kubectl version -o json` read-only. No GPU, no live cluster, no real benchmark run.

## Phase 61 — Right-size the harness launcher CPU request for small/Kind clusters (`LLMDBENCH_HARNESS_CPU_NR`) — DONE
*Source: docs/resource_requirements.md (via docs/USEFUL_REPO_DOCS.md). Closes the silent-`FailedScheduling` gap on the MVP Kind path — turns an opaque `Pending` launcher pod into a scheduled, successful run.*

> **RESULT (merged into feature/roadmap-v4):** Shipped a `node_capacity` read-only probe (per-node allocatable/capacity CPU + min-allocatable across nodes) in `probe_environment`, a backend-only `harness_cpu_nr` flag plumbed as the `LLMDBENCH_HARNESS_CPU_NR` env var through `execute.py` → `context.run_command(env=)` → `runner` (merged last, never reaches the browser, no allowlist change), and `knowledge/harness_sizing.md` holding the lower-it-or-not / to-what (inference-perf vs vllm-benchmark) judgment. Merge reconciled the two added probes against Phase 27/59 (probe count 7→8: prometheus_crds + node_capacity). Suite **735 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** stop the benchmark launcher pod from sitting in opaque `FailedScheduling`/`Pending` on a single-node Kind cluster: when the probed node can't satisfy the harness default (`LLMDBENCH_HARNESS_CPU_NR=16`), the agent lowers the launcher's CPU request to what the node can actually schedule, so the MVP Kind run proceeds instead of silently hanging.
- **BUILD:** plumb a **backend-only** env var `LLMDBENCH_HARNESS_CPU_NR` through `app/config.py:child_env` into the `llmdbenchmark` subprocess (never surfaced to the browser, never an allowlist flag — it's an env var, not a flag/executable, so **no `security/allowlist.yaml` change**); extend `probe_environment` (`app/tools/`) to report the node's allocatable CPU. The *judgment* — whether to lower it, and to what value given probed node CPU and the chosen harness (inference-perf's multi-process launcher needs more headroom than vllm-benchmark's single-process one) — lives in a new `knowledge/harness_sizing.md`, **never** as `if/elif` in Python. Repos stay read-only; any sizing artifact is authored into the session workspace only.
- **ACCEPTANCE:** on a small-node fixture the launcher subprocess `child_env` carries a lowered `LLMDBENCH_HARNESS_CPU_NR` (sourced from `knowledge/harness_sizing.md`, not Python branches) and the run schedules instead of going `Pending`; on a large-node fixture the var is absent/default (16); the value never reaches the browser; the harness-aware (inference-perf vs vllm-benchmark) distinction is documented in `knowledge/`.
- **HERMETIC-TEST:** with a fixture probe reporting small node CPU, assert the emitted `child_env` carries the chosen `LLMDBENCH_HARNESS_CPU_NR`; with a large-node fixture, assert it's absent (default 16); assert it never appears in the browser-facing scrubbed env. No GPU / live cluster / real benchmark run.

## Phase 62 — Gated-model access pre-flight (check_model_access / GatedStatus before standup) — DONE
*Source: llmdbenchmark/utilities/README.md (via docs/USEFUL_REPO_DOCS.md). Closes the read-only-pre-flight gap for HF gated-model auth: today the capacity bridge sizes memory but never asks "can this token even pull this model?", so a gated-but-unauthorized model only fails mid-standup.*

**RESULT (2026-06-03):** Shipped. The already-allowlisted read-only `scripts/capacity_check.py` bridge now also calls the benchmark repo's OWN `llmdbenchmark.utilities.huggingface.check_model_access` / `GatedStatus` (never reimplemented) and returns a token-free `{gated, authorized, reason}` block; `CapacityVerdict` gained `gated`/`authorized`/`gated_reason` fields (defaulted → legacy paths unchanged) wired via a pure-field-copy `merge_gated_access` in `app/capacity/planner.py`. Per-status judgment (public/authorized → proceed; gated+unauthorized → provision the secret via Phase 30) lives in `knowledge/capacity.md`, not Python. `HF_TOKEN` read from the scrubbed child env only, never echoed. No allowlist change. Hermetic tests (`tests/test_capacity_gated.py`) drive a fixture `ModelAccessResult` per `GatedStatus` and assert the verdict + that the token never leaks. Suite after merge into `feature/roadmap-v4`: **756 passed / 20 skipped**; ruff + mypy clean.

- **GOAL:** before a long standup, tell a non-expert the exact gated-model verdict up front — *public* (no token needed), *gated + authorized* (your token can pull it, proceed), or *gated + unauthorized* (your token can't pull it, here's the fix) — instead of letting an opaque image-pull/weights failure surface minutes into the deploy. Pairs the existing capacity "will it fit?" pre-flight with a "can you even get the weights?" pre-flight.
- **BUILD:** extend the already-allowlisted read-only capacity bridge `scripts/capacity_check.py` (driven by `app/capacity/planner.py`, run with the benchmark repo's venv Python) to *also* call the repo's own `llmdbenchmark.utilities.huggingface.check_model_access()` / `GatedStatus` and return a structured `{gated, authorized, reason}` alongside the capacity verdict — never reimplementing the gating check. Add the `gated`/`authorized`/`reason` fields to the capacity result model (`app/capacity/` schema / `app/tools/schemas.py`'s `CheckCapacityInput`/output) so the agent sees them. The `HF_TOKEN` is read from backend env only, passed to the bridge through the already-scrubbed child env (`app/config.py:child_env`) and **never echoed** into the structured result, events, or logs. **No allowlist change** — this reuses the already-allowlisted read-only `capacity_check.py` project-script (one `.json`-path argument), so `security/allowlist.yaml` is untouched. JUDGMENT — what to say for each `GatedStatus`, and whether to offer Phase 30 secret-provisioning next when the verdict is gated+unauthorized — lives in `knowledge/capacity.md` (which already notes `HF_TOKEN` for gated lookups), never in Python `if/elif`. Repos stay read-only; any artifact (the JSON request) is authored into the session workspace only.
- **ACCEPTANCE:** running `check_capacity` on a gated model surfaces `{gated, authorized, reason}` at the plan gate before any mutating step; a gated+unauthorized verdict prompts the knowledge-driven "your HF token can't pull this model" explanation (and the option to provision the secret via Phase 30), gated+authorized says "proceed", and public needs no token — all three decisions are read from `knowledge/capacity.md`, not Python branches; the token never appears in the result or events.
- **HERMETIC-TEST:** drive the bridge / planner with a fixture `ModelAccessResult` for each `GatedStatus` (public/NOT_GATED, gated+authorized, gated+denied) and assert the structured `{gated, authorized, reason}` verdict is produced; assert the secret-scrub test finds no `HF_TOKEN` value in the structured result or emitted command events; no live HuggingFace call, no GPU, no live cluster, no real standup.

## Phase 63 — Accelerator + CPU-inferencing precondition advisor (GPU-advertised? DRA vs device-plugin? 64c/64GB-per-replica?) — DONE
*Source: docs/accelerators/README.md (via docs/USEFUL_REPO_DOCS.md). Adds the accelerator-availability dimension the planner doesn't cover: pairs node-advertised-resource detection with `check_capacity`'s GPU-memory sizing so the agent can answer "can my hardware run this?" grounded in upstream truth.*

> **RESULT (merged into feature/roadmap-v4):** Shipped a read-only `advise_accelerators` probe (`app/tools/probe.py`, registered in `app/tools/registry.py` with `AdviseAcceleratorsInput` in `app/tools/schemas.py`) that runs the already-allowlisted `kubectl get nodes -o json` and mechanically extracts per-node `capacity`/`allocatable` cpu + memory (verbatim K8s quantity, never lossily converted) and the advertised accelerator extended-resource keys (`nvidia.com/gpu` + amd/gaudi/tpu/Intel-XPU siblings), returning FACTS only (`any_accelerator`, `cpu_only`, `advertised_resources`, per-node `accelerators`). No allowlist change. All feasibility judgment — CUDA/driver minimums, Device-Plugin vs DRA, the real-CPU 64c/64GB-per-replica floor, and the Kind/CPU-sim exemption — lives as DATA in new `knowledge/accelerators.yaml` (with pointers from `knowledge/preconditions.md` + `knowledge/capacity.md`); no `if/elif` feasibility branch in Python. Hermetic `tests/test_accel_advisor.py` feeds canned GPU-advertised + CPU-only `kubectl get nodes` fixtures through a fake runner and asserts the extracted facts and the knowledge floors/minimums; no GPU, no live cluster. Merge reconciled the new probe/helpers additively against Phase 60's `cluster_preconditions` probe (both probes + both helper sets preserved). Full suite **835 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** let the agent answer "can my hardware actually run this?" before a standup — detect whether a node advertises `nvidia.com/gpu` (or amd/gaudi/tpu/xpu siblings) versus CPU-only, and warn that a REAL (non-sim) CPU-only replica needs **64 cores + 64GB RAM**, complementing the GPU-memory sizing `check_capacity` already does.
- **BUILD:** add a read-only `advise_accelerators` probe (`app/tools/probe.py`, registered in `app/tools/registry.py`) that runs the **already-allowlisted** `kubectl get nodes -o json` and mechanically extracts per-node `status.capacity`/`status.allocatable` (cpu, memory, and the `nvidia.com/gpu` + sibling extended-resource keys); add its `AdviseAcceleratorsInput`/output to `app/tools/schemas.py`. No new mutating command and no allowlist widening (reuses the existing `kubectl get nodes` entry — confirm it's present, else add it as read-only DATA in `security/allowlist.yaml`). All **judgment** — the CUDA 12.9.1 / driver 575.x (min 525.60.13, < 580) and CUDA 13.0.2 / driver 580.65.06 minimums, Device-Plugin vs DRA selection, the CPU-only 64c/64GB-per-replica floor, and confirmation that the Kind/CPU **sim** path is supported and exempt from the floor — lives as pure DATA in a new `knowledge/accelerators.yaml` that the LLM reasons over (plus a pointer from `knowledge/preconditions.md` + `knowledge/capacity.md`). **No `if/elif` feasibility logic in Python.** Repos stay read-only; any generated artifact is authored into the session workspace only.
- **ACCEPTANCE:** on a GPU-advertised node the agent reports the advertised accelerator resource + its DRA-vs-device-plugin/CUDA-driver advice; on a CPU-only node it warns that a real (non-sim) replica needs 64c/64GB and pairs the warning with `check_capacity`'s GPU-memory sizing; on Kind/CPU sim it confirms the path is supported (floor exempt). Every feasibility judgment is sourced from `knowledge/accelerators.yaml`, not Python branches.
- **HERMETIC-TEST:** feed canned `kubectl get nodes -o json` fixtures (one node advertising `nvidia.com/gpu`, one CPU-only) through the probe with the fake runner and assert the extracted advertised-resource facts (cpu/memory/gpu keys) per node; assert `knowledge/accelerators.yaml` loads and carries the 64c/64GB-per-replica CPU floor, the CUDA/driver minimums, and the DRA-vs-device-plugin distinction. No GPU, no live cluster, no real benchmark.

## Phase 64 — Provider-aware precondition pack (oc-vs-kubectl, GPU taints/tolerations, GMP/known-issues) — DONE
*Source: docs/infra-providers/openshift/README.md (via docs/USEFUL_REPO_DOCS.md). Closes the cross-provider precondition gap so the agent's commands and unstick-advice actually fit OpenShift / DOKS / GKE / AKS, not just kind.*

**RESULT:** Added a read-only `provider_detection` capability to the `probe_environment` tool (`app/tools/probe.py`, registered in `app/tools/registry.py`, enum widened in `app/tools/schemas.py`). It reuses the already-allowlisted `kubectl get nodes -o json` to emit FACTS only — `provider` (openshift/gke/doks/aks vs `kind` default), `providers_seen`, per-node `gpu_taints` `{node,key,value,effect}`, and per-node label/taint facts — via a plain label-prefix membership lookup (mechanism table kept in lockstep with the knowledge file by test), with NO provider `if/elif`. `security/allowlist.yaml` (DATA only) gains an `oc:` tool entry carrying the SAME constrained read-only subcommands as `kubectl` (shared refs, no Python oc-vs-kubectl branching). The per-provider playbook — which CLI, which taint/toleration to author, which known issue (GMP / "Undetected platform" / NVSHMEM) applies — is pure JUDGMENT in new `knowledge/infra_providers.yaml` (cross-linked from `knowledge/preconditions.md`). New hermetic tests `tests/test_provider_pack.py` + `tests/test_allowlist.py` (oc validates against the same read-only constraints as kubectl; mutating/unknown subcommands denied). Merged into `feature/roadmap-v4`. Suite **903 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** adapt to the user's detected cloud provider so its commands work and it can unstick the common Pending / `PROGRAMMED=False` failures: on **OpenShift** use `oc` (not `kubectl`), avoid ServiceMesh/Istio gateway conflicts, and apply the L40S taint tolerations that unstick Pending model-server pods; on **DOKS/GKE/AKS** apply the `nvidia.com/gpu` toleration and flag the GKE Google-Managed-Prometheus, "Undetected platform", and NVSHMEM known issues — all as advice the user can approve, never silent mutation.
- **BUILD:** add a read-only **provider-detection probe** to `app/tools/probe.py` (reuse the already-allowlisted `kubectl get nodes -o json` to read node labels/taints + cluster facts; emit `provider`/`gpu_taints` detection *facts*, no decision branches). Widen `security/allowlist.yaml` (**DATA only**) to add an `oc:` tool entry carrying the **same** constrained read-only `subcommands:` set as `kubectl` (a kubectl-equivalent entry referencing the shared `kubectl_resource`/`output_format`/`namespace`/`label_selector` refs — DATA, no Python `oc`-vs-`kubectl` branching). The per-provider playbook — which CLI, which taint/toleration to author, which known issue (GMP / "Undetected platform" / NVSHMEM) applies — is pure JUDGMENT in a new `knowledge/infra_providers.yaml` the LLM reasons over (cross-linked from `knowledge/preconditions.md`). Any toleration/patch the agent proposes is authored **into the session workspace** and applied only as an approval-gated mutating step; the sibling repos stay read-only.
- **ACCEPTANCE:** given canned OpenShift node labels the agent prefers `oc` and surfaces the ServiceMesh/L40S-taint guidance; given GKE/DOKS labels it surfaces the `nvidia.com/gpu` toleration plus the GMP / "Undetected platform" / NVSHMEM known-issue notes and an approval-gated toleration patch — with every which-CLI / which-toleration / which-known-issue decision sourced from `knowledge/infra_providers.yaml`, not Python `if/elif`.
- **HERMETIC-TEST:** feed canned node-label JSON fixtures for openshift / gke / doks to the detection probe and assert the emitted provider/taint facts; assert in `tests/test_allowlist.py` that `oc` validates against the **same** read-only constraints as `kubectl` (the equivalent read-only subcommands accepted, mutating/unknown subcommands denied). No GPU, no live cluster, no real benchmark run.

## Phase 65 — Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool Accepted/ResolvedRefs) — DONE
*Source: guides/prereq/gateways/gke.md (via docs/USEFUL_REPO_DOCS.md). Extends the endpoint-readiness gate to the Gateway-API control plane (GKE/Istio/agentgateway), closing the "model pods up but no traffic reaches them" blind spot for gateway-mode deploys.*

**RESULT:** Extended `app/orchestrator/readiness.py` to parse `kubectl get gateway,gatewayclass,inferencepool,httproute -o json` status conditions into FACTS on the `EndpointReadiness` verdict — Gateway `PROGRAMMED`, InferencePool `Accepted`/`ResolvedRefs`, HTTPRoute `Accepted`/`Reconciled`, GatewayClass-exists — never branching on them; threaded through `app/tools/readiness.py` + the verdict schema/`check_gateway` flag in `app/tools/schemas.py`, registered in `app/tools/registry.py`. `security/allowlist.yaml` (DATA only) gains `gateway`/`gatewayclass`/`inferencepool`/`httproute` to the read-only `kubectl_resource` enum. The wait-vs-stand-up-vs-config-error judgment (incl. the GKE fault-filter-abort symptom) is pure knowledge in new `knowledge/gateway_readiness.md`. New hermetic tests `tests/test_gateway_readiness.py` (canned Gateway/InferencePool/HTTPRoute JSON permutations + allowlist asserts). Merged into `feature/roadmap-v4`. Suite **923 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** in gateway-mode deploys the agent can distinguish "model pods are Ready" from "traffic can actually reach them" — so it says *"the model pods are up, but the Gateway is still `PROGRAMMED:False` (or the InferencePool isn't `ResolvedRefs`), so no traffic reaches them yet"*, a distinct, common not-ready state that today's pod/endpoint check misses.
- **BUILD:** extend the readiness analyzer (`app/orchestrator/readiness.py`) to also parse `kubectl get gateway,gatewayclass,inferencepool,httproute -o json` status conditions — Gateway `PROGRAMMED`, InferencePool `status.parents[].conditions` (`Accepted`/`ResolvedRefs`), and GatewayClass existence — into new fields on the `EndpointReadiness` verdict (mechanism: it extracts conditions into facts, never branches on them); thread them through the tool layer (`app/tools/readiness.py` + the verdict schema in `app/tools/schemas.py`). Widen `security/allowlist.yaml` (DATA only) by adding `gateway`/`gatewayclass`/`inferencepool`/`httproute` to the `kubectl_resource` enum as read-only `get -o json` data. JUDGMENT — what `PROGRAMMED:False` / the GKE fault-filter-abort symptom mean, how long each is expected to take, and whether to wait vs. stand up vs. surface a config error — lives in a new `knowledge/gateway_readiness.md` (no `if/elif` decision logic in Python; the parser extracts conditions, the agent interprets). Repos stay read-only; the analyzer only reads `kubectl` output and never writes.
- **ACCEPTANCE:** given Gateway/InferencePool/GatewayClass JSON, the verdict carries `PROGRAMMED` + `Accepted`/`ResolvedRefs` condition facts and the GatewayClass-exists fact; pods-Ready-but-`PROGRAMMED:False` yields a not-ready verdict with a gateway-specific reason token; a fully-programmed gateway with `ResolvedRefs:True` yields ready; all wait-vs-standup-vs-error decisions come from `knowledge/gateway_readiness.md`, not Python.
- **HERMETIC-TEST:** feed canned Gateway/InferencePool JSON permutations (`PROGRAMMED` True/False, `ResolvedRefs` True/False, GatewayClass present/absent) into the analyzer and assert the verdict `ready` boolean + reason/condition tokens for each; assert `security/allowlist.yaml` permits exactly the four new read-only `kubectl_resource` values under `get`. No GPU, no live cluster, no real benchmark.

## Phase 66 — EPP HTTP-header decoder (interpret 429s + `x-llm-d-request-dropped-reason`) — DONE
*Source: docs/api-reference/epp-http-headers.md (via docs/USEFUL_REPO_DOCS.md). Turns opaque "some requests failed" into a grounded admission/eviction read, closing the coverage gap where the agent can't explain EPP-surfaced 429s.*

> **RESULT (merged into feature/roadmap-v4):** Shipped DATA-only. New `knowledge/epp_headers.yaml` catalogues every EPP request/response header (the SLO set-headers `x-llm-d-slo-ttft-ms`/`x-llm-d-slo-tpot-ms` + `x-llm-d-inference-objective`/`x-llm-d-inference-fairness-id`) and the `x-llm-d-request-dropped-reason` enum → plain-language cause/remedy (`rejected-saturated` = at admission capacity, shed before serving; `evicted-priority` = preempted mid-flight by higher-priority work), plus deprecated aliases — so a 7%-failure run reads as an admission/eviction (capacity) signal, not breakage. Wired into `CORE_KNOWLEDGE` (`app/agent/prompt.py`) so it's reachable via `read_knowledge("epp_headers")`, and `knowledge/results_interpretation.md` now routes failed-request/429 interpretation there. The rejected-vs-evicted-vs-broken classification lives entirely in `knowledge/` — no Python `if/elif`. Hermetic `tests/test_epp_headers.py` (15 tests) assert the YAML loads, is reachable via `read_knowledge`, documents both drop-reason enum values + the four SLO/objective/fairness header names, and is listed in `CORE_KNOWLEDGE`. Suite **938 passed / 20 skipped / 0 failed**; ruff + mypy clean.

- **GOAL:** stop reporting failed requests as "the system was broken." When a benchmark encounters EPP request/response headers, let the agent decode them — the SLO set-headers (`x-llm-d-slo-ttft-ms` / `x-llm-d-slo-tpot-ms` / `x-llm-d-inference-objective` / `x-llm-d-inference-fairness-id`) and especially the `x-llm-d-request-dropped-reason` enum (`rejected-saturated` / `evicted-priority`) — so a 7%-failure run reads as "rejected at admission capacity, not failing — lower concurrency or scale out" instead of "some requests failed."
- **BUILD:** mostly-DATA. Author `knowledge/epp_headers.yaml` cataloguing each header name → meaning and the `x-llm-d-request-dropped-reason` enum → plain-language cause (`rejected-saturated` = at admission capacity, shed before serving; `evicted-priority` = preempted mid-flight by higher-priority work), plus the deprecated header aliases; wire it into `CORE_KNOWLEDGE` (`app/agent/prompt.py`) so it's reachable via `read_knowledge` (`app/tools/probe.py`), and point `knowledge/results_interpretation.md` at it. Optional thin mechanism only: IF a harness/report surfaces these headers or 429 counts, mechanically attach them to the results summary and let the LLM map them via the YAML — **no decision logic in Python** (the rejected-vs-evicted-vs-broken judgment lives entirely in `knowledge/`). Repos stay read-only; nothing is authored outside the session workspace.
- **ACCEPTANCE:** given a run that hit EPP drops, the agent loads `epp_headers` and explains the failure fraction as a saturation/eviction signal (capacity, not breakage) with the right remedy — and the SLO set-headers are decoded — with the rejected/evicted/broken classification coming from `knowledge/epp_headers.yaml`, not a Python `if/elif`.
- **HERMETIC-TEST:** assert `knowledge/epp_headers.yaml` loads and is reachable via `read_knowledge("epp_headers")`; assert it documents the `x-llm-d-request-dropped-reason` enum (both `rejected-saturated` and `evicted-priority`) and the four SLO/objective/fairness header names; assert it's listed in `CORE_KNOWLEDGE`. No GPU, no live cluster, no real benchmark run.

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
