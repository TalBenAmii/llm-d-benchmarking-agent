# PROGRESS LOG

Reverse-chronological log of the autonomous roadmap effort. One entry per work session /
phase milestone. See [`ROADMAP.md`](ROADMAP.md) for the plan and phase status.

Branch: `feature/roadmap` (integration; never merged to `main` during this effort).
Test baseline at start (primary checkout `main` @ `04c06fe`): **111 passed / 5 skipped**.
Latest completed phase: **Phase 26** (suite **591 passed / 9 skipped**). Each completed phase below
is collapsed to a one-liner (date · phase · what shipped · branch · final suite count); see git
history for the full per-phase narrative. ROADMAP_V4.md (Phases 27-58) is the forward-looking plan.

---

## Completed phases (newest first)

- 2026-06-04 — Phase 33 (ROADMAP_V4): Multi-stack scenarios + `--stack` subset + `--parallel` cap.
  Modeled two previously-unmodeled multi-stack flags as pure mechanism + DATA (judgment in `knowledge/`):
  `build_argv` (`app/tools/execute.py`) now emits subcommand-aware `--stack <names>` on standup/smoketest/
  run/teardown to restrict a multi-stack scenario (N model pools behind one gateway) to a single stack or a
  comma-separated subset, and `--parallel <int>` on standup/smoketest/experiment (an `is not None` guard so an
  explicit `0` is honored) to cap how many stacks deploy at once — kept DISTINCT from the existing
  `--parallelism`/`-j` harness-pod count (no regression). `schemas.py`/`registry.py` document both flags and
  point at the judgment; `security/allowlist.yaml` (DATA) gains a `stack_list` value constraint (N RFC1123
  labels) + the two flags on the matching subcommands. `knowledge/multi_stack.md` carries the WHICH-subset /
  WHEN-to-cap judgment; `tests/test_multi_stack.py` adds 48 hermetic tests (emission, subcommand guards,
  explicit-0, `-j` non-regression, allowlist value pinning + injection refusal, schema, knowledge
  discoverability). Merged into `feature/roadmap-v4` (no-ff; no conflicts — branch was directly ahead of HEAD).
  Suite **1153 passed / 20 skipped / 0 failed**; ruff + mypy clean. Branch `feature/roadmap-v4-p33-multi-stack`. — done
- 2026-06-04 — Phase 53 (ROADMAP_V4): convert-guide (guide → scenario/experiment file generation).
  Shipped `convert_guide_to_scenario` (`app/tools/convert_guide.py`), the workspace-only variant of upstream
  `skills/convert-guide`: emits `ai.<name>.sh` (sorted, `shlex.quote`-safe `export LLMDBENCH_*` lines with
  `# SOURCE:` provenance) plus a validatable companion `ai.<name>.yaml`/`.spec.yaml` (reusing the Phase-45
  config_artifact mechanism so plan/--dry-run has a real `--spec` target). `LLMDBENCH_*` mappings + standard
  practices are DATA in `knowledge/convert_guide.md` (thin code); all four outputs confined to `ctx.workspace`
  — the read-only repos are never written, no allowlist change. Registered in `registry.py`/`schemas.py`;
  28 new hermetic tests in `tests/test_convert_guide.py`. Merged into `feature/roadmap-v4`. Suite
  **1105 passed / 20 skipped / 0 failed**; ruff + mypy clean.
- 2026-06-04 — Phase 41 (ROADMAP_V4): Dataset replay URL (`-x`/`--dataset`). Promoted real-dataset
  replay from unsupported (synthetic profiles only) to a modeled `flags["dataset"]`; `build_argv`
  (`app/tools/execute.py`, `schemas.py`) emits `-x <url>` ONLY on `run`/`experiment` (the two subcommands
  upstream accepts it on) so the harness REPLAYS a real dataset instead of the synthetic workload profile,
  omitted ⇒ synthetic still drives the load. `-x`/`--dataset` are allowlisted on both subcommands with a
  `dataset_url` value constraint (http(s)/hf/gs/s3 scheme or bare path; `security/allowlist.yaml`, DATA-only);
  no env var is set here — the CLI derives `LLMDBENCH_RUN_DATASET_DIR/_FILE` from the URL. `knowledge/dataset_replay.md`
  documents WHEN to replay vs stay synthetic; new hermetic suite `tests/test_dataset_replay.py` (+21 tests).
  Merged into `feature/roadmap-v4` (no-ff); resolved additive/structural conflicts vs Phases 29/31/36 by
  composing the union (kept every existing flag + the new `dataset` one). Full suite **1063 passed / 20 skipped
  / 0 failed**; ruff + mypy clean. Branch `feature/roadmap-v4-p41-dataset-replay`. — done
- 2026-06-04 — Phase 46 (ROADMAP_V4): Kustomize deploy config block (`kustomize.*`). Promoted the kustomize
  deploy method from "only `-t kustomize` is allowlisted" to first-class config authoring:
  `write_and_validate_config(artifact_type='scenario')` now authors the full `kustomize.*` block
  (enabled/guideName/repoPath/repoRef/acceleratorBackend/monitoring/overlayPath/extraHelmValues/extraHelmSets/
  guideVariableOverrides + a list of strategic-merge `patches`), deep-merged onto a minimal `scenario:` skeleton
  and shape-validated against the repo's own scenario examples. `build_argv` (`app/tools/execute.py`,
  `schemas.py`) threads `flags["repo_path"]` as the real standup `--llmd-repo-path` flag (the CLI fallback for
  `kustomize.repoPath`), allowlisted + path-constrained (no `..`; `security/allowlist.yaml`, DATA-only).
  `knowledge/deploy_path_playbook.md` carries the WHICH-guide/overlay/patches judgment; new hermetic suite
  `tests/test_kustomize_block.py` (+14 tests). Merged into `feature/roadmap-v4` (no-ff); resolved additive
  conflicts in `schemas.py`/`execute.py` by composing the union (kept every prior flag — skip/step/dataset/
  cluster_url/cluster_token — plus the new `methods`/`repo_path`). Full suite **1077 passed / 20 skipped /
  0 failed**; ruff + mypy clean. Branch `feature/roadmap-v4-p46-kustomize-block`. — done

