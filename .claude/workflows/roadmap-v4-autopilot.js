export const meta = {
  name: 'roadmap-v4-autopilot',
  description: 'Autonomously PLAN then IMPLEMENT Roadmap v4 (the 32 active, non-deferred phases of ROADMAP_V4.md) for the llm-d-benchmarking-agent. Per phase: an architect agent inspects the REAL current code and returns a concrete file-level plan; a fresh-context coder implements it in an isolated git worktree per the plan; three adversarial lenses verify; a serial integrator merges into feature/roadmap-v4 behind a full-suite gate. Waves follow the roadmap PRIORITY ranking (P1->P2->P3), not phase number. The integration branch is cut off docs/roadmap-v4-refresh (so ROADMAP_V4.md travels with the code). main is NEVER touched and is NOT merged at the end (left for human review). Skips the 7 DEFERRED phases + the merged Phase 35. Resumable via resumeFromRunId.',
  whenToUse: 'Plan + implement Roadmap v4 (benchmark feature-coverage + deploy-stack gaps: --monitoring activation, readiness/infra/accelerator/gated pre-flight gates, model/flag overrides, observability trends, EPP-header decoding, etc.) onto an integration branch for review. Run after the roadmap-v4-refresh doc is committed on docs/roadmap-v4-refresh.',
  phases: [
    { title: 'Prep', detail: 'cut feature/roadmap-v4 integration worktree off docs/roadmap-v4-refresh, copy .env, record green baseline, detect already-DONE phases' },
    { title: 'Wave 1', detail: 'P1 standup/readiness: 27 monitoring-activate, 61 harness-cpu-size, 59 model-load-readiness, 62 gated-preflight' },
    { title: 'Wave 2', detail: 'P1: 60 infra-precond, 49 observability-trends (after 27), 45 vllm-overrides, 63 accel-advisor, 28 model-override' },
    { title: 'Wave 3', detail: 'P2: 66 epp-headers, 65 gateway-readiness, 64 provider-pack, 48 session-perf, 30 hf-secret' },
    { title: 'Wave 4', detail: 'P2: 41 dataset-replay, 29 cluster-access, 46 kustomize-block, 53 convert-guide, 36 skip-collect, 31 step-select' },
    { title: 'Wave 5', detail: 'P3: 33 multi-stack, 38 phase-timeouts, 42 runconfig-roundtrip, 39 cloud-sink, 40 analyze-plots, 54 tracing-config' },
    { title: 'Wave 6', detail: 'P3: 37 harness-debug, 32 gateway-class, 55 metric-streaming, 56 stack-discover, 50 results-store, 51 jupyter-plots' },
    { title: 'Verify-suite', detail: 'run the full suite + ruff + mypy on feature/roadmap-v4 and report (NO merge to main)' },
  ],
}

// ----------------------------------------------------------------------------
// Constants (verified against on-disk state 2026-06-03)
// ----------------------------------------------------------------------------
const MONO  = '/home/tal/kind-quickstart-guide'          // main checkout: shares .git, has POPULATED read-only sibling repos llm-d/ + llm-d-benchmark/
const PROJ  = 'llm-d-benchmarking-agent-project'
const INTEG = 'feature/roadmap-v4'                        // integration branch (NEVER main)
const BASE  = 'docs/roadmap-v4-refresh'                   // cut INTEG off the refreshed-roadmap branch so ROADMAP_V4.md travels with the code
const HOME  = '/home/tal/kqg-v4-home'                     // integration worktree — CREATED in Prep
const PDIR  = HOME + '/' + PROJ                           // integration project dir
const VENV  = MONO + '/' + PROJ + '/.venv/bin/python'     // reuse the existing venv for ALL runs

// phase id -> slug. Waves follow the ROADMAP_V4 priority ranking (P1->P2->P3), NOT phase number.
const SLUG  = {
  27:'monitoring-activate', 61:'harness-cpu-size', 59:'model-load-readiness', 62:'gated-preflight',
  60:'infra-precond', 49:'observability-trends', 45:'vllm-overrides', 63:'accel-advisor', 28:'model-override',
  66:'epp-headers', 65:'gateway-readiness', 64:'provider-pack', 48:'session-perf', 30:'hf-secret',
  41:'dataset-replay', 29:'cluster-access', 46:'kustomize-block', 53:'convert-guide', 36:'skip-collect', 31:'step-select',
  33:'multi-stack', 38:'phase-timeouts', 42:'runconfig-roundtrip', 39:'cloud-sink', 40:'analyze-plots', 54:'tracing-config',
  37:'harness-debug', 32:'gateway-class', 55:'metric-streaming', 56:'stack-discover', 50:'results-store', 51:'jupyter-plots',
}
const TITLE = {
  27:'Default-enable benchmark --monitoring + surface results.observability (incl. merged Phase 35)',
  28:'First-class model override (-m/--models)',
  29:'Explicit cluster access (-k/--kubeconfig, URL, token)',
  30:'HuggingFace gated-model secret provisioning',
  31:'First-class step selection / re-run (-s/--step)',
  32:'Gateway class / provider selection (--gateway-class)',
  33:'Multi-stack scenarios + --stack subset + --parallel',
  36:'First-class skip / collect-only mode (-z/--skip)',
  37:'Harness debug mode (-d/--debug, sleep infinity)',
  38:"Model the CLI's per-phase timeouts (--wait/--*-timeout)",
  39:'Cloud results sink for the run flag (-r gs://, s3://)',
  40:"Trigger the CLI's local --analyze plot families",
  41:'Dataset replay URL (-x/--dataset)',
  42:"Round-trip the CLI's run-config (--generate-config / -c)",
  45:'Author per-knob vLLM scenario overrides',
  46:'Kustomize deploy config block (kustomize.*)',
  48:'Parse session_performance metrics (multi-turn)',
  49:'Surface results.observability serving metrics end-to-end (narrowed: trend store)',
  50:'Results Store (git-like result mgmt: remotes/push/pull)',
  51:'Jupyter / standalone plotting scripts surfacing',
  53:'convert-guide (guide -> scenario/experiment file generation)',
  54:'Distributed tracing config (OpenTelemetry tracing: block)',
  55:'Real-time metric streaming / custom Prometheus queries',
  56:'Stack discovery tool (llm-d-discover)',
  59:'Model-load readiness gate (/v1/models vs /health + stuck-pod diagnostics)',
  60:'Infra precondition gate (K8s version + vLLM/NIXL minimums + sidecar gotcha)',
  61:'Right-size harness launcher CPU request for Kind (LLMDBENCH_HARNESS_CPU_NR)',
  62:'Gated-model access pre-flight (check_model_access / GatedStatus)',
  63:'Accelerator + CPU-inferencing precondition advisor',
  64:'Provider-aware precondition pack (oc-vs-kubectl, GPU taints, GMP/known-issues)',
  65:'Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool Accepted/ResolvedRefs)',
  66:'EPP HTTP-header decoder (429s + x-llm-d-request-dropped-reason)',
}
const WAVES = [
  [27, 61, 59, 62],
  [60, 49, 45, 63, 28],
  [66, 65, 64, 48, 30],
  [41, 29, 46, 53, 36, 31],
  [33, 38, 42, 39, 40, 54],
  [37, 32, 55, 56, 50, 51],
]

const wt = (id) => '/home/tal/kqg-v4-p' + id + '-' + SLUG[id]
const br = (id) => 'feature/roadmap-v4-p' + id + '-' + SLUG[id]

// ----------------------------------------------------------------------------
// Schemas
// ----------------------------------------------------------------------------
const STATUS_SCHEMA = { type:'object', additionalProperties:false,
  properties:{ done:{type:'array', items:{type:'integer'}}, base:{type:'string'},
    passCount:{type:'integer'}, skipCount:{type:'integer'}, notes:{type:'string'} }, required:['done'] }

const PLAN_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'},
  approach:{type:'string'},
  filesToEdit:{type:'array', items:{type:'object', additionalProperties:false,
    properties:{ path:{type:'string'}, change:{type:'string'} }, required:['path','change'] }},
  filesToCreate:{type:'array', items:{type:'object', additionalProperties:false,
    properties:{ path:{type:'string'}, purpose:{type:'string'} }, required:['path','purpose'] }},
  knowledgeChanges:{type:'array', items:{type:'string'}},
  allowlistChanges:{type:'string'},
  tests:{type:'array', items:{type:'string'}},
  risks:{type:'string'},
  ready:{type:'boolean'} },
  required:['phase','approach','filesToEdit','tests','ready'] }

const IMPL_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, branch:{type:'string'}, worktree:{type:'string'},
  summary:{type:'string'}, filesChanged:{type:'array', items:{type:'string'}},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  ok:{type:'boolean'}, blocker:{type:'string'} },
  required:['phase','branch','worktree','summary','passCount','failCount','ok'] }

const VERDICT_SCHEMA = { type:'object', additionalProperties:false, properties:{
  lens:{type:'string'}, acceptable:{type:'boolean'},
  blocking:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['lens','acceptable','blocking'] }

const INTEG_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, merged:{type:'boolean'}, fullSuitePassed:{type:'boolean'},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  timedOut:{type:'boolean'}, notes:{type:'string'} },
  required:['phase','merged','fullSuitePassed','passCount','failCount'] }

const HEALTH_SCHEMA = { type:'object', additionalProperties:false, properties:{
  suitePassed:{type:'boolean'}, lintPassed:{type:'boolean'}, typePassed:{type:'boolean'},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  headCommit:{type:'string'}, notes:{type:'string'} },
  required:['suitePassed','passCount','failCount'] }

