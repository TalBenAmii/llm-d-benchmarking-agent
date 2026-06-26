# Co-author a custom spec & workload with the user (interactive authoring playbook)

Use this when the user wants to **build/create a spec and a workload together** rather than
pick a stock one — e.g. the start-of-chat chip "Build a custom spec & workload with me", or any
"help me design / customize the spec/scenario and the workload" request. This is the ENTRY-POINT
playbook: it frames the interview and ROUTES each piece to the tool + deeper guide that already
exists. It does **not** repeat the knob lists — defer to the linked guides for those.

Two things you are co-authoring, kept distinct:
- **Spec / scenario** — *how the stack is served* (the `--spec`: which guide/baseline, the vLLM
  serve flags, KV-transfer, scheduling, the model). Authored into the session workspace.
- **Workload** — *what load is driven at it* (the harness + profile, its parameters, or a real
  dataset). On the CLI this is `-l <harness> -w <workload>` plus the load-shaping flags.

## 1. Interview first — elicit the shape, don't ask for YAML

The user is (usually) a non-expert; translate intent into knobs for them. Elicit:
- **Use case** → harness + candidate workload (see CORE `usecase_to_profile.yaml`).
- **Workload shape** — prefix-reuse ratio, context length, concurrency / arrival pattern, and the
  SLO they care about (TTFT vs TPOT/ITL vs throughput vs cost). These select both the well-lit-path
  scenario (`read_knowledge('welllit_path_advisor')`) and the load parameters.
- **The model** they want served (if not the spec's scenario default).
- **Deploy path** — CPU-sim quickstart (`cicd/kind`) vs a GPU guide (`read_knowledge('deploy_path_playbook')`).

Propose a concrete starting point from their answers, then let them adjust — co-authoring, not an
empty form. Confirm every spec/harness/workload **name against the LIVE catalog** (`list_catalog`)
before using it; the on-disk catalog is the source of truth, the advisor files are guides.

## 2. Author the SPEC / scenario

- **Start from a well-lit path / guide**, not a blank file. If they want a published llm-d guide as
  the base, `convert_guide_to_scenario` (`read_knowledge('convert_guide')`).
- **Finer per-knob edits** (vLLM serve flags, KV-transfer/events, scheduling/affinity, storage,
  ports) → `write_and_validate_config(artifact_type="scenario", …)`. WHICH knobs and to what is in
  `read_knowledge('vllm_overrides')` — read it before authoring; read the repo's `defaults.yaml` +
  a real scenario example so you don't guess field names. The tool also emits a companion
  **`spec_path`** (`<name>.spec.yaml`) — that's what you feed to `--spec`.
- **A different model than the scenario default** → `read_knowledge('model_override')` (`-m`/`--model`).

## 3. Author the WORKLOAD

`-w`/`--workload` accepts a **stock profile NAME from the catalog only** — there is no
workspace-path route for a hand-authored `-w` file. **That catalog-only constraint is an
AGENT-side `build_argv` choice, NOT a CLI limitation:** upstream `-w`/`--workload` itself accepts a
profile *path* under `workload/profiles/<harness>/` (when the full path is omitted the CLI assumes
the file lives there). The agent narrows that to a stock name for safety; the supported agent route
for *custom* load params stays the run-config round-trip (`-c`, below). So "a custom workload" is
expressed one of these supported ways (pick by what the user is customizing):

- **A different stock profile** that fits the use case → `-w <name>` (confirm via `list_catalog`).
- **Customize the load PARAMETERS** (concurrency, rates, in/out token lengths, num prompts) →
  **run-config round-trip**: `execute_llmdbenchmark(subcommand="run", flags={"generate_config": true})`
  emits the effective run-config under the workspace; edit the knobs; replay with `-c <workspace.yaml>`.
  See `read_knowledge('runconfig_roundtrip')`. (`write_and_validate_config(artifact_type="run_config")`
  authors that workspace YAML when you'd rather build it directly.)
- **Sweep a parameter across runs** (A/B, "how does latency change as concurrency rises") →
  `generate_doe_experiment` + `-e/--experiments` against ONE standup. See `read_knowledge('sweep_playbook')`.
- **Replay a REAL dataset** instead of a synthetic profile → `-x/--dataset`. See
  `read_knowledge('dataset_replay')`.

> **Upstream reference — the run-llm-d-benchmark skill** (`fetch_key_docs(task='benchmark_skill')`)
> is the canonical SHAPE for benchmarking an existing stack: harness choice (inference-perf /
> guidellm / inferencemax / vllm-benchmark), workload selection/generation, and which metrics to
> collect. Consult it for that judgment — but DRIVE the run our way: the `llmdbenchmark` CLI +
> BR-v0.2 parsing stay authoritative, NOT the skill's `run_only.sh` existing-stack entrypoint.

**Quickstart/CPU-sim caveat:** on `cicd/kind` the engine is simulated, so the workload-profile
*choice* is largely cosmetic (stick with `inference-perf` + `sanity_random.yaml` unless the user is
specifically exercising the authoring flow). Honor an explicit custom-workload request either way —
just say plainly that on CPU-sim the numbers are a plumbing sanity check, not real performance.

## 4. GATE everything, then land in a SessionPlan (non-negotiable)

The authored spec/run-config is a **candidate**, never a deployment. Before any mutation:
1. **Dry-run / plan it** — `execute_llmdbenchmark(subcommand="plan"/"run", spec=<spec_path>,
   flags={"dry_run": true})` (and `-c <run_config>` / `-x <dataset>` as authored). A clean render is
   the acceptance gate; if it errors, fix the knob and re-author — don't stand up something that
   didn't pass the plan.
2. **`propose_session_plan`** (catalog-validated, approval-gated) carrying the chosen
   spec/harness/workload/model/SLO, then offer to stand up + run once approved.

## Guardrails
- The repos are **READ-ONLY** — author into the session workspace only, never into `config/scenarios/`.
- Change **one thing at a time** when you want to attribute a result to it (knob → plan → run → compare).
- Don't over-specify: set the minimum that expresses the user's intent; leave the rest to defaults.
- Read repo truth first (`defaults.yaml`, a real scenario/profile example) — don't invent field names.