- 2026-06-04 — Phase 36 (ROADMAP_V4): First-class skip / collect-only mode (`-z`/`--skip`). Promoted
  collect/analyze-only from the raw `extra` passthrough to a modeled `flags["skip"]`; `build_argv`
  (`app/tools/execute.py`, `schemas.py`) emits a bare `-z` on `run` so the agent can re-collect/re-analyze the
  EXISTING results of a prior run WITHOUT re-running the benchmark load. `-z`/`--skip` are allowlisted as
  `read_only_trigger` on `run` ALONE (`security/allowlist.yaml`, DATA-only) so it auto-runs like
  `--list-endpoints`/`--dry-run`. Added `knowledge/collect_only.md` (when to re-collect vs re-run). Merged into
  feature/roadmap-v4 — resolved additive conflicts in `schemas.py` (flag-list union kept Phase 29/31
  cluster_url/cluster_token/step) and `allowlist.yaml` (kept both the Phase 31 `-s` and Phase 36 `-z` blocks); the
  Phase 27/29/31/36 build_argv wiring auto-merged into one coherent function. Suite **1042 passed / 20 skipped**
  (+11 new `tests/test_collect_only.py`), ruff + mypy clean. Branch `feature/roadmap-v4-p36-skip-collect`. — done
- 2026-06-04 — Phase 31 (ROADMAP_V4): First-class step selection / re-run (`-s`/`--step`). Promoted step
  selection from the raw `extra` passthrough to a modeled `flags["step"]` (step-list grammar `N / N-M / comma`,
  e.g. `5`, `5-9`, `3-5,9`); `build_argv` (`app/tools/execute.py`, `schemas.py`) emits `-s <spec>` on
  standup/smoketest/run/teardown so a single failed step (or range) can be re-run instead of redoing the whole
  phase. Value-pinned by a new `step_list` allowlist regex (`^[0-9]+([,-][0-9]+)*$`) on all four subcommands
  (`security/allowlist.yaml`, DATA-only); `-s` does NOT change a command's mode, so mutating re-runs stay
  approval-gated. Added `knowledge/step_select.md` (per-phase step numbering + when to re-run). Merged into
  feature/roadmap-v4 (schemas.py flag-list union kept the Phase 29 cluster_url/cluster_token entries); suite
  **1031 passed / 20 skipped** (+63 new tests), ruff + mypy clean. Branch `feature/roadmap-v4-p31-step-select`. — done
- 2026-06-04 — Phase 29 (ROADMAP_V4): Explicit cluster access (`-k`/`--kubeconfig` FILE + backend-only URL/token).
  A top-level `kubeconfig` field on `ExecuteInput` emits `-k <path>` after every subcommand via `build_argv`
  (`app/tools/execute.py`, `schemas.py`) to target a non-default kubeconfig FILE — a plain, allowlist-pinned,
  non-secret path (no `..`; `security/allowlist.yaml` widened DATA-only). The remote-by-URL+TOKEN route stays
  BACKEND-ONLY: `flags.cluster_url`/`flags.cluster_token` ride the same scrubbed `child_env` overlay as
  `LLMDBENCH_HARNESS_CPU_NR` (forwarded as `LLMDBENCH_CLUSTER_URL`/`LLMDBENCH_CLUSTER_TOKEN`), so the SECRET token
  never crosses argv/allowlist, a `command` event, or a log (mirrors the HF_TOKEN non-leak). Judgment (WHEN/WHICH
  cluster) in `knowledge/preconditions.md`. 30 hermetic tests (`tests/test_cluster_access.py`); no live cluster.
  Merged into `feature/roadmap-v4`. Suite **968 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 66 (ROADMAP_V4): EPP HTTP-header decoder (interpret 429s + `x-llm-d-request-dropped-reason`).
  DATA-only. New `knowledge/epp_headers.yaml` catalogues every EPP request/response header — the SLO set-headers
  `x-llm-d-slo-ttft-ms`/`x-llm-d-slo-tpot-ms` + `x-llm-d-inference-objective`/`x-llm-d-inference-fairness-id` — and the
  `x-llm-d-request-dropped-reason` enum → plain-language cause/remedy (`rejected-saturated` = at admission capacity, shed
  before serving → lower concurrency or scale out; `evicted-priority` = preempted mid-flight by higher-priority work →
  raise this request's inference-objective priority or add capacity), plus the deprecated header aliases. Wired into
  `CORE_KNOWLEDGE` (`app/agent/prompt.py`) so it's reachable via `read_knowledge("epp_headers")`, and
  `knowledge/results_interpretation.md` now routes failed-request/429 interpretation there, reframing a non-100%
  `success_rate` as an admission/eviction (capacity) signal rather than "the system was broken." The
  rejected-vs-evicted-vs-broken classification lives entirely in `knowledge/` — no Python `if/elif`. New hermetic
  `tests/test_epp_headers.py` (15 tests) asserts the YAML loads, is reachable via `read_knowledge`, documents both
  drop-reason enum values (`rejected-saturated`, `evicted-priority`) + the four SLO/objective/fairness header names, and
  is listed in `CORE_KNOWLEDGE` — no GPU, no live cluster, no real benchmark run. Merged into `feature/roadmap-v4`. Suite
  **938 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 65 (ROADMAP_V4): Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool
  Accepted/ResolvedRefs). Extended `check_endpoint_readiness` to the Gateway-API control plane via a new `check_gateway`
  flag (on by default; `app/tools/schemas.py`, registered in `app/tools/registry.py`). In gateway-mode deploys
  `app/orchestrator/readiness.py` now reads `kubectl get gateway,gatewayclass,inferencepool,httproute -o json` and folds
  the status conditions into FACTS on the `EndpointReadiness` verdict — Gateway `PROGRAMMED`, InferencePool
  `Accepted`/`ResolvedRefs`, HTTPRoute `Accepted`/`Reconciled`, and the GatewayClass-exists fact — never branching on
  them (the parser extracts conditions; the agent interprets), threaded through the tool layer in `app/tools/readiness.py`.
  This tells "the model pods are Ready" apart from "traffic can actually reach them" (pods can be Ready while the Gateway
  is still `PROGRAMMED:False`). `security/allowlist.yaml` (DATA only) widens the read-only `kubectl_resource` enum with
  `gateway`/`gatewayclass`/`inferencepool`/`httproute` (read-only `get -o json` only; gatewayclass cluster-scoped). The
  wait-vs-stand-up-vs-config-error judgment — incl. the GKE fault-filter-abort symptom and expected timings — is pure
  JUDGMENT in new `knowledge/gateway_readiness.md` (no `if/elif` in Python). New hermetic suite
  `tests/test_gateway_readiness.py` feeds canned Gateway/InferencePool/HTTPRoute JSON permutations (PROGRAMMED True/False,
  ResolvedRefs True/False, GatewayClass present/absent) and asserts the verdict + condition tokens, plus an allowlist
  assert that exactly the four new read-only `kubectl_resource` values are permitted under `get` — no GPU, no live
  cluster, no real benchmark run. Merged into `feature/roadmap-v4`. Suite **923 passed / 20 skipped / 0 failed**; ruff +
  mypy clean. — done