// ----------------------------------------------------------------------------
// Uniform test command: PYTHONPATH="$PWD" so the worktree's app wins over the shared
// venv's editable finder; REPOS_DIR=MONO so sibling-repo-reading tests find the
// POPULATED read-only repos (worktrees get them EMPTY); timeout 600 so a hung test
// returns 124 (treated as failure to keep the suite hermetic) instead of wedging.
// ----------------------------------------------------------------------------
const TESTCMD = (projDir) =>
  'cd ' + projDir + ' && REPOS_DIR=' + MONO + ' PYTHONPATH="$PWD" timeout 600 ' + VENV + ' -m pytest tests/ -q'

const TRAILER = 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>'

// ----------------------------------------------------------------------------
// Per-phase specs (AUTHORITATIVE; transcribed faithfully from ROADMAP_V4.md).
// NO backticks and NO ${ } inside these template literals.
// ----------------------------------------------------------------------------
const SPEC = {
27: `Phase 27 — Default-enable benchmark --monitoring + surface results.observability (carries merged Phase 35).
GOAL: stop shipping empty results.observability. Activate the metrics PRODUCER (--monitoring / metricsScrapeEnabled) so the KV-cache hit rate, schedule delay (queue-depth proxy), GPU utilization, and replica/startup/EPP-log snapshots the report already knows how to parse actually appear — default ON, with a knowledge-driven opt-out for clusters lacking the Prometheus-operator CRDs. Also carries MERGED Phase 35: the standup-level PodMonitor/ServiceMonitor creation + EPP verbosity, with a clean --no-monitoring escape.
BUILD:
- Add a monitoring flag to ExecuteInput.flags (app/tools/schemas.py).
- Emit --monitoring / --no-monitoring in build_argv (app/tools/execute.py) for standup, run, and experiment.
- Widen security/allowlist.yaml (DATA only) to permit --monitoring/--no-monitoring under those subcommands.
- Default ON, with a knowledge-driven opt-out (emit --no-monitoring / monitoring.installPrometheusCrds) for Kind / CRD-less clusters, keyed on a probe_environment CRD check.
- Surface the parsed results.observability metrics in the report/analysis summary; document the procedure in knowledge/observability.md + knowledge/results_interpretation.md. Confirm the standup path specifically wires PodMonitor/ServiceMonitor + EPP verbosity (merged Phase 35); expose monitoring.podmonitor.enabled / monitoring.installPrometheusCrds guidance in knowledge/observability.md.
ACCEPTANCE: a run/standup emits --monitoring (allowlist-approved); when scraping ran, results.observability KV-cache/GPU/queue metrics appear in the summary; the --no-monitoring opt-out works on a CRD-less cluster; a standup creates PodMonitor/ServiceMonitor when monitoring is on and skips cleanly with --no-monitoring; all decision logic lives in knowledge/, not Python.
HERMETIC TEST: assert build_argv emits --monitoring/--no-monitoring per subcommand (standup/run/experiment); assert the allowlist permits exactly those flags; drive a fixture BR v0.2 with a populated results.observability through summarize_report/analyze_results and assert the metrics surface; assert the opt-out path is selected from a probed no-CRD environment.
THIN-CODE: flag emission + allowlist data = mechanism; on/off + CRD opt-out judgment = knowledge. This is the rank-1 headline gap that UNBLOCKS Phase 49.`,

28: `Phase 28 — First-class model override (-m/--models).
GOAL: let the agent select a model per standup rather than only via the chosen spec.
BUILD: add a models field to ExecuteInput -> emit -m/--models in build_argv (app/tools/execute.py); widen security/allowlist.yaml (DATA) for -m; keep model intent consistent with the capacity pre-flight (CheckCapacityInput); guidance in knowledge/.
ACCEPTANCE: a standup can run with an explicit -m model not pinned by the spec; the capacity pre-flight sees the same model; catalog grounding still validates the name.
HERMETIC TEST: build_argv emits -m; allowlist permits it; capacity input mirrors the override.
THIN-CODE: flag/allowlist = mechanism; which-model judgment stays with the agent + catalog.`,

29: `Phase 29 — Explicit cluster access (-k/--kubeconfig, URL, token).
GOAL: target a remote cluster instead of relying only on the ambient kube context.
BUILD: model kubeconfig / cluster.url / cluster.token on ExecuteInput; emit -k/URL where the CLI accepts them; keep tokens BACKEND-ONLY and scrubbed (app/config.py child_env); widen security/allowlist.yaml (DATA); judgment in knowledge/preconditions.md.
ACCEPTANCE: a non-default kubeconfig/URL is threaded into the CLI call; the token NEVER reaches the browser or logs.
HERMETIC TEST: build_argv emits -k/URL; a secret-scrub test asserts the token is absent from emitted env + command events.
THIN-CODE: flag plumbing = mechanism; secrets stay backend-only and scrubbed.`,

30: `Phase 30 — HuggingFace gated-model secret provisioning.
GOAL: make gated-model standups actually WORK, not just the capacity lookup (natural follow-on to the Phase 62 gated pre-flight).
BUILD: surface huggingface.enabled + provision the cluster HF secret as an APPROVAL-GATED mutating step (via allowlisted kubectl); keep the token backend-only; judgment ("when is a gated model in scope") in knowledge/.
ACCEPTANCE: a gated-model plan provisions the HF secret BEFORE standup; the token is never exposed; non-gated flows are unchanged.
HERMETIC TEST: the secret-provision command is approval-gated + allowlisted; a scrub test asserts the token never appears in events.
THIN-CODE: the kubectl secret create = mechanism (approval-gated); when-to-provision = knowledge.`,

31: `Phase 31 — First-class step selection / re-run (-s/--step).
GOAL: promote step selection from the raw extra passthrough to a modeled, advisory flag.
BUILD: add a step field to ExecuteInput.flags -> emit -s/--step (incl. ranges like 3-5, 5,7) in build_argv; widen security/allowlist.yaml (DATA); add step-list guidance to knowledge/ so the agent can re-run a single failed step.
ACCEPTANCE: the agent can re-run a step range as a modeled flag (not via extra).
HERMETIC TEST: build_argv emits -s with a range; allowlist permits it.
THIN-CODE: flag emission + allowlist data = mechanism.`,

32: `Phase 32 — Gateway class / provider selection (--gateway-class).
GOAL: let the agent choose the gateway provider instead of inheriting it from the spec.
BUILD: model gateway.className -> emit --gateway-class in build_argv; widen security/allowlist.yaml (DATA) with the provider enum (istio/agentgateway/gke/epponly); add provider-selection guidance to knowledge/ (when to pick which).
ACCEPTANCE: a standup can override the gateway provider; the choice is grounded in knowledge, not Python branches.
HERMETIC TEST: build_argv emits --gateway-class from the enum; allowlist validates it.
THIN-CODE: enum allowlist data = mechanism; which-provider judgment = knowledge.`,

33: `Phase 33 — Multi-stack scenarios + --stack subset + --parallel.
GOAL: target a subset of stacks and cap per-pool parallelism for multi-stack specs (--parallel is already allowlisted / -j emitted; the --stack NAME[,NAME...] subset remains unmodeled).
BUILD: model --stack NAME[,NAME...] (+ confirm --parallel) -> emit in build_argv; widen security/allowlist.yaml (DATA); add multi-stack run guidance to knowledge/.
ACCEPTANCE: the agent can run/target one stack of a multi-stack spec and cap parallelism.
HERMETIC TEST: build_argv emits --stack/--parallel; allowlist permits them.
THIN-CODE: flag emission + allowlist = mechanism. Low impact on the single-stack Kind MVP — keep changes additive and do not regress the existing -j/--parallel emission.`,

36: `Phase 36 — First-class skip / collect-only mode (-z/--skip).
GOAL: promote collect/analyze-only from extra to a modeled flag.
BUILD: add a skip field to ExecuteInput.flags -> emit -z/--skip in build_argv; widen security/allowlist.yaml (DATA); document the collect-only flow in knowledge/.
ACCEPTANCE: the agent can re-collect/analyze existing results without re-running the load.
HERMETIC TEST: build_argv emits -z; allowlist permits it.
THIN-CODE: flag emission + allowlist = mechanism.`,

37: `Phase 37 — Harness debug mode (-d/--debug, sleep infinity).
GOAL: support the interactive sleep-infinity debug pod within the approval-gated flow.
BUILD: model -d/--debug -> emit in build_argv; widen security/allowlist.yaml (DATA) as an APPROVAL-GATED mutating action; add debug-workflow guidance to knowledge/ (and the boundary: interactive in-pod exec stays a manual, user-driven step — the agent never drives the interactive shell).
ACCEPTANCE: the agent can launch a debug harness pod (approval-gated) and explain how to exec into it, WITHOUT driving the interactive shell itself.
HERMETIC TEST: build_argv emits -d; the debug launch is approval-gated + allowlisted.
THIN-CODE: flag = mechanism; the in-pod boundary = knowledge.`,

38: `Phase 38 — Model the CLI's per-phase timeouts (--wait/--*-timeout).
GOAL: thread the CLI's own per-phase timeout flags instead of relying solely on the runner/orchestrator deadlines (two timeouts are already allowlisted; none are emitted).
BUILD: model -s/--wait, --wait-timeout, --data-access-timeout, --*-deploy-timeout, --pvc-bind-timeout on ExecuteInput.flags -> emit in build_argv; widen security/allowlist.yaml (DATA); reconcile with the existing timeout_s policy + active_deadline_seconds so the two layers do not fight; guidance in knowledge/.
ACCEPTANCE: a slow-deploy scenario can set a longer CLI deploy timeout; the runner deadline still bounds the whole process.
HERMETIC TEST: build_argv emits the timeout flags; allowlist permits them; the runner deadline still applies.
THIN-CODE: flag emission + allowlist = mechanism; do not duplicate or fight the existing deadline layer.`,

39: `Phase 39 — Cloud results sink for the run flag (-r gs://, s3://).
GOAL: let users with a bucket send run results to GCS/S3 instead of local-only (gs/s3 are deliberately not allowlisted today).
BUILD: keep the LOCAL default; widen security/allowlist.yaml (DATA) to permit gs:// / s3:// destinations on -r/--output (guarded, OPT-IN only); add a "do you have a bucket?" elicitation to knowledge/.
ACCEPTANCE: a user can OPT IN to a gs:// / s3:// results sink; the default stays local.
HERMETIC TEST: build_argv emits -r gs://...; the allowlist permits the cloud scheme ONLY when opted in; local stays the default.
THIN-CODE: scheme allowlist data = mechanism; opt-in elicitation = knowledge. Pairs with the DEFERRED Phase 47 (upload internals) — do NOT implement the upload helpers here.`,

40: `Phase 40 — Trigger the CLI's local --analyze plot families.
GOAL: generate the CLI's optional workstation matplotlib plot families (per-request distributions, session-lifecycle, Prometheus time-series) IN ADDITION to the harness PNGs.
BUILD: model --analyze -> emit in build_argv; widen security/allowlist.yaml (DATA); surface the generated plot families alongside the harness charts via the existing artifact endpoint; guidance in knowledge/analysis.md.
ACCEPTANCE: --analyze runs and the extra plot families are surfaced in the UI; the agent's own SLO/goodput/Pareto math is UNCHANGED.
HERMETIC TEST: build_argv emits --analyze; allowlist permits it; the artifact lister surfaces the new PNG families from a fixture results dir.
THIN-CODE: flag + artifact listing = mechanism; the agent's analysis stays intact.`,

41: `Phase 41 — Dataset replay URL (-x/--dataset).
GOAL: support replaying a real dataset instead of only synthetic workload profiles.
BUILD: model -x/--dataset (+ REPLACE_ENV_LLMDBENCH_RUN_DATASET_DIR) -> emit in build_argv; widen security/allowlist.yaml (DATA); add dataset-vs-synthetic guidance to knowledge/.
ACCEPTANCE: the agent can run a dataset-replay workload; synthetic profiles still work.
HERMETIC TEST: build_argv emits -x; allowlist permits it.
THIN-CODE: flag emission + allowlist = mechanism; dataset-vs-synthetic judgment = knowledge.`,

42: `Phase 42 — Round-trip the CLI's run-config (--generate-config / -c).
GOAL: use the CLI's own --generate-config / -c reuse mechanism (in addition to the agent's in-workspace write_and_validate_config).
BUILD: model --generate-config + -c/--config -> emit in build_argv; widen security/allowlist.yaml (DATA); store/reuse the generated config under --workspace; guidance in knowledge/.
ACCEPTANCE: the agent can generate a run-config with the CLI and replay it via -c.
HERMETIC TEST: build_argv emits --generate-config then -c; allowlist permits both.
THIN-CODE: flag emission + allowlist = mechanism; low impact (the agent already authors/validates configs).`,

45: `Phase 45 — Author per-knob vLLM scenario overrides.
GOAL: let the agent author finer vLLM/scheduling/storage scenario edits beyond the parallelism/memory knobs already in capacity + DoE.
BUILD: extend the in-workspace config authoring (app/tools/config_artifact.py) to set vllmCommon.flags.*, servicePort/port, kvTransfer.*, affinity.*, priorityClassName, schedulerName, ephemeralStorage, networkResource; validate via the CLI --dry-run/plan; WHICH knobs to set lives in knowledge/ (the repos stay READ-ONLY — author into the session WORKSPACE, never the spec).
ACCEPTANCE: the agent can produce a validated scenario with custom vLLM/scheduling knobs; the determinism gate (plan/--dry-run) passes.
HERMETIC TEST: an authored config sets the knobs + passes structural validation against the repo's example shape; NO write into the read-only repo.
THIN-CODE: config emission + validation = mechanism; which-knobs judgment = knowledge.`,

46: `Phase 46 — Kustomize deploy config block (kustomize.*).
GOAL: author the kustomize config block (guideName/repoPath/repoRef/patches/overlays/extraHelmValues/guideVariableOverrides), not just select -t kustomize (only the bare method is allowlisted today).
BUILD: extend config authoring to emit the kustomize.* block into the session WORKSPACE; thread --llmd-repo-path; validate via plan/--dry-run; guidance in knowledge/deploy_path_playbook.md.
ACCEPTANCE: the agent can author + validate a kustomize-method scenario with a guide + patches; the determinism gate passes.
HERMETIC TEST: the authored kustomize block validates against the example shape; -t kustomize stays allowlisted.
THIN-CODE: block authoring + validation = mechanism; which-overlay judgment = knowledge.`,

48: `Phase 48 — Parse session_performance metrics (multi-turn).
GOAL: parse and surface the results.session_performance stats block for multi-turn inference-perf workloads (session_rate, session_duration, events/tokens per session) — confirmed zero session_performance references in report.py/analysis.py today.
BUILD: extend app/validation/report.py to MECHANICALLY extract results.session_performance (gracefully None when absent); surface in the summary + analyze_results; field-name discovery as DATA in knowledge/standard_metrics.yaml (thin code / thick agent); NEVER fabricate.
ACCEPTANCE: a multi-turn BR v0.2 report surfaces session metrics; single-turn reports are unchanged (no fabrication).
HERMETIC TEST: a fixture multi-turn BR v0.2 surfaces session_performance; a single-turn report yields None without error.
THIN-CODE: parsing = mechanism; field-name catalog + interpretation = knowledge. Pairs with the DEFERRED Phase 52.`,

49: `Phase 49 — Surface results.observability serving metrics end-to-end (NARROWED: trend store only).
GOAL: surfacing is largely done; the ONLY remaining slice is adding the 3 standard results.observability metrics (KV-cache hit rate, schedule delay, GPU utilization) to the trend store. Verified 0/8 current trend metrics are standard.
BUILD: add the 3 standard results.observability metrics to app/storage/history.py _TREND_METRICS (keyed to their BR v0.2 dotted paths + correct improve direction); ensure the existing _extract_standard_metric output flows into the report card + analyze_results summary + the history/trend store; keep them INFORMATIONAL Pareto objectives (out of dominance); document interpretation in knowledge/results_interpretation.md.
ACCEPTANCE: with monitoring on (Phase 27), the serving metrics appear in the summary, the report card, AND the trend store; goodput/SLO/Pareto dominance is UNCHANGED.
HERMETIC TEST: a populated results.observability fixture surfaces the metrics across summary/analysis/history; dominance unaffected; assert the 3 new _TREND_METRICS keys exist with sane directions.
THIN-CODE: metric registration = mechanism; interpretation = knowledge. DEPENDS ON Phase 27 (producer) — already integrated in Wave 1 before this wave.`,

50: `Phase 50 — Results Store (git-like result mgmt: remotes/push/pull).
GOAL: optionally interoperate with the CLI's git-like result store (remotes + push/pull to GCS) for teams that share results that way (the agent's own history store already meets the local need).
BUILD: model the results store subcommands (init/remote/status/add/push/ls/pull) -> emit in build_argv; widen security/allowlist.yaml (DATA) as APPROVAL-GATED; bridge to the agent's existing result_history; guidance in knowledge/history.md.
ACCEPTANCE: a user can publish/pull results via the CLI store; the local history store is unchanged.
HERMETIC TEST: build_argv emits the store subcommands; push/pull are approval-gated + allowlisted.
THIN-CODE: subcommand emission + allowlist = mechanism. Keep additive; do NOT regress the local history store.`,

51: `Phase 51 — Jupyter / standalone plotting scripts surfacing.
GOAL: point users at (and optionally drive) the interactive notebook / experimental plotting scripts WITHOUT making them part of the automated flow (the agent already surfaces harness PNGs + does its own analysis).
BUILD: add guidance + the artifact paths to knowledge/analysis.md (the notebook lives in the READ-ONLY repo); optionally allowlist the standalone plot scripts as READ-ONLY generators against a results dir.
ACCEPTANCE: the agent can explain / point at the notebook and (optionally) run a standalone plot script against a results dir.
HERMETIC TEST: if scripted, the plot script is allowlisted READ-ONLY against a fixture results dir.
THIN-CODE: pointers/knowledge first; any script run is read-only mechanism. Minimal footprint.`,

53: `Phase 53 — convert-guide (guide -> scenario/experiment file generation).
GOAL: generate a benchmark scenario/experiment file from an arbitrary llm-d guide.
BUILD: author ai.<name>.sh / ai.<name>.yaml from a guide URL/path INTO the session WORKSPACE (NEVER the read-only repo), using the LLMDBENCH_* mappings as DATA in knowledge/; validate via plan/--dry-run. The upstream convert-guide canonically writes into the read-only benchmark repo; the agent's variant must write to the WORKSPACE only.
ACCEPTANCE: the agent can produce a validated workspace-local scenario from a guide; NO write into the read-only repo.
HERMETIC TEST: the generated scenario validates against the example shape; asserts NO write outside the workspace.
THIN-CODE: mapping table = knowledge (DATA); file emission + validation = mechanism. Hard rule: workspace-only output.`,

54: `Phase 54 — Distributed tracing config (OpenTelemetry tracing: block).
GOAL: let advanced users configure a scenario tracing: block (endpoint, sampling rate, service names) for an external OTel backend.
BUILD: author the tracing: block into the WORKSPACE scenario; validate via plan/--dry-run; guidance in knowledge/observability.md (note: the benchmark CONFIGURES, never COLLECTS, traces — collection is the user's OTel backend).
ACCEPTANCE: the agent can author a validated tracing: block; the limitation (config only) is explained.
HERMETIC TEST: the authored tracing block validates against the example shape.
THIN-CODE: block authoring + validation = mechanism; the config-only limitation = knowledge.`,

55: `Phase 55 — Real-time metric streaming / custom Prometheus queries (track upstream + provide substitute).
GOAL: track the upstream feature (explicitly UNIMPLEMENTED upstream) and provide the agent's best-available equivalent.
BUILD: document in knowledge/observability.md that benchmark metric streaming is upstream-unimplemented; point at the agent's live coverage (observe_run_metrics via kubectl top + real-time pod log streaming) as the substitute.
ACCEPTANCE: the agent answers "can you stream live benchmark metrics?" HONESTLY and offers kubectl top + log streaming instead.
HERMETIC TEST: a knowledge assertion that the feature is upstream-unimplemented + the substitute is named (assert the knowledge file contains the substitute references).
THIN-CODE: knowledge-only addition; no app behavior change.`,

56: `Phase 56 — Stack discovery tool (llm-d-discover).
GOAL: optionally invoke the standalone stack-discovery tool (URL -> live stack config, BR-v0.2 output) for richer environment capture than the agent's endpoint probing.
BUILD: model the llm-d-discover <url> --output-format benchmark-report invocation -> allowlist (DATA) as READ-ONLY (it has its own env-var redaction + read-only RBAC); feed its BR-v0.2 output into the report path; guidance in knowledge/.
ACCEPTANCE: the agent can run discovery and consume its BR-v0.2 output; endpoint probing still works as the default.
HERMETIC TEST: the discovery invocation is allowlisted READ-ONLY; a fixture BR-v0.2 output flows through the report path.
THIN-CODE: invocation + report ingestion = mechanism. The agent's existing probing stays the default.`,

59: `Phase 59 — Model-load readiness gate: poll /v1/models vs /health with stuck-pod load-timing diagnostics.
Source: docs/readiness-probes.md. Today the Phase 24 gate only confirms pod/endpoint PRESENCE, so a Running-but-NotReady model server still loading weights is indistinguishable from a wedged one — this adds true serving-readiness classification before a benchmark is launched.
GOAL: stop benchmarking against a server that is not actually serving yet. Extend the endpoint-readiness gate from "pod exists / endpoint present" to TRUE model-serving readiness, and tell the user WHY a Running pod is still NotReady: classify "still loading weights (legitimate — keep waiting)" vs "wedged/broken (stop waiting)" by distinguishing GET /v1/models (serving-ready, startup/readiness probe) from GET /health (process-alive, liveness probe) and reading pod readiness conditions by role-port (8000 prefill / 8200 decode).
BUILD: mechanism in Python — extend the readiness analyzer (the app/tools/ endpoint-readiness path + the Phase 24 EndpointReadiness struct in app/validation/) to parse the already-allowlisted kubectl get pods/endpoints -o json for pod readiness conditions / restartCount / age, and add a TIGHTLY-CONSTRAINED curl entry to security/allowlist.yaml as READ-ONLY DATA — value_constraints restrict it to GET against an in-namespace service URL on the model-server ports (8000/8200) and to the path ENUM {/v1/models, /health} ONLY (no other host/port/path/verb). Fold both signals into a new field on the EndpointReadiness verdict (e.g. serving_readiness). All JUDGMENT — what "stuck loading" vs "broken" means, how long failureThreshold*periodSeconds legitimately permits (the doc's failureThreshold: 60 startup budget), and that /health passes but /v1/models 503s => weights still loading — lives in a new knowledge/readiness_probes.md. No if/elif decision logic in Python; repos stay read-only; any captured probe output is written into the session WORKSPACE only.
ACCEPTANCE: when a model-server pod is Running but NotReady, the agent reports the loading-vs-broken verdict (and the recommended wait/stop action) BEFORE any benchmark is submitted; the verdict is driven by knowledge/readiness_probes.md, not Python branches; the curl probe is permitted ONLY for GET on ports 8000/8200 at /v1/models or /health against an in-namespace svc URL, and is rejected for any other verb/port/path/host.
HERMETIC TEST: feed canned kubectl get pods JSON fixtures (Running+NotReady with low restartCount & young age vs high restartCount/crash-looping) plus canned curl bodies (200 with a model list, 503, connection-refused) into the analyzer and assert: /health 200 + /v1/models 503 => "still loading weights"; /health refused or high restartCount => "wedged/broken"; both 200 => serving-ready. Assert the allowlist validator permits curl -X GET <svc>:8000/v1/models and :8200/health but REJECTS a POST, an off-enum path (e.g. /v1/completions), and a non-svc host. No GPU, no live cluster, no real benchmark.
THIN-CODE: probe parsing + constrained curl allowlist = mechanism; loading-vs-broken judgment = knowledge.`,

60: `Phase 60 — Infra precondition gate: K8s server version + vLLM/NIXL image minimums + sidecar gotcha.
Source: docs/infrastructure.md. Turns an opaque Init:0/1 stall minutes into a long real-cluster standup into an honest up-front go/no-go.
GOAL: give an honest go/no-go BEFORE a long real-cluster standup. On K8s 1.27 the user hears "the sidecar-based P/D guide will get stuck in Init:0/1 — upgrade to 1.33+ or pick a non-sidecar path" (and that vLLM <0.10.0 / NIXL <0.5.0 / UCX <0.19.0 tags are below tested minimums) instead of a baffling failure after the standup burned real time.
BUILD: mechanism in Python (FACTS only) — extend probe.probe_environment (app/tools/probe.py) with a read-only kubectl version -o json (already allowlisted as kubectl version read_only; widen the version subcommand's --output value-ref DATA only if json is not already permitted) parsed into cluster_info.server_version {major, minor}, plus parse the vLLM/NIXL/UCX/NVSHMEM image tags out of the rendered spec; surface both as plain facts on the probe schema (app/tools/schemas.py). NO version-comparison if/elif in Python — the thresholds (K8s >=1.29, 1.33+ recommended for sidecars, the Init:0/1 gotcha on <=1.28, vLLM 0.10.0+ / NIXL 0.5.0+ / UCX 0.19.0+ / NVSHMEM 3.3.9+) and the which-combo-can-run + when-to-warn rules live as DATA in a new knowledge/infrastructure_preconditions.yaml (with prose tie-in from knowledge/preconditions.md); the LLM reasons over that table. Repos stay read-only — nothing is written into llm-d/; any captured probe artifact is authored into the session WORKSPACE only.
ACCEPTANCE: before a real-cluster standup the agent reports the probed K8s server major.minor and the spec's image tags, and (reasoning from knowledge/infrastructure_preconditions.yaml, not Python) issues the right verdict: on 1.27 -> "sidecar P/D won't init, upgrade to 1.33+ or pick a non-sidecar path"; on 1.29 -> "runs, but 1.33+ recommended for full sidecar support"; on 1.33 -> green; below-minimum vLLM/NIXL/UCX tags are flagged. The decision logic lives entirely in knowledge/, not app/.
HERMETIC TEST: feed canned kubectl version -o json for 1.27 / 1.29 / 1.33 plus canned rendered image tags through probe_environment (fake runner, no live cluster) and assert the extracted facts (cluster_info.server_version major.minor + the parsed vLLM/NIXL/UCX tags); assert knowledge/infrastructure_preconditions.yaml lists the thresholds (K8s 1.29 / 1.33 / 1.28-sidecar-gotcha, vLLM 0.10.0, NIXL 0.5.0, UCX 0.19.0); assert the allowlist still permits kubectl version -o json read-only. No GPU, no live cluster, no real benchmark run.
THIN-CODE: fact extraction = mechanism; version thresholds + verdicts = knowledge DATA.`,

61: `Phase 61 — Right-size the harness launcher CPU request for small/Kind clusters (LLMDBENCH_HARNESS_CPU_NR).
Source: docs/resource_requirements.md. Turns a silent FailedScheduling/Pending launcher pod into a scheduled, successful run on the MVP Kind path.
GOAL: stop the benchmark launcher pod from sitting in opaque FailedScheduling/Pending on a single-node Kind cluster: when the probed node cannot satisfy the harness default (LLMDBENCH_HARNESS_CPU_NR=16), the agent lowers the launcher's CPU request to what the node can actually schedule, so the MVP Kind run proceeds instead of silently hanging.
BUILD: plumb a BACKEND-ONLY env var LLMDBENCH_HARNESS_CPU_NR through app/config.py child_env into the llmdbenchmark subprocess (never surfaced to the browser, never an allowlist flag — it is an env var, not a flag/executable, so NO security/allowlist.yaml change); extend probe_environment (app/tools/probe.py) to report the node's allocatable CPU. The JUDGMENT — whether to lower it, and to what value given probed node CPU and the chosen harness (inference-perf's multi-process launcher needs more headroom than vllm-benchmark's single-process one) — lives in a new knowledge/harness_sizing.md, NEVER as if/elif in Python. Repos stay read-only; any sizing artifact is authored into the session WORKSPACE only.
ACCEPTANCE: on a small-node fixture the launcher subprocess child_env carries a lowered LLMDBENCH_HARNESS_CPU_NR (sourced from knowledge/harness_sizing.md, not Python branches) and the run schedules instead of going Pending; on a large-node fixture the var is absent/default (16); the value never reaches the browser; the harness-aware (inference-perf vs vllm-benchmark) distinction is documented in knowledge/.
HERMETIC TEST: with a fixture probe reporting small node CPU, assert the emitted child_env carries the chosen LLMDBENCH_HARNESS_CPU_NR; with a large-node fixture, assert it is absent (default 16); assert it never appears in the browser-facing scrubbed env. No GPU / live cluster / real benchmark run.
THIN-CODE: env plumbing + node-CPU probe = mechanism; sizing judgment = knowledge.`,

62: `Phase 62 — Gated-model access pre-flight (check_model_access / GatedStatus before standup).
Source: llmdbenchmark/utilities/README.md. Pairs the capacity "will it fit?" pre-flight with a "can you even get the weights?" pre-flight.
GOAL: before a long standup, tell a non-expert the exact gated-model verdict up front — PUBLIC (no token needed), GATED + AUTHORIZED (your token can pull it, proceed), or GATED + UNAUTHORIZED (your token can't pull it, here's the fix) — instead of letting an opaque image-pull/weights failure surface minutes into the deploy.
BUILD: extend the already-allowlisted read-only capacity bridge scripts/capacity_check.py (driven by app/capacity/planner.py, run with the benchmark repo's venv Python) to ALSO call the repo's own llmdbenchmark.utilities.huggingface.check_model_access() / GatedStatus and return a structured {gated, authorized, reason} alongside the capacity verdict — NEVER reimplementing the gating check. Add the gated/authorized/reason fields to the capacity result model (app/capacity/ schema / app/tools/schemas.py CheckCapacityInput/output) so the agent sees them. The HF_TOKEN is read from BACKEND env only, passed to the bridge through the already-scrubbed child env (app/config.py child_env) and NEVER echoed into the structured result, events, or logs. NO allowlist change — this reuses the already-allowlisted read-only capacity_check.py project-script (one .json-path argument), so security/allowlist.yaml is untouched. JUDGMENT — what to say for each GatedStatus, and whether to offer Phase 30 secret-provisioning next when gated+unauthorized — lives in knowledge/capacity.md (which already notes HF_TOKEN for gated lookups), never in Python if/elif. Repos stay read-only; any artifact (the JSON request) is authored into the session WORKSPACE only.
ACCEPTANCE: running check_capacity on a gated model surfaces {gated, authorized, reason} at the plan gate before any mutating step; a gated+unauthorized verdict prompts the knowledge-driven "your HF token can't pull this model" explanation (and the option to provision the secret via Phase 30), gated+authorized says "proceed", and public needs no token — all three decisions are read from knowledge/capacity.md, not Python branches; the token never appears in the result or events.
HERMETIC TEST: drive the bridge/planner with a fixture ModelAccessResult for each GatedStatus (public/NOT_GATED, gated+authorized, gated+denied) and assert the structured {gated, authorized, reason} verdict is produced; assert the secret-scrub test finds no HF_TOKEN value in the structured result or emitted command events; no live HuggingFace call, no GPU, no live cluster, no real standup.
THIN-CODE: reuse the repo's gating check (mechanism, no reimplementation); per-status messaging = knowledge.`,

63: `Phase 63 — Accelerator + CPU-inferencing precondition advisor.
Source: docs/accelerators/README.md. Pairs node-advertised-resource detection with check_capacity's GPU-memory sizing so the agent can answer "can my hardware run this?" grounded in upstream truth.
GOAL: let the agent answer "can my hardware actually run this?" before a standup — detect whether a node advertises nvidia.com/gpu (or amd/gaudi/tpu/xpu siblings) versus CPU-only, and warn that a REAL (non-sim) CPU-only replica needs 64 cores + 64GB RAM, complementing the GPU-memory sizing check_capacity already does.
BUILD: add a read-only advise_accelerators probe (app/tools/probe.py, registered in app/tools/registry.py) that runs the ALREADY-ALLOWLISTED kubectl get nodes -o json and mechanically extracts per-node status.capacity/status.allocatable (cpu, memory, and the nvidia.com/gpu + sibling extended-resource keys); add its AdviseAcceleratorsInput/output to app/tools/schemas.py. No new mutating command and no allowlist widening (reuses the existing kubectl get nodes entry — confirm it is present, else add it as read-only DATA in security/allowlist.yaml). All JUDGMENT — the CUDA 12.9.1 / driver 575.x (min 525.60.13, < 580) and CUDA 13.0.2 / driver 580.65.06 minimums, Device-Plugin vs DRA selection, the CPU-only 64c/64GB-per-replica floor, and confirmation that the Kind/CPU SIM path is supported and exempt from the floor — lives as pure DATA in a new knowledge/accelerators.yaml that the LLM reasons over (plus a pointer from knowledge/preconditions.md + knowledge/capacity.md). NO if/elif feasibility logic in Python. Repos stay read-only; any generated artifact is authored into the session WORKSPACE only.
ACCEPTANCE: on a GPU-advertised node the agent reports the advertised accelerator resource + its DRA-vs-device-plugin/CUDA-driver advice; on a CPU-only node it warns that a real (non-sim) replica needs 64c/64GB and pairs the warning with check_capacity's GPU-memory sizing; on Kind/CPU sim it confirms the path is supported (floor exempt). Every feasibility judgment is sourced from knowledge/accelerators.yaml, not Python branches.
HERMETIC TEST: feed canned kubectl get nodes -o json fixtures (one node advertising nvidia.com/gpu, one CPU-only) through the probe with the fake runner and assert the extracted advertised-resource facts (cpu/memory/gpu keys) per node; assert knowledge/accelerators.yaml loads and carries the 64c/64GB-per-replica CPU floor, the CUDA/driver minimums, and the DRA-vs-device-plugin distinction. No GPU, no live cluster, no real benchmark.
THIN-CODE: node-resource extraction = mechanism; feasibility thresholds = knowledge DATA.`,

64: `Phase 64 — Provider-aware precondition pack (oc-vs-kubectl, GPU taints/tolerations, GMP/known-issues).
Source: docs/infra-providers/openshift/README.md. Makes the agent's commands and unstick-advice fit OpenShift / DOKS / GKE / AKS, not just kind.
GOAL: adapt to the user's detected cloud provider so its commands work and it can unstick the common Pending / PROGRAMMED=False failures: on OpenShift use oc (not kubectl), avoid ServiceMesh/Istio gateway conflicts, and apply the L40S taint tolerations that unstick Pending model-server pods; on DOKS/GKE/AKS apply the nvidia.com/gpu toleration and flag the GKE Google-Managed-Prometheus, "Undetected platform", and NVSHMEM known issues — all as ADVICE the user can approve, never silent mutation.
BUILD: add a read-only PROVIDER-DETECTION probe to app/tools/probe.py (reuse the already-allowlisted kubectl get nodes -o json to read node labels/taints + cluster facts; emit provider/gpu_taints detection FACTS, no decision branches). Widen security/allowlist.yaml (DATA only) to add an oc: tool entry carrying the SAME constrained read-only subcommands: set as kubectl (a kubectl-equivalent entry referencing the shared kubectl_resource/output_format/namespace/label_selector refs — DATA, no Python oc-vs-kubectl branching). The per-provider playbook — which CLI, which taint/toleration to author, which known issue (GMP / "Undetected platform" / NVSHMEM) applies — is pure JUDGMENT in a new knowledge/infra_providers.yaml the LLM reasons over (cross-linked from knowledge/preconditions.md). Any toleration/patch the agent proposes is authored INTO the session WORKSPACE and applied only as an approval-gated mutating step; the sibling repos stay read-only.
ACCEPTANCE: given canned OpenShift node labels the agent prefers oc and surfaces the ServiceMesh/L40S-taint guidance; given GKE/DOKS labels it surfaces the nvidia.com/gpu toleration plus the GMP / "Undetected platform" / NVSHMEM known-issue notes and an approval-gated toleration patch — with every which-CLI / which-toleration / which-known-issue decision sourced from knowledge/infra_providers.yaml, not Python if/elif.
HERMETIC TEST: feed canned node-label JSON fixtures for openshift / gke / doks to the detection probe and assert the emitted provider/taint facts; assert in tests/test_allowlist.py that oc validates against the SAME read-only constraints as kubectl (the equivalent read-only subcommands accepted, mutating/unknown subcommands denied). No GPU, no live cluster, no real benchmark run.
THIN-CODE: provider/taint detection + oc allowlist entry = mechanism (DATA); the per-provider playbook = knowledge.`,

65: `Phase 65 — Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool Accepted/ResolvedRefs).
Source: guides/prereq/gateways/gke.md. Extends the endpoint-readiness gate to the Gateway-API control plane (GKE/Istio/agentgateway).
GOAL: in gateway-mode deploys the agent can distinguish "model pods are Ready" from "traffic can actually reach them" — so it says "the model pods are up, but the Gateway is still PROGRAMMED:False (or the InferencePool isn't ResolvedRefs), so no traffic reaches them yet", a distinct, common not-ready state that today's pod/endpoint check misses.
BUILD: extend the readiness analyzer (app/orchestrator/readiness.py) to also parse kubectl get gateway,gatewayclass,inferencepool,httproute -o json status conditions — Gateway PROGRAMMED, InferencePool status.parents[].conditions (Accepted/ResolvedRefs), and GatewayClass existence — into new fields on the EndpointReadiness verdict (mechanism: it extracts conditions into facts, never branches on them); thread them through the tool layer (app/tools/readiness.py + the verdict schema in app/tools/schemas.py). Widen security/allowlist.yaml (DATA only) by adding gateway/gatewayclass/inferencepool/httproute to the kubectl_resource enum as read-only get -o json data. JUDGMENT — what PROGRAMMED:False / the GKE fault-filter-abort symptom mean, how long each is expected to take, and whether to wait vs. stand up vs. surface a config error — lives in a new knowledge/gateway_readiness.md (no if/elif decision logic in Python; the parser extracts conditions, the agent interprets). Repos stay read-only; the analyzer only reads kubectl output and never writes.
ACCEPTANCE: given Gateway/InferencePool/GatewayClass JSON, the verdict carries PROGRAMMED + Accepted/ResolvedRefs condition facts and the GatewayClass-exists fact; pods-Ready-but-PROGRAMMED:False yields a not-ready verdict with a gateway-specific reason token; a fully-programmed gateway with ResolvedRefs:True yields ready; all wait-vs-standup-vs-error decisions come from knowledge/gateway_readiness.md, not Python.
HERMETIC TEST: feed canned Gateway/InferencePool JSON permutations (PROGRAMMED True/False, ResolvedRefs True/False, GatewayClass present/absent) into the analyzer and assert the verdict ready boolean + reason/condition tokens for each; assert security/allowlist.yaml permits exactly the four new read-only kubectl_resource values under get. No GPU, no live cluster, no real benchmark.
THIN-CODE: condition extraction + allowlist enum = mechanism; wait-vs-standup-vs-error judgment = knowledge.`,

66: `Phase 66 — EPP HTTP-header decoder (interpret 429s + x-llm-d-request-dropped-reason).
Source: docs/api-reference/epp-http-headers.md. Turns opaque "some requests failed" into a grounded admission/eviction read.
GOAL: stop reporting failed requests as "the system was broken." When a benchmark encounters EPP request/response headers, let the agent decode them — the SLO set-headers (x-llm-d-slo-ttft-ms / x-llm-d-slo-tpot-ms / x-llm-d-inference-objective / x-llm-d-inference-fairness-id) and especially the x-llm-d-request-dropped-reason enum (rejected-saturated / evicted-priority) — so a 7%-failure run reads as "rejected at admission capacity, not failing — lower concurrency or scale out" instead of "some requests failed."
BUILD: mostly-DATA. Author knowledge/epp_headers.yaml cataloguing each header name -> meaning and the x-llm-d-request-dropped-reason enum -> plain-language cause (rejected-saturated = at admission capacity, shed before serving; evicted-priority = preempted mid-flight by higher-priority work), plus the deprecated header aliases; wire it into CORE_KNOWLEDGE (app/agent/prompt.py) so it is reachable via read_knowledge (app/tools/probe.py), and point knowledge/results_interpretation.md at it. Optional THIN mechanism only: IF a harness/report surfaces these headers or 429 counts, mechanically attach them to the results summary and let the LLM map them via the YAML — NO decision logic in Python (the rejected-vs-evicted-vs-broken judgment lives entirely in knowledge/). Repos stay read-only; nothing is authored outside the session workspace.
ACCEPTANCE: given a run that hit EPP drops, the agent loads epp_headers and explains the failure fraction as a saturation/eviction signal (capacity, not breakage) with the right remedy — and the SLO set-headers are decoded — with the rejected/evicted/broken classification coming from knowledge/epp_headers.yaml, not a Python if/elif.
HERMETIC TEST: assert knowledge/epp_headers.yaml loads and is reachable via read_knowledge("epp_headers"); assert it documents the x-llm-d-request-dropped-reason enum (both rejected-saturated and evicted-priority) and the four SLO/objective/fairness header names; assert it is listed in CORE_KNOWLEDGE. No GPU, no live cluster, no real benchmark run.
THIN-CODE: knowledge-first; any header attach = thin mechanism; all classification = knowledge.`,
}

