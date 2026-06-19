# Playbook: choosing a deploy path

Two ways an llm-d stack comes into being. For the MVP only the first is supported
end-to-end; the others are described so you can set expectations honestly.

## Completing a deploy flow — no optional mid-flow gates, always finish teardown
When the user gives a complete end-to-end instruction (e.g. "create cluster → standup → smoketest
→ run → teardown"), run it to completion: do NOT pause mid-execution on a NON-mandatory offer
(the metrics-server install is optional — skip it silently if it wasn't requested rather than
ending the turn on that question), and if the instruction included a teardown, the flow is not
done until teardown has run. Never leave a created cluster up after a partial flow without
explicitly telling the user and either tearing down or handing it back. Full rule: quickstart_playbook.md
("Complete a fully-specified run+teardown…") + run_lifecycle.md ("Don't leave a cluster running…").
Garbled / low-confidence intent must be CLARIFIED before any irreversible action (cluster create) —
see quickstart_playbook.md and governance.md; do not deploy off a keyword wall.

## 1. kind + simulated engine (MVP — supported)
`spec=cicd/kind`. Local kind cluster, CPU-only, `llm-d-inference-sim`. No GPU, no model
download, no HF token. This is the quickstart (`quickstart_playbook.md`). Use for
"try it on my laptop", demos, and plumbing/SLO sanity checks.

## 2. Real deployment via llmdbenchmark specs (future)
`spec=examples/gpu` or `spec=guides/<name>` (e.g. `guides/optimized-baseline`). The
benchmark CLI stands up a real stack. Needs GPUs and often a HuggingFace token for
gated models. Confirm specs with `list_catalog` and read the spec/guide with
`read_repo_doc` before promising anything. Do NOT attempt on the local kind node — no
GPUs, not enough CPU/RAM for the default sizes.

### Deploying a published llm-d GUIDE (optimized-baseline as the reference)
`guides/optimized-baseline` is the reference well-lit-path guide — load-aware + prefix-cache-aware
scheduling, the renamed inference-scheduling guide (the benchmark README lists `guides/optimized-baseline`
as "formerly inference-scheduling"). Two ways to
drive it (BOTH go through `execute_llmdbenchmark` / `run_command` — code is mechanism only):
- **Through the benchmark CLI** — `execute_llmdbenchmark subcommand=standup flags={spec:"guides/optimized-baseline", ...}`
  (after `propose_session_plan` + `check_capacity`; standup is mutating → user Approves).
- **As the guide's own manifests** — the `-t kustomize` path with `kustomize.guideName: optimized-baseline`
  (see "Kustomize deploy method" below); applies the guide verbatim via helm+kustomize.

**Client prerequisites — offer `install-deps.sh` when they're MISSING.** A guide deploy needs the
deployment client toolchain (`helm` + helm-diff plugin, `helmfile`, `kustomize`, `yq`, `kubectl`),
reported by `probe_environment` in `tools.*`. When the user wants a guide-based deploy and those
client tools are absent (and `run_setup`/`install.sh` hasn't already supplied them), OFFER the
UPSTREAM guide installer `run_command argv=["install-deps.sh"]` (add `--dev` for chart-testing);
mutating → user Approves. This is the llm-d guide repo's OWN `helpers/client-setup/install-deps.sh`
— DISTINCT from `install_prereqs.sh` (Docker daemon + kind binary) and the benchmark repo's
`install.sh` (framework venv). See `preconditions.md` ("Guide-based deploy: the UPSTREAM client
prerequisites") for which install step to run when, and never re-offer one whose tools are present.

## 3. Hand-run llm-d guides + run_only.sh (not automated here)
The `llm-d` repo `guides/*` deploy via helm+kustomize and then benchmark an EXISTING stack
with `existing_stack/run_only.sh` — a different entry point than `llmdbenchmark`, out of MVP
scope. Mention it exists if the user asks about the published guide numbers.

## How to decide
- "on my laptop / locally / just try it / no GPU" → path 1 (cicd/kind). Default.
- GPU cluster + names a well-lit path → path 2 (future); set expectations that the agent's
  automated support is the kind/sim path for now.
- Always `probe_environment` first: no GPU and on kind → path 1 is the only realistic option.

## Which well-lit path matches the WORKLOAD shape?
Once you know it's a GPU (path 2) deploy, *which* well-lit-path scenario to benchmark lives in
`welllit_path_advisor.yaml` (the Well-lit-path advisor), which maps a workload shape → an llm-d
scenario/guide with the SIGNALS that select it:
- prefix-heavy chat → `guides/precise-prefix-cache-routing`
- long-context RAG / large models → `guides/pd-disaggregation` (P/D)
- high-throughput / batch → `guides/optimized-baseline` (intelligent scheduling baseline)
- agentic / multi-turn → `guides/agentic-tests`
- multimodal (image+text) chat → `guides/multimodal-serving` (profile `guide_multimodal-serving_1.yaml`, inference-perf; advisor-deferred)
- bursty / elastic load, SLO-aware autoscaling → `guides/workload-autoscaling` (profile `guide_workload-autoscaling_1.yaml.in`, guidellm)
- default / local sanity → `cicd/kind` (this playbook's path 1)

The advisor is loaded into your context; consult it (and confirm names with `list_catalog`)
when recommending a scenario. GPU-only entries are DEPLOY-PATH guidance — recommend them, but
benchmark `cicd/kind` for a local sanity pass.

## Kustomize deploy method (`-t kustomize` + the `kustomize.*` block)

`-t kustomize` deploys an upstream **llm-d guide** by applying the guide's own manifests
(`guides/<guideName>` in the llm-d repo) instead of rendering the modelservice/standalone
templates. Ground every choice in `llm-d-benchmark/docs/kustomize.md` (read it via
`read_repo_doc('llm-d-benchmark/docs/kustomize.md')`) and the named guide's own
`guides/<guideName>/README.md` + `guides/<guideName>/modelserver/<backend>/` — valid patch
targets and helm keys are **specific to that guide**, never arbitrary.

### When to choose kustomize (vs modelservice)
- kustomize: benchmark a **published well-lit-path guide exactly as authored** (manifests verbatim),
  or when an upstream guide is the source of truth and you should not re-render it.
- modelservice (default): anything you tune through the scenario (model, replicas, parallelism,
  gateway, vLLM knobs) — under kustomize **all of that is ignored** (the guide's manifests define
  everything); the *only* way to modify a kustomize deploy is the `kustomize.*` keys.
- **Multi-model / multi-stack is NOT supported under kustomize** (keyed on `guideName` with no
  per-stack uniquification — stacks collide). Use modelservice (e.g. `examples/multi-model-wva`)
  for multi-model; keep kustomize scenarios single-stack.

### How to author it (mechanism is `write_and_validate_config`, judgment is yours)
Author the block as a scenario via `write_and_validate_config(artifact_type='scenario', …)`
with DOTTED `kustomize.*` override keys, then GATE it through `plan`/`--dry-run`. The knobs:

- `kustomize.enabled: true` — REQUIRED to deploy (equivalent to `-t kustomize`; false ⇒ inert).
- `kustomize.guideName` — REQUIRED; the `guides/<name>` dir (confirm it exists in the llm-d
  repo). Match it to the workload shape via `welllit_path_advisor.yaml`
  (e.g. `optimized-baseline`, `precise-prefix-cache-routing`, `pd-disaggregation`).
- `kustomize.repoPath` — a LOCAL llm-d clone path. Prefer threading the **same** path as the
  `flags.repo_path` (`--llmd-repo-path`) on the standup so the CLI fallback and the block agree;
  empty ⇒ upstream clones `https://github.com/llm-d/llm-d.git` into `workspace/llm-d`.
- `kustomize.repoRef` — git ref to clone (default `main`); pin it for reproducibility.
- `kustomize.patches` — a LIST of `{patch: <inline strategic-merge YAML>}` against the guide's
  **modelserver** base, matched by apiVersion+kind+metadata.name. Use a patch to change replica
  count (`metadata.name: decode`, `spec.replicas`) or, for a **gated model**, to inject the
  `HF_TOKEN` env from the `llm-d-hf-token` secret (provision it first via `provision_hf_secret`).
  Read the guide's `modelserver/<backend>/` for the real resource names — do not guess.
- `kustomize.overlayPath` — a directory overlay for the modelserver (combinable with `patches`).
- `kustomize.extraHelmValues` / `kustomize.extraHelmSets` — apply ONLY to the **router/GAIE**
  helm release the guide installs (`-f <file>` / `--set k=v`); read the guide README's helm step
  for valid keys.
- `kustomize.guideVariableOverrides` — override/fill the guide README's `${VAR}` tokens; CANNOT
  introduce new variables, and `GUIDE_NAME`/`NAMESPACE`/`GAIE_VERSION` are forced.
- `kustomize.acceleratorBackend` (default `gpu/vllm`), `kustomize.monitoring`,
  `kustomize.deployTimeout`, `kustomize.gaieVersion` — tune as the guide/cluster requires.

### Threading the local clone at standup
When deploying with `-t kustomize`, set `flags.repo_path` on `execute_llmdbenchmark` so the CLI
emits `--llmd-repo-path <path>` (it points the kustomize step at your local llm-d clone — the
fallback for `kustomize.repoPath`). On the kind/CPU MVP path the kustomize guides are GPU
deploys: author + `--dry-run` to validate the block, but benchmark `cicd/kind` for a local
sanity pass and set expectations honestly (no GPU on the kind node).