- 2026-06-03 — Phase 64 (ROADMAP_V4): Provider-aware precondition pack (oc-vs-kubectl, GPU taints/tolerations,
  GMP/known-issues). Added a read-only `provider_detection` capability to the `probe_environment` tool
  (`app/tools/probe.py`, registered in `app/tools/registry.py`, enum widened in `app/tools/schemas.py`) that reuses the
  already-allowlisted `kubectl get nodes -o json` to emit FACTS only — the detected cloud `provider`
  (openshift/gke/doks/aks vs a `kind` default), `providers_seen`, per-node `gpu_taints` `{node,key,value,effect}`, and
  per-node label/taint facts — via a plain label-prefix membership lookup (the mechanism prefix table is kept in lockstep
  with the knowledge file by a test), with NO provider `if/elif` in Python. `security/allowlist.yaml` (DATA only) gains an
  `oc:` tool entry carrying the SAME constrained read-only subcommands as `kubectl` (shared `kubectl_resource`/
  `output_format`/`namespace`/`label_selector` refs — DATA, no oc-vs-kubectl branching). The per-provider playbook (which
  CLI, which taint/toleration to author, which known issue — GMP / "Undetected platform" / NVSHMEM — applies) is pure
  JUDGMENT in new `knowledge/infra_providers.yaml`, cross-linked from `knowledge/preconditions.md`; any toleration/patch is
  authored into the session workspace and applied only as an approval-gated mutating step. New hermetic suites
  `tests/test_provider_pack.py` + `tests/test_allowlist.py` (oc validates against the same read-only constraints as
  kubectl; mutating/unknown subcommands denied) — no GPU, no live cluster, no real benchmark run. Merged into
  `feature/roadmap-v4`. Suite **903 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 48 (ROADMAP_V4): Parse + surface `results.session_performance` (multi-turn). `app/validation/report.py`
  gained `extract_session_performance`, which mechanically pulls the `results.session_performance.sessions` stats block
  (session_rate/duration, events/tokens per session) with field-name discovery as DATA in `knowledge/standard_metrics.yaml`
  (thin code / thick agent) — surfaced on the report summary and per-run in `analyze_results` (`app/tools/analyze.py`).
  Single-turn reports yield `None` with no fabrication; the committed BR v0.2 schema lags, so a multi-turn report surfaces
  session_performance as a non-fatal additionalProperties deviation (validate_report untouched). `knowledge/results_interpretation.md`
  documents the multi-turn section. New hermetic suite `tests/test_session_performance.py` (multi-turn surfacing, single-turn
  None, catalog-driven discovery, validation deviation, analyze end-to-end). Merged into `feature/roadmap-v4`. Suite
  **879 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 30 (ROADMAP_V4): HuggingFace gated-model secret provisioning. New approval-gated mutating tool
  `provision_hf_secret` (`app/tools/hf_secret.py`, registered in `app/tools/registry.py`, `ProvisionHfSecretInput` in
  `app/tools/schemas.py`) materializes the cluster HF-token Secret (`llm-d-hf-token`) a gated-model standup needs — the
  follow-on to the Phase 62 gated-access pre-flight. The token stays BACKEND-ONLY: a vetted `scripts/provision_hf_secret.py`
  (allowlisted `project-script`, committed 0755) reads `HF_TOKEN` from the already-scrubbed child env and runs the upstream
  `kubectl create secret … --dry-run=client -o yaml | kubectl apply -f -` shape over its OWN `shell=False` subprocess, so
  the token never crosses the allowlist/argv or reaches a command event/log (a raw `kubectl create secret` is deliberately
  NOT allowlisted). Judgment lives in `knowledge/capacity.md`. 22 hermetic tests (`tests/test_hf_secret.py`), incl. two
  real-runner-exec tests; no live cluster/network/GPU. Merged into `feature/roadmap-v4`. Suite **857 passed / 20 skipped /
  0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 63 (ROADMAP_V4): Accelerator + CPU-inferencing precondition advisor ("can my hardware run
  this?"). Added a read-only `advise_accelerators` probe (`app/tools/probe.py`, registered in `app/tools/registry.py`
  with `AdviseAcceleratorsInput` in `app/tools/schemas.py`) that runs the already-allowlisted `kubectl get nodes -o
  json` and mechanically extracts per-node `capacity`/`allocatable` cpu + memory (raw K8s quantity, never lossily
  converted) and the advertised accelerator extended-resource keys (`nvidia.com/gpu` + amd/gaudi/tpu/Intel-XPU
  siblings) — FACTS only (`any_accelerator`, `cpu_only`, `advertised_resources`, per-node `accelerators`); no allowlist
  change. All feasibility judgment — CUDA/driver minimums, Device-Plugin vs DRA, the real-CPU 64c/64GB-per-replica
  floor, and the Kind/CPU-sim exemption — lives as DATA in new `knowledge/accelerators.yaml` (pointers from
  `knowledge/preconditions.md` + `knowledge/capacity.md`); no `if/elif` feasibility branch in Python. Complements
  `check_capacity`'s GPU-memory sizing. Hermetic `tests/test_accel_advisor.py` feeds canned GPU-advertised + CPU-only
  `kubectl get nodes` fixtures through a fake runner and asserts the extracted facts and knowledge floors; no GPU, no
  live cluster. Merge into `feature/roadmap-v4` reconciled the new probe + helpers additively against Phase 60's
  `cluster_preconditions` probe (both probes and both helper sets preserved). Suite **835 passed / 20 skipped / 0
  failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 60 (ROADMAP_V4): Infra precondition gate before a long real-cluster standup. Added a
  `cluster_preconditions` read-only probe to `probe_environment` (`app/tools/probe.py`): a read-only
  `kubectl version --output json` (already allowlisted) parsed into `cluster_info.server_version` `{major, minor}`,
  plus the spec's pinned vLLM/NIXL/UCX/NVSHMEM `{repository, tag}` image tags parsed off the rendered scenario YAML.
  The probe reports FACTS only — no version-comparison `if/elif` in Python. The thresholds (K8s ≥1.29, 1.33+ for
  sidecars, the ≤1.28 Init:0/1 stall gotcha, vLLM 0.10.0+ / NIXL 0.5.0+ / UCX 0.19.0+ / NVSHMEM 3.3.9+) and verdict
  bands live as DATA in new `knowledge/infrastructure_preconditions.yaml` (+ prose in `knowledge/preconditions.md`);
  the LLM reasons over the table to issue the go/no-go. The `schemas.py` probe field was extended additively — the
  Phase 28 model-override and Phase 45 vLLM-knob entries on the diverged base are both preserved. Hermetic
  `tests/test_infra_preconditions.py` (10 tests, + command-event coverage) feeds canned 1.27/1.29/1.33 `kubectl version`
  output and image tags through a fake runner and asserts the extracted facts and the knowledge thresholds; no live
  cluster, no GPU, no real benchmark run. Branch `feature/roadmap-v4-p60-infra-precond` → `feature/roadmap-v4` (no-ff).
  Full suite **818 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done