// ----------------------------------------------------------------------------
// Prompts
// ----------------------------------------------------------------------------
const PREP_PROMPT = `Prepare a FRESH integration worktree for Roadmap v4 (plan+implement) so phase branches merge cleanly. Use Bash. main is NEVER touched. Do EXACTLY:

1. Sanity: note (do NOT switch) the main checkout's current branch:  git -C ${MONO} rev-parse --abbrev-ref HEAD . Run  git -C ${MONO} status --porcelain  (if unrelated uncommitted work exists, leave it and mention it in notes).

2. Confirm the base branch ${BASE} exists:  git -C ${MONO} rev-parse --verify ${BASE}  (this branch carries the refreshed ROADMAP_V4.md). If it does NOT exist, set base="MISSING" in the result and STOP (return done:[]).

3. Create (or reuse) the integration branch ${INTEG} + worktree ${HOME}:
   - If ${HOME} already exists as a worktree (git -C ${MONO} worktree list shows it): put it on ${INTEG} (git -C ${HOME} checkout ${INTEG}) and report reuse.
   - Else if branch ${INTEG} already exists:  git -C ${MONO} worktree add ${HOME} ${INTEG}
   - Else:  git -C ${MONO} worktree add -b ${INTEG} ${HOME} ${BASE}
   Verify:  git -C ${HOME} rev-parse --abbrev-ref HEAD  ==  ${INTEG}

4. Make the integration worktree runnable + record the GREEN baseline:
   - Copy env if missing:  cp -n ${MONO}/${PROJ}/.env ${HOME}/${PROJ}/.env 2>/dev/null || true
   - Confirm the venv:  ${VENV} --version
   - Confirm the worktree's app wins on the path:  cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"  (MUST print a path under ${PDIR})
   - Run the suite to record the green baseline:  ${TESTCMD(PDIR)}   (record pass/skip counts into passCount/skipCount)
   - Confirm ruff/mypy are configured (grep -q 'tool.ruff' ${PDIR}/pyproject.toml ; ${VENV} -m ruff --version) so integrators can run the lint gate.

5. Confirm ${PDIR}/ROADMAP_V4.md is present (it is the authoritative roadmap; phase status lines are updated by the integrators, not here).

6. Detect resume state: read ${PDIR}/ROADMAP_V4.md and report which of the ACTIVE phases (27,28,29,30,31,32,33,36,37,38,39,40,41,42,45,46,48,49,50,51,53,54,55,56,59,60,61,62,63,64,65,66) are ALREADY marked DONE in their phase heading.

Return {done:[...ids already DONE...], base:"${BASE}", passCount, skipCount, notes:"baseline counts + ruff/mypy availability + anything notable"}.`

function planPrompt(id) {
  return `You are a software ARCHITECT. Produce a concrete, file-level implementation PLAN for EXACTLY ONE roadmap phase — Phase ${id} "${TITLE[id]}" (${SLUG[id]}). You do NOT write code or create worktrees — you READ the real current codebase and return a precise plan a coder will follow.

== Where to read (READ-ONLY) ==
- Integration project dir (on ${INTEG}, reflects all earlier waves already integrated): ${PDIR}
- Conventions/law: ${PDIR}/CLAUDE.md (thin code/thick agent; allowlist-as-data; hermetic tests; secrets; determinism). Skim ${PDIR}/ROADMAP_V4.md for where this phase fits, and ${PDIR}/PROGRESS.md for current state.
- The POPULATED read-only sibling repos (for upstream truth referenced by the spec): ${MONO}/llm-d and ${MONO}/llm-d-benchmark.

== The phase spec (AUTHORITATIVE) ==
${SPEC[id]}

== Your job ==
Inspect the ACTUAL files named in the spec (open them, find the real functions/structs/anchors — e.g. build_argv in app/tools/execute.py, ExecuteInput in app/tools/schemas.py, _TREND_METRICS in app/storage/history.py, probe_environment in app/tools/probe.py, CORE_KNOWLEDGE in app/agent/prompt.py, the allowlist schema in security/allowlist.yaml + tests/test_allowlist.py, the existing readiness path, the knowledge loader). VERIFY the spec's file/function references against reality and CORRECT them if drifted. Then return a plan with:
- approach: 2-5 sentences on how to implement it within this codebase (cite the real anchors you found).
- filesToEdit: each existing file to change + the precise change (function/line-area + what).
- filesToCreate: each new file (esp. knowledge/*.md|yaml) + its purpose.
- knowledgeChanges: the knowledge/*.md|yaml additions (the JUDGMENT must live here, NOT in Python if/elif).
- allowlistChanges: the exact security/allowlist.yaml DATA addition, or "none" if the spec adds no command/flag surface.
- tests: the hermetic pytest cases to add (matching the spec's HERMETIC TEST), naming the test file + the fakes to use (FakeKubeClient, CaptureRunner, the tests/ TestClient harness, fixtures).
- risks: shared-file conflict risk (which other phases touch the same files) + any spec assumption that did not match the code.
- ready: true only if the plan is concrete and the spec is implementable as written (set false + explain in risks if a spec anchor is wrong or the feature is already present).

Keep it tight and factual — a coder will implement strictly from this plan + the spec. Do NOT edit anything.`
}