- 2026-06-03 — Phase 49 (ROADMAP_V4): Surface results.observability serving metrics in the trend store. Added the 3
  §3.4 standard/serving metrics — KV-cache hit rate, GPU utilization, and schedule-delay (queue-depth proxy) — to
  `app/storage/history.py` `_TREND_METRICS` at their nested `standard_metrics.<key>.value` stat path. They are present
  only when the run used monitoring (Phase 27 / `flags.monitoring`) so `results.observability` was populated; `trend()`
  simply skips records lacking the metric on non-monitoring runs. Labelled informationally (same as the analyzer's
  Pareto objectives) — they NEVER affect dominance/pass-fail. New hermetic tests in `tests/test_history.py`;
  `knowledge/history.md` + `knowledge/results_interpretation.md` updated. Riding on the Phase 27 producer, this closes
  the last slice of the standard-serving-metrics catalog row (🟡 → ✅). Merged into `feature/roadmap-v4` (no-ff).
  Full suite **806 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done

- 2026-06-03 — Phase 45 (ROADMAP_V4): Author per-knob vLLM scenario overrides. Extended in-workspace config
  authoring (`app/tools/config_artifact.py`) so the agent can set finer vLLM/scheduling/storage knobs by DOTTED upstream
  field path — `vllmCommon.flags.*`, `vllmCommon.kvTransfer.*`, `vllmCommon.kvEvents.*`, `vllmCommon.priorityClassName`,
  `vllmCommon.ephemeralStorage`, `vllmCommon.networkResource`, `affinity.*`, `schedulerName` — writing into the session
  workspace (the sibling repos stay read-only) and validating via the CLI plan/`--dry-run` determinism gate. WHICH knobs
  to set is JUDGMENT, not Python: new `knowledge/vllm_overrides.md` (no enumerable knob catalog, no value `if/elif`).
  `security/allowlist.yaml` gains a value-pinned `model_id` + workspace-confined `--spec` file rule; `app/security/
  allowlist.py`, `registry.py`, and `schemas.py` wired additively (Phase 28 model-override entries preserved alongside).
  Hermetic `tests/test_scenario_overrides.py` (26 tests) covers each knob path, structural validation against the repo
  example shape, and the no-write-into-read-only-repo guarantee. Branch `feature/roadmap-v4-p45-vllm-overrides` →
  `feature/roadmap-v4` (merge `a56eee7`). Suite **802 passed / 20 skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 28 (ROADMAP_V4): First-class model override (`-m/--models`). A top-level `models` field on
  `ExecuteInput` threads through `execute_llmdbenchmark` into `build_argv` (`app/tools/execute.py`), emitting `-m <id>`
  only when present — `-m` is the one short form valid across standup/plan/run/experiment (upstream uses `--models` on
  standup/plan/experiment, `--model` on run). `security/allowlist.yaml` (DATA) gains a value-pinned, metachar-screened
  `model_id` constraint plus the `-m`/`--models`/`--model` flagspecs under those four subcommands. Model lockstep with
  the capacity pre-flight (pass the SAME id to `check_capacity` so it sizes + gated-checks the identical model) is
  knowledge, not Python: new `knowledge/model_override.md` + a `knowledge/capacity.md` cross-link; no on-disk model
  catalog and no value `if/elif`. Hermetic `tests/test_model_override.py` asserts `-m` is emitted per subcommand, the
  allowlist permits + value-pins it and refuses injection, and the standup id + the `check_capacity` override resolve to
  the IDENTICAL `plan_config` path. Also de-flaked a pre-existing full-suite-only race in `tests/test_concurrency.py`
  (target the teardown gate by `tool_call_id` instead of an arbitrary first pending key) — no assertion weakened. Branch
  `feature/roadmap-v4-p28-model-override` → `feature/roadmap-v4`. Suite **776 passed / 20 skipped / 0 failed** (5
  consecutive clean full runs); ruff + mypy clean. — done
- 2026-06-03 — Phase 62 (ROADMAP_V4): Gated-model access pre-flight before standup. The already-allowlisted read-only
  capacity bridge `scripts/capacity_check.py` (driven by `app/capacity/planner.py`) now also calls the benchmark repo's
  OWN `llmdbenchmark.utilities.huggingface.check_model_access` / `GatedStatus` (never reimplemented) and returns a
  token-free `{gated, authorized, reason}` block alongside the sizing verdict. `CapacityVerdict` gained
  `gated`/`authorized`/`gated_reason` fields (defaulted → non-gated/legacy paths unchanged), wired via a pure-field-copy
  `merge_gated_access` (no `if/elif`). Per-status judgment (PUBLIC/authorized → proceed; gated+unauthorized → provision
  the secret via Phase 30) lives in `knowledge/capacity.md`, not Python. `HF_TOKEN` is read from the scrubbed child env
  only and never echoed into the result, events, or logs. No allowlist change. Hermetic tests
  (`tests/test_capacity_gated.py`) drive a fixture `ModelAccessResult` per `GatedStatus` and assert the verdict + token
  non-leak. Merged into `feature/roadmap-v4`; suite **756 passed / 20 skipped**; ruff + mypy clean.
- 2026-06-03 — Phase 61 (ROADMAP_V4): Right-size the harness launcher CPU for small/Kind clusters. Added a read-only
  `node_capacity` probe (per-node allocatable/capacity CPU + min-allocatable across nodes via `kubectl get nodes -o
  json`) to `probe_environment` (`app/tools/probe.py`), and a backend-only `harness_cpu_nr` flag plumbed as the
  `LLMDBENCH_HARNESS_CPU_NR` env var through `execute.py` → `context.run_command(env=)` → `runner._build_env` (merged
  last so it wins; never an allowlist flag, never reaches the browser); the lower-it-or-not / to-what (inference-perf
  multi-process vs vllm-benchmark single-process) judgment lives in `knowledge/harness_sizing.md`, not Python. Turns a
  silent `FailedScheduling`/`Pending` launcher pod into a scheduled run on the MVP Kind path. Merge into
  `feature/roadmap-v4` reconciled the two newly-added probes against Phase 27/59 (probe-emit/exec parity count 7→8:
  `prometheus_crds` + `node_capacity`). Branch `feature/roadmap-v4-p61-harness-cpu-size`. Suite **735 passed / 20
  skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 59 (ROADMAP_V4): Model-load serving-readiness gate (`/v1/models` vs `/health` + stuck-pod
  diagnostics). Extended the endpoint-readiness path (`app/orchestrator/readiness.py`, `app/tools/readiness.py`,
  `app/tools/registry.py`) to classify a `Running`-but-`NotReady` model server as "still loading weights (keep
  waiting)" vs "wedged/broken (stop waiting)" from pod readiness conditions / `restartCount` / age (8000 prefill,
  8200 decode) plus a GET-only `curl` probe pinned by `security/allowlist.yaml` to the enum `{/v1/models, /health}`
  on in-namespace `*.svc` URLs. The loading-vs-broken JUDGMENT lives in the new `knowledge/readiness_probes.md`
  (no Python `if/elif`). Hermetic fixtures only (canned `kubectl`/`curl` bodies). Suite: 723 passed, 20 skipped,
  0 failed (ruff + mypy clean).

- 2026-06-03 — Phase 27 (ROADMAP_V4): Default-enable benchmark `--monitoring` + surface `results.observability`
  (THE headline observability gap — closed). Added a subcommand-aware `monitoring` flag to `ExecuteInput.flags` +
  `build_argv`: `--monitoring` for standup/run/experiment/plan, `--no-monitoring` only for standup (matching upstream
  argparse store_true vs both-flags); allowlisted those flags per subcommand (DATA-only `security/allowlist.yaml`);
  added a read-only `_probe_prometheus_crds` probe (`app/tools/probe.py`, key `prometheus_crds`) that reports
  PodMonitor/ServiceMonitor CRD presence so the on/off + CRD opt-out JUDGMENT lives in `knowledge/observability.md`
  + `knowledge/results_interpretation.md`, not Python. Phase 35 (standup PodMonitor/ServiceMonitor + EPP verbosity)
  folded in as a sub-deliverable. Unblocks Phase 49 (trend-store consumer). Merged into `feature/roadmap-v4`
  (`feature/roadmap-v4-p27-monitoring-activate`). Suite **692 passed / 20 skipped** (+26 from the 666 baseline;
  new `tests/test_monitoring_activate.py`); ruff + mypy clean. — done