function implPrompt(id, plan) {
  const planBlock = plan ? JSON.stringify(plan).slice(0, 5000) : '(no plan available — implement from the spec)'
  return `You implement EXACTLY ONE roadmap phase — Phase ${id} "${TITLE[id]}" (${SLUG[id]}) — in your OWN isolated git worktree, then commit on its branch. Other agents may be implementing other phases in parallel in DIFFERENT worktrees — stay strictly inside yours. Favor correctness over speed. main is NEVER touched.

== Repo facts ==
- Main checkout (shares .git; has the POPULATED read-only sibling repos llm-d/ and llm-d-benchmark/): ${MONO}
- Integration branch (never merge anywhere yourself): ${INTEG}
- Project dir name inside any worktree: ${PROJ}
- Conventions/law: read ${PDIR}/CLAUDE.md and ${PDIR}/PROGRESS.md (thin code/thick agent; allowlist-as-data; hermetic tests; secrets; determinism; the current test baseline). Skim ${PDIR}/ROADMAP_V4.md for where your phase fits.

== The phase spec (AUTHORITATIVE) ==
${SPEC[id]}

== The architect's PLAN (follow it; correct it only if it is wrong against the real code) ==
${planBlock}

== Step 1: create YOUR worktree off the integration branch ==
If ${wt(id)} already exists from a prior run:  git -C ${MONO} worktree remove --force ${wt(id)} ; git -C ${MONO} branch -D ${br(id)}  (ignore errors). Then:
  git -C ${MONO} worktree add -b ${br(id)} ${wt(id)} ${INTEG}
Verify:  git -C ${wt(id)} rev-parse --abbrev-ref HEAD  ==  ${br(id)} . Do ALL edits inside ${wt(id)}/${PROJ} ONLY.

== Step 2: implement per the plan + spec. Hard rules ==
- THIN CODE, THICK AGENT: NO decision logic in Python if/elif; mechanism in Python, judgment in knowledge/*.md|yaml.
- SECURITY: the allowlist is DATA (security/allowlist.yaml) — widen via YAML, never per-command Python. Commands are argv lists, shell=False. Read-only auto-runs; mutating needs approval.
- Read repo truth at runtime; NEVER edit the sibling repos llm-d/ or llm-d-benchmark/. Author any generated artifact into the session WORKSPACE only.
- SECRETS (HF_TOKEN, kube tokens) stay BACKEND-ONLY and scrubbed; the browser/events/logs never see them.
- NO new REQUIRED runtime dependency.
- Tests: add/extend pytest under tests/ that MEANINGFULLY cover the feature (no vacuous asserts, no skip-to-pass, no xfail). HERMETIC ONLY — no live cluster, no GPU, no network, no long real runs; use the existing fakes (FakeKubeClient, CaptureRunner, the tests/ TestClient harness, fake clocks, JSON fixtures).
- DO NOT edit ROADMAP_V4.md, PROGRESS.md, or docs/BENCHMARK_FEATURE_COVERAGE.md (the integrator owns those). You MAY add knowledge/*.md|yaml and docs.
- Keep your changes ruff- and mypy-clean (both are configured in pyproject).

== Step 3: run the suite from YOUR worktree ==
First confirm your app wins on the path:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${wt(id)})
Then:
  ${TESTCMD(wt(id) + '/' + PROJ)}
ZERO failures, and your NEW tests must pass. Iterate until green. (timeout 600: exit 124 = a hung test reached a real resource — make it hermetic, do NOT skip.) Optionally run  cd ${wt(id)}/${PROJ} && ${VENV} -m ruff check . and ${VENV} -m mypy app  and fix what you touched.

== Step 4: commit (do NOT push, do NOT merge) ==
git -C ${wt(id)} add -A
git -C ${wt(id)} commit -m "<clear scoped Phase ${id} message>" -m "${TRAILER}"

Return the structured result. ok=true ONLY if the suite is green (failCount=0) AND the spec's ACCEPTANCE is genuinely met. summary <= 6 lines. branch=${br(id)}, worktree=${wt(id)}.`
}