- 2026-06-02 — Phase 26: llm-d-inference-sim integration tests (opt-in). Proposal §5.3/§7 integration
  layer (`tests/integration/`) drives a sim-shaped BR v0.2 fixture through real `analyze_results`/
  `compare_reports`; a live sim test is opt-in (`LLMD_SIM_INTEGRATION=1`) and skips cleanly otherwise;
  non-gating CI job + `test_quality_gates` opt-in assertion; `knowledge/sim_integration.md`,
  `docs/VALIDATION.md`. Branch `feature/roadmap-v3-p26-sim-integration`. Suite **591/9 skipped**. — done
- 2026-06-02 — Phase 25: Analyzer metric completeness. `summarize_report`/`analysis.py` parse + surface
  §3.4 standard serving metrics (KV-cache hit rate, schedule delay, GPU util) from BR v0.2 or harness
  output; candidate field names are DATA in `knowledge/standard_metrics.yaml`; absent ⇒ `None` (never
  fabricated); INFORMATIONAL Pareto objectives kept out of dominance. Branch
  `feature/roadmap-v3-p25-analyzer-metrics`. Suite **509/7 skipped**. — done
- 2026-06-02 — Phase 24: Endpoint health-check before submit (+ optional auto-standup). New read-only
  `check_endpoint_readiness` tool over `kubectl get endpoints` (+ `run --list-endpoints` corroboration);
  `orchestrate_benchmark_run` gates on it by default, unready ⇒ no mutation + approval-gated standup
  suggestion. Branch `feature/roadmap-v3-p24-health-check`. Suite **584/7 skipped**. — done
- 2026-06-02 — Phase 23: Resource management (node affinity / GPU selection / anti-starvation). Optional
  `Scheduling` value object on `JobSpec`/`build_job_manifest` (node_selector, tolerations, affinity, pod
  anti-affinity from `avoid_labels`); unset ⇒ byte-for-byte baseline manifest; WHICH/WHERE judgment is
  DATA in `knowledge/resource_management.md`. Branch `feature/roadmap-v3-p23-resource-mgmt`. Suite
  **556/7 skipped**. — done
- 2026-06-02 — Phase 22: DOE checkpoint/resume for long sweeps. Cluster-backed checkpoint/resume for
  `run_sweep` (proposal §3.3/§4) via per-sweep ConfigMap (`app/orchestrator/checkpoint.py`); re-invoking
  with the same `sweep_id` resumes idempotently, skipping COMPLETED treatments; no `sweep_id` ⇒ original
  stateless behavior. Branch `feature/roadmap-v3-p22-checkpoint`. Suite **568/7 skipped**. — done
- 2026-06-02 — Phase 21: Real-time benchmark-pod log streaming. `kube.stream_logs(follow=True)` wired into
  the orchestrator run loop via optional `on_log_line` sink; `orchestrate_benchmark_run` surfaces each line
  as an `output` event (live progress); best-effort (a failing tail never breaks the run). Branch
  `feature/roadmap-v3-p21-log-stream`. Suite **519/7 skipped**. — done
- 2026-06-02 — Phase 20: Well-lit-path advisor. `knowledge/welllit_path_advisor.yaml` maps workload shape →
  llm-d well-lit-path scenario (prefix-heavy→precise-prefix-cache-routing, long-context RAG→pd-disaggregation,
  high-throughput→optimized-baseline, agentic→agentic-tests, default→cicd/kind); wired into CORE_KNOWLEDGE +
  `read_knowledge`; `deploy_path_playbook.md` references it. Branch `feature/roadmap-v3-p20-welllit-advisor`.
  Suite **491/7 skipped**. — done
- 2026-06-02 — Phase 19: DOE experiment-file generator + token-characteristics elicitation. `generate_doe_experiment`
  tool (`app/tools/doe.py`) over pure mechanism in `app/validation/doe.py`: cross-products factors × levels into
  a treatments matrix, emits + structurally validates an experiment YAML (validated live against the benchmark
  example format). WHICH factors live in `knowledge/sweep_playbook.md`. Branch `feature/roadmap-v3-p19-doe-gen`.
  Suite **477/7 skipped**. — done
- 2026-06-02 — Phase 18: Workspace lifecycle — retention/GC + startup self-check. `app/storage/retention.py`
  config-driven GC over scratch areas (policy as DATA in `config.py`, unlimited by default); lifespan runs a
  one-shot startup GC that never prunes a live session; structured `self_check(settings)` surfaced via new
  `/readyz` (200/503); liveness stays on `/healthz`. Branch `feature/roadmap-v2-p18-workspace`. Suite
  **404/6 skipped**. — done
- 2026-06-02 — Phase 17: Operability docs + alert rules. Added `docs/SECURITY.md`, `docs/TROUBLESHOOTING.md`,
  `docs/CONTRIBUTING.md`, `docs/CHANGELOG.md` (linked from `docs/README.md`) + `deploy/observability/alerts.rules.yaml`
  (5 alert rules over exported metrics); `tests/test_ops_docs.py` verifies sections + that every referenced metric
  is actually exported. Docs + data only. Branch `feature/roadmap-v2-p17-ops-docs`. Suite **424/7 skipped**. — done
- 2026-06-02 — Phase 16: Run lifecycle & readiness. `app/agent/lifecycle.py` (`RunRegistry`) + `cancel_run` tool
  that cancels a DIFFERENT session's run (frees the concurrency-cap slot, reaps the child process group, no orphaned
  Job); SIGTERM graceful-shutdown cancels every in-flight run; `cancelled` event + `runner_ok` on `/readyz`; WHEN-to-
  cancel judgment in `knowledge/run_lifecycle.md`. Branch `feature/roadmap-v2-p16-run-lifecycle`. Suite
  **415/6 skipped**. — done
- 2026-06-02 — Phase 15: WebSocket protocol hardening + live event buffer. `/ws` validates every inbound frame
  against a Pydantic tagged union (`ws_schemas.py`, `extra="forbid"`); malformed frame ⇒ structured `protocol_error`
  with the socket kept alive; bounded per-turn live ring buffer replays missed live stream on mid-turn reconnect
  (handshake frames excluded). Branch `feature/roadmap-v2-p15-ws-protocol`. Suite **384/6 skipped**. — done
- 2026-06-02 — Phase 14: Quality gates — ruff + mypy + coverage. Enforced `ruff check`, `mypy app` (strict), and a
  coverage-gated suite (`--cov-fail-under=85`) via Makefile + pyproject + CI; cleaned types/lint to green (no behavior
  change); `tests/test_quality_gates.py` locks config/thresholds/CI. Coverage **88.90%**. Branch
  `feature/roadmap-v2-p14-quality-gates`. Suite **432/7 skipped**. — done
- 2026-06-02 — Phase 13: Allowlist governance — per-command timeouts + quotas. Execution limits moved out of Python
  into `security/allowlist.yaml` as data (optional `timeout_s` + `quota {per_session, per_day}`, schema-validated at
  startup, ridden on `Decision`); runner sources its deadline from `Decision.timeout_s` (removed parallel `_TIMEOUTS`);
  new `app/security/quota.py` refuses over-quota commands before execution; judgment in `knowledge/governance.md`.
  Branch `feature/roadmap-v2-p13-allowlist-gov`. Suite **378/6 skipped**. — done
- 2026-06-02 — Phase 12: API trust — auth + rate-limit + CORS. Stdlib-only, all defaulting OFF/open — optional Bearer
  auth (`secrets.compare_digest`, guards HTTP routes + `/ws` handshake → 401/WS 1008) + `TokenBucket`/`RateLimiter`
  (injectable clock) throttling `/api/*` (`/healthz`+`/metrics` exempt); `CORSMiddleware` only when `CORS_ALLOW_ORIGINS`
  set; fails loud if `AUTH_ENABLED` with empty `AUTH_TOKEN`; judgment in `knowledge/api_trust.md`. Branch
  `feature/roadmap-v2-p12-authz`. Suite **351/6 skipped**. — done
- 2026-06-02 — Phase 11: Structured logging + correlation IDs. Stdlib-only JSON logging (`app/observability/logging.py`
  + `logctx.py` contextvars carrier for `corr_id`/`session_id`/`run_id`/`tool`); a fresh `corr_id` minted at the WS
  handshake propagates into the loop, every tool dispatch, and the runner — trace one turn by `corr_id`, one chat by
  `session_id`; secrets never logged (exec record carries argv[0] only); `LOG_LEVEL`/`LOG_FORMAT` added; judgment in
  `knowledge/logging.md`. Branch `feature/roadmap-v2-p11-logging`. Suite **339/6 skipped**. — done
- 2026-06-01 — Phase 10: Multi-harness orchestration in one session. New read-only `compare_harness_runs` tool
  (`app/tools/multiharness.py`) over pure `compare_across_harnesses()`: groups runs by harness, reports which metrics
  ≥2 harnesses both measured vs only one, side-by-side per-harness values with NO cross-harness winner, refuses unless
  ≥2 distinct harnesses; `summarize_report` surfaces producing harness + load point; judgment in
  `knowledge/multi_harness.md`. (Registry was 17 tools at this point.) Branch `feature/roadmap-p10-multiharness`
  (`60546f4`). Suite **329/6 skipped**. — done
- 2026-06-01 — Phase 9: Documentation suite + upstream-PR readiness. Added the `docs/` suite (`ARCHITECTURE.md`,
  `API.md`, `DEPLOYMENT.md`, `USER_GUIDE.md`, `docs/README.md` index) + refreshed root `README.md`/`CLAUDE.md`/`plan.md`.
  Docs-only. Branch `feature/roadmap-p9-docs`. Suite **329/6 skipped**. — done
- 2026-06-01 — Phase 8: Packaging — container image + Helm/Kustomize one-command deploy. Hardened non-root,
  read-only-rootfs multi-stage `Dockerfile` (+`.dockerignore`, pinned kubectl); Helm chart + Kustomize base/overlay
  rendering Deployment/Service/ServiceAccount + namespaced least-privilege Role/RoleBinding (the exact kubectl verbs
  RealKubeClient uses — resolves the Phase-3 RBAC deferral); `orchestrate_benchmark_run` threads
  `orchestrator_service_account`; judgment in `knowledge/packaging.md`. Branch `feature/roadmap-p8-packaging`
  (`742e20d`). Suite **315/6 skipped**. — done