function fixPrompt(id, feedback) {
  return `Phase ${id} ("${SLUG[id]}" — ${TITLE[id]}) FAILED review. Fix it IN PLACE in the existing worktree ${wt(id)} on branch ${br(id)} — do not create a new worktree.

Reviewer feedback (address EVERY blocking item):
${feedback}

Re-read the spec if needed:
${SPEC[id]}

Keep the hard rules (thin code/thick agent; allowlist-as-data; hermetic tests only; no new required runtime dep; secrets backend-only; do NOT edit ROADMAP_V4.md/PROGRESS.md/docs/BENCHMARK_FEATURE_COVERAGE.md; keep ruff/mypy clean). Then re-run, asserting your app wins on the path:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (under ${wt(id)})
  ${TESTCMD(wt(id) + '/' + PROJ)}
It must be green (failCount=0). Amend or add a commit on ${br(id)} ending with:
${TRAILER}
Return the structured result with ok=true only if green and the blocking items are resolved.`
}

function verifyPrompt(id, lens) {
  const head = `Adversarially review Phase ${id} ("${SLUG[id]}" — ${TITLE[id]}) implemented on branch ${br(id)} in worktree ${wt(id)}. Be skeptical; default to acceptable=false if unsure. You are ONE of three independent lenses. Read-only review (re-running pytest is allowed). Inspect the diff:  git -C ${wt(id)} diff ${INTEG}...HEAD

The AUTHORITATIVE phase spec:
${SPEC[id]}

`
  const lenses = {
    'acceptance':
      head + `LENS = ACCEPTANCE. Does the implementation ACTUALLY deliver the spec's BUILD + ACCEPTANCE (the real feature, not a stub)? Are the named files/functions/knowledge actually changed as the spec requires? Coherent and complete? List anything missing/incorrect as blocking.`,
    'tests-real':
      head + `LENS = TESTS-ARE-REAL. RE-RUN the suite yourself. First assert app origin:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (must be under ${wt(id)})
  ${TESTCMD(wt(id) + '/' + PROJ)}
Confirm GREEN (0 failures, NOT a 124 timeout) and the NEW tests genuinely exercise the feature (meaningful assertions, not skipped/vacuous/tautological, matching the spec's HERMETIC TEST). If red, vacuous, or fake coverage, list it as blocking with the weak/failing test names.`,
    'philosophy-security':
      head + `LENS = PHILOSOPHY+SECURITY. Enforce: (a) thin code/thick agent — NO decision logic in Python if/elif; the JUDGMENT lives in knowledge/*.md|yaml; (b) the allowlist stays DATA in security/allowlist.yaml, widened via YAML not Python, commands argv-only shell=False, mutating actions approval-gated, any new read-only command (e.g. the constrained curl in P59, the oc entry in P64) is tightly value-constrained; (c) NO new REQUIRED runtime dependency; (d) NO edits to sibling repos llm-d/ or llm-d-benchmark/, and any generated artifact is workspace-only; (e) SECRETS (HF_TOKEN/kube token) stay backend-only + scrubbed (never in result/events/logs); (f) tests hermetic (no live cluster/GPU/network/long runs); (g) did NOT edit ROADMAP_V4.md, PROGRESS.md, or docs/BENCHMARK_FEATURE_COVERAGE.md. List every violation as blocking.`,
  }
  return lenses[lens]
}

function integratePrompt(id) {
  const slug = SLUG[id]
  return `You are the SERIAL integrator (only one runs at a time). Merge the verified Phase ${id} ("${slug}") branch into ${INTEG}, gate on the FULL suite, write the state docs, and clean up. Use Bash. main is NEVER touched and is NOT merged anywhere.

== Context ==
- Integration worktree (on ${INTEG}; runnable): ${HOME}   (project dir ${PDIR})
- Phase branch to merge: ${br(id)}   (implemented in worktree ${wt(id)})
- Shared venv python: ${VENV}

== Steps ==
1. Ensure the integration worktree is on ${INTEG} and clean:
   git -C ${HOME} rev-parse --abbrev-ref HEAD   (if not ${INTEG}:  git -C ${HOME} checkout ${INTEG})
   git -C ${HOME} status --porcelain   (must be clean; commit/stash stray WIP sensibly first)

2. Merge (no fast-forward):
   git -C ${HOME} merge --no-ff ${br(id)} -m "Merge Phase ${id} (${slug}) into ${INTEG}"
   CONFLICT POLICY:
   - ADDITIVE-REGISTRATION files (app/tools/registry.py, app/tools/schemas.py, app/agent/prompt.py, security/allowlist.yaml, knowledge index/loader, knowledge/*.yaml catalogs): KEEP BOTH SIDES' entries — never drop an existing tool/field/flag/policy/knowledge line.
   - STRUCTURAL-WIRING files (app/tools/execute.py build_argv, app/tools/probe.py, app/validation/report.py, app/validation/analysis.py, app/orchestrator/readiness.py, app/tools/readiness.py, app/storage/history.py, app/capacity/planner.py, app/config.py): COMPOSITION-RECONCILE — read both versions and write the deliberate union into ONE coherent function that runs BOTH behaviors; do NOT blind-concatenate duplicate blocks. (Many phases extend build_argv / probe_environment / the readiness verdict — reconcile, don't clobber.)
   Then:  git -C ${HOME} add <files> && git -C ${HOME} commit --no-edit

3. AUTHORITATIVE full suite — from the integration worktree, TIMEOUT-bounded, worktree app on the path:
   cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${PDIR})
   ${TESTCMD(PDIR)}
   Exit 124 = the suite HUNG — make the offending test hermetic; do NOT skip/xfail to hide it (set timedOut=true, merged=false if you cannot). Require 0 failures and a pass count >= the prior baseline. If red, FIX the real integration problem (resolve the merge composition) — do NOT delete/weaken tests.
3b. LINT/TYPE GATE: run  cd ${PDIR} && ${VENV} -m ruff check .  and  ${VENV} -m mypy app  — both must pass (ruff check --fix . is OK for trivial import-sort/format, then re-check). If they cannot pass cleanly, set merged=false and leave the branch for review.

4. Update state docs ON ${INTEG} (you are the ONLY writer — no conflicts):
   - ${PDIR}/ROADMAP_V4.md: change the Phase ${id} heading status from "— TODO" to "— DONE" and append a 2-4 line result note (what shipped + new pass/skip counts).
   - ${PDIR}/docs/BENCHMARK_FEATURE_COVERAGE.md: flip the matching coverage row's emoji to closed/done if straightforward.
   - ${PDIR}/PROGRESS.md: append a short Phase ${id} entry (what shipped + counts).
   git -C ${HOME} add ${PROJ}/ROADMAP_V4.md ${PROJ}/PROGRESS.md ${PROJ}/docs/BENCHMARK_FEATURE_COVERAGE.md && git -C ${HOME} commit -m "docs: mark Phase ${id} (${slug}) done" -m "${TRAILER}"

5. Cleanup:
   git -C ${MONO} worktree remove --force ${wt(id)}
   git -C ${MONO} branch -D ${br(id)}
   git -C ${MONO} worktree prune

Return {phase:${id}, merged, fullSuitePassed, passCount, skipCount, failCount, timedOut, notes}. Set merged=false (and leave the branch+worktree intact for review) if you could not land it cleanly.`
}