- 2026-06-01 — Phase 7: Observability — Prometheus /metrics + instrumentation + live run metrics. Dependency-free
  metrics registry (`app/observability/metrics.py`, Prometheus text format) + instrumentation hooks wired through
  `ToolContext` and the orchestrator; `GET /metrics` scrape endpoint; new read-only `observe_run_metrics` tool reads
  LIVE cluster CPU/mem via `kubectl top` (distinct from the agent's own `/metrics`); ops assets under
  `deploy/observability/`; judgment in `knowledge/observability.md`. Branch `feature/roadmap-p7-observability`. Suite
  **269/6 skipped**. — done
- 2026-06-01 — Phase 6: Configuration Explorer / Capacity Planner pre-flight. Read-only `check_capacity` tool
  (`app/tools/capacity.py`) at the plan gate ("will this fit?" before a ~10-min standup OOMs) — renders the scenario
  over repo defaults + conversation overrides and runs the BENCHMARK REPO's own capacity planner via a vetted
  `scripts/capacity_check.py` bridge (weights + activation + KV-cache vs accelerator memory + HF config lookup);
  feasibility math in `app/capacity/planner.py`, verdict judgment in `knowledge/capacity.md`; `enforce=True` ⇒ ERRORs
  else WARNINGs. Branch `feature/roadmap-p6-capacity`. Suite **245/6 skipped**. — done
- 2026-06-01 — Phase 5: Historical result storage + trends UI. Cross-session history store (`app/storage/history.py`)
  + single `result_history` tool (store/list/get/trend/delete) persisting a validated report's summary, browsing
  newest-first, and reading a time-series for one metric across results; new `knowledge/history.md`; UI results-browser/
  trends view. (Landed last of the stalled-run branches, out of numeric order.) Branch `feature/roadmap-p5-storage`
  (`60d356d`). Suite **297/6 skipped**. — done
- 2026-06-01 — Phase 4: Results Analyzer (goodput, SLO filtering, Pareto/DoE). Read-only `analyze_results` tool
  (`app/tools/analyze.py`) + pure math in `app/validation/analysis.py` (`SLOTargets`, `evaluate_slo`, `pareto_analysis`):
  schema-validates each BR v0.2 report (never scrapes logs), per-run SLO verdict over the full percentile ladder + an
  honest goodput estimate, and for a sweep the Pareto-optimal + SLO-feasible frontier; `SessionPlan` gained optional
  `slo` targets; judgment in new `knowledge/analysis.md`. Branch `feature/roadmap-p4-analyzer`. Suite
  **219/6 skipped**. — done
- 2026-06-01 — Phase 3: Kubernetes-native Benchmark Orchestrator (the 40% centerpiece). `app/orchestrator/`
  (kube.py/job.py/controller.py/faults.py) + `orchestrate_benchmark_run`: RealKubeClient over ToolContext + Fake for
  tests (allowlisted kubectl apply/logs/delete-job with value constraints, workspace-confined `-f`), Job manifest
  (backoffLimit:0, deadline, labels), submit/watch/logs/reconstruct, fault classification (6 kinds) + retry/dead-letter,
  concurrency-capped parallel sweep + cleanup; judgment in `knowledge/orchestrator.md`. 3-agent adversarial review
  fixed real bugs (watch() busy-loop, sweep gather swallowing a raising treatment, classify mapping, retry collapse) +
  hardening (manifest_path `..` ban, DNS-1123 Job name, pod securityContext). In-cluster image + RBAC deferred to
  Phase 8; tested hermetically (FakeKubeClient + CaptureRunner). Branch `feature/roadmap-p3-orchestrator`. Suite
  **190/6 skipped**. — done
- 2026-06-01 — Phase 2: Parallel sessions & parallel benchmark runs. Concurrency cap (`config.max_concurrent_runs`,
  default 2) as a shared `asyncio.Semaphore` wrapping only MUTATING executions (read-only probes uncapped);
  background-safe runs (an in-flight turn detaches on disconnect and finishes server-side, replayed via Phase-1 history;
  a `connected` gate auto-rejects post-disconnect approvals; a 2nd connection's concurrent turn is rejected). Adversarial
  review fixed a real runner hang (`proc.wait()` after the stdout-pump timeout) via `wait_for(gather(pump, wait))` +
  SIGKILL of the process group. Deferred to Phase 3: abandoned runs hold a slot until timeout; reconnecting clients see
  only the end result. Branch `feature/roadmap-p2-parallel`. Suite **127/6 skipped**. — done
- 2026-06-01 — Phase 1: Command transparency, debug mode, UI polish. A `command` event for every executed command
  (centralized in `ToolContext._emit_command`): read-only probes now announce themselves, mutating commands announce
  only after approval; bounded (500) `Session.commands` trail persisted + replayed on resume; UI inline `$ cmd` lines +
  global "Executed commands" log (read-only/mutating + auto/approved badges) + a Debug toggle. Slider audit: no sliders
  exist; deliberately did NOT invent parameter sliders (would embed judgment in the UI). 3-agent adversarial review
  fixed FOUC + added aria-live, flow-level command parity asserts, harness command recording. Branch
  `feature/roadmap-p1-transparency`. Suite **119/6 skipped**. — done
- 2026-06-01 — Phase 0: Autonomous scaffolding. Created integration worktree on `feature/roadmap` off `main`
  (`04c06fe`); fresh `.venv` (uv, py3.11, `-e ".[dev]"`); `.env` carried over with `REPOS_DIR` so app + tests see the
  real sibling repos. conftest portability fix: `BENCH_REPO` resolves via `get_settings().bench_repo` (honors
  `REPOS_DIR`/`.env`) instead of a hardcoded path — converts ~12 sibling-repo tests FAIL→PASS in a worktree, backward-
  compatible. Wrote `ROADMAP.md` (10 phases) + this `PROGRESS.md`. The one extra skip vs the primary checkout is
  `test_snapshot_matches_live` (catalog-drift guard, skips off the canonical sibling path — expected). Suite
  **110/6 skipped**. — done