function healthPrompt(integratedList, skippedList) {
  return `You run the FINAL health gate on the Roadmap v4 integration branch — but you do NOT merge anything to main and you do NOT push. main MUST be left exactly as it is. Use Bash.

== Context ==
- Integration worktree (on ${INTEG}): ${HOME}   (project dir ${PDIR})
- Shared venv python: ${VENV}
- Integrated phases: [${integratedList.join(',')}]   Skipped/left-for-review: [${skippedList.join(',')}]

== Steps (READ + TEST ONLY; do not touch main, do not merge, do not push) ==
1. Confirm the branch + clean tree:
   git -C ${HOME} rev-parse --abbrev-ref HEAD   (must be ${INTEG})
   git -C ${HOME} status --porcelain            (note any stray changes in notes)
   git -C ${HOME} rev-parse --short HEAD         (record as headCommit)
2. Full suite, worktree app on the path:
   cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${PDIR})
   ${TESTCMD(PDIR)}   -> record passCount/skipCount/failCount; suitePassed = (failCount==0 and not a 124 timeout)
3. Lint/type:
   cd ${PDIR} && ${VENV} -m ruff check .   -> lintPassed
   cd ${PDIR} && ${VENV} -m mypy app       -> typePassed
4. Do NOTHING else. Do NOT merge ${INTEG} into main. Do NOT push. Leave the integration worktree in place for human review.

Return {suitePassed, lintPassed, typePassed, passCount, skipCount, failCount, headCommit, notes:"one-line state of ${INTEG} for the reviewer"}.`
}

// ----------------------------------------------------------------------------
// Per-phase pipeline: plan -> implement (per plan) -> 3-lens parallel verify ->
// (bounded fix+reverify) -> readiness. Integration is done separately + SERIALLY by the caller.
// ----------------------------------------------------------------------------
async function preparePhase(id, waveTitle) {
  const Aj = (prompt, label, schema) => agent(prompt, { label, phase: waveTitle, agentType: 'general-purpose', schema })
  try {
    const plan = await Aj(planPrompt(id), 'plan:p' + id, PLAN_SCHEMA)
    if (plan && plan.ready === false) {
      log('Phase ' + id + ': architect flagged the plan NOT ready — ' + (plan.risks || '').slice(0, 200) + ' (implementing from spec anyway)')
    }

    let impl = await Aj(implPrompt(id, plan), 'impl:p' + id, IMPL_SCHEMA)
    if (!impl) return { phase: id, ready: false, reason: 'impl-null', plan }

    const lenses = ['acceptance', 'tests-real', 'philosophy-security']
    let verdicts = (await parallel(lenses.map(L => () =>
      Aj(verifyPrompt(id, L), 'verify:' + L + ':p' + id, VERDICT_SCHEMA)))).filter(Boolean)
    let blocking = verdicts.filter(v => !v.acceptable || (v.blocking && v.blocking.length))

    if (!impl.ok || blocking.length) {
      const feedback = JSON.stringify({ implOk: impl.ok, blocker: impl.blocker, verdicts }).slice(0, 6000)
      const fixed = await Aj(fixPrompt(id, feedback), 'fix:p' + id, IMPL_SCHEMA)
      if (fixed) impl = fixed
      verdicts = (await parallel(lenses.map(L => () =>
        Aj(verifyPrompt(id, L), 'reverify:' + L + ':p' + id, VERDICT_SCHEMA)))).filter(Boolean)
      blocking = verdicts.filter(v => !v.acceptable || (v.blocking && v.blocking.length))
    }

    const ready = !!(impl && impl.ok && !blocking.length)
    return { phase: id, slug: SLUG[id], ready, impl, plan,
      blocking: blocking.map(v => ({ lens: v.lens, blocking: v.blocking })) }
  } catch (e) {
    return { phase: id, ready: false, reason: 'exception: ' + (e && e.message ? e.message : String(e)) }
  }
}

// ----------------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------------
phase('Prep')
const status = await agent(PREP_PROMPT, { label: 'prep', phase: 'Prep', agentType: 'general-purpose', schema: STATUS_SCHEMA })
const done = new Set((status && status.done) || [])
log('Prep complete. Base: ' + ((status && status.base) || '?') + '. Baseline: ' + ((status && status.passCount) || '?') + ' passed / ' + ((status && status.skipCount) || '?') + ' skipped. Already DONE: [' + [...done].join(',') + ']' + (status && status.notes ? ' — ' + status.notes : ''))

if (status && status.base === 'MISSING') {
  log('ABORT: base branch ' + BASE + ' is missing — cannot cut the integration branch. Nothing was changed.')
  return { aborted: true, reason: 'base branch ' + BASE + ' missing' }
}

const integrated = []
const skipped = []
const plans = []

for (let w = 0; w < WAVES.length; w++) {
  const waveTitle = 'Wave ' + (w + 1)
  phase(waveTitle)
  const ids = WAVES[w].filter(id => !done.has(id))
  if (!ids.length) { log(waveTitle + ': all phases already done — skipping'); continue }
  log(waveTitle + ': planning + implementing phases [' + ids.join(',') + '] in parallel isolated worktrees')

  const prepared = (await parallel(ids.map(id => () => preparePhase(id, waveTitle)))).filter(Boolean)
  for (const p of prepared) { if (p.plan) plans.push({ phase: p.phase, plan: p.plan }) }

  for (const p of prepared.sort((a, b) => a.phase - b.phase)) {
    if (!p.ready) {
      skipped.push({ phase: p.phase, reason: p.reason || 'failed-verification', blocking: p.blocking })
      log('SKIP Phase ' + p.phase + ' — not ready (' + (p.reason || 'verification') + '); branch+worktree left for review')
      continue
    }
    let r = null
    try {
      r = await agent(integratePrompt(p.phase), { label: 'integrate:p' + p.phase, phase: waveTitle, agentType: 'general-purpose', schema: INTEG_SCHEMA })
    } catch (e) {
      r = null
    }
    if (r && r.merged && r.fullSuitePassed && r.failCount === 0) {
      integrated.push({ phase: p.phase, pass: r.passCount, skip: r.skipCount })
      log('INTEGRATED Phase ' + p.phase + ' — suite ' + r.passCount + ' passed / ' + r.skipCount + ' skipped')
    } else {
      skipped.push({ phase: p.phase, reason: 'integration-failed', notes: r && r.notes })
      log('SKIP Phase ' + p.phase + ' — integration did not land cleanly; left for review')
    }
  }
}

// ----------------------------------------------------------------------------
// Final health gate on feature/roadmap-v4 — NO merge to main, NO push.
// ----------------------------------------------------------------------------
let health = null
if (integrated.length) {
  phase('Verify-suite')
  log('Verify-suite: running full suite + ruff + mypy on ' + INTEG + ' (NO merge to main)')
  try {
    health = await agent(
      healthPrompt(integrated.map(i => i.phase), skipped.map(s => s.phase)),
      { label: 'verify-suite', phase: 'Verify-suite', agentType: 'general-purpose', schema: HEALTH_SCHEMA },
    )
  } catch (e) {
    health = null
  }
  if (health) {
    log('Verify-suite: ' + INTEG + ' @ ' + (health.headCommit || '?') + ' — suite ' + (health.suitePassed ? 'GREEN' : 'RED') + ' (' + health.passCount + ' passed / ' + health.skipCount + ' skipped / ' + health.failCount + ' failed), ruff ' + (health.lintPassed ? 'ok' : 'FAIL') + ', mypy ' + (health.typePassed ? 'ok' : 'FAIL'))
  }
} else {
  log('Verify-suite skipped: nothing integrated into ' + INTEG + '.')
}

const summary = {
  base: BASE,
  integrationBranch: INTEG,
  integrationWorktree: HOME,
  integrated: integrated.map(i => i.phase),
  skipped,
  finalSuite: integrated.length ? integrated[integrated.length - 1] : null,
  health,
  mergedToMain: false,
  plans,
  note: 'Roadmap v4 was PLANNED + IMPLEMENTED onto ' + INTEG + ' (cut off ' + BASE + '). main was NOT touched and ' + INTEG + ' was NOT merged to main — left for human review per the request. Skipped phases retain their branch+worktree for inspection. Review:  git -C ' + HOME + ' log --oneline ' + BASE + '..' + INTEG,
}
log('Roadmap v4 autopilot finished. Integrated: [' + summary.integrated.join(',') + ']  Skipped: [' + skipped.map(s => s.phase).join(',') + ']  (main untouched, not merged)')
return summary
