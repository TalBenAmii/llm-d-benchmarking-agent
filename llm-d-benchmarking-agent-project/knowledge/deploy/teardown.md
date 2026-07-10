# Teardown: removing a deployed llm-d stack

Canonical procedure → the upstream **teardown-llm-d skill**. Read it with
`fetch_key_docs(task='teardown_skill')` (or
`read_repo_doc('llm-d-skills/skills/teardown-llm-d/SKILL.md')`) whenever the user wants to remove,
undeploy, clean up, or free resources from an llm-d deployment — even if they don't say "teardown".

## Before ANY deletion: enumerate what THIS session deployed (origin gate)

Never propose a deletion by name-guessing. FIRST enumerate what this session actually created, from
the real mechanisms, and present an explicit **keep vs remove** split for the user to approve:
- **Session state** — the agent's own SessionPlan / deploy record for this session (what standup/run
  this session drove) is the primary source of "we created X".
- **Helm releases** — `helm list -n $NS` (read-only, auto-runs). The CLI's default release is
  `llmdbench` (env `LLMDBENCH_RELEASE`); a release is standup's if its name matches the release or a
  deployed model's `model_id_label`.
- **K8s labels standup applies** (read-only `kubectl get ... -l <selector>` to confirm origin):
  - `stood-up-from=llm-d-benchmark` + `stood-up-via=<modelservice|standalone|fma>` (+ `stood-up-by=<user>`)
    — the strongest "this tool deployed it" marker.
  - `llm-d.ai/inferenceServing=true` on model-serving workloads; `llm-d.ai/guide=<name>` on
    guide/kustomize-deployed resources.
- **Namespaces** — default `llmdbench` (env `LLMDBENCH_NAMESPACE` / `LLMDBENCH_HARNESS_NAMESPACE`,
  model + harness). A namespace that predates the session (or holds other teams' workloads) is NOT
  ours to delete.

**NEVER delete a namespace or workload this session did not create without the user confirming its
origin.** If enumeration can't prove the session created a resource, treat it as keep-by-default and
ask — a shared namespace or a pre-existing stack is exactly what a blind teardown must not wipe.

## How OUR agent tears down (architecture stays authoritative)
Prefer the mechanism that matches HOW the stack came up (see `deploy_path_playbook.md`):
- **Stood up via the `llmdbenchmark` CLI** (the MVP/standup path) → tear down with the CLI's own
  teardown phase: `execute_llmdbenchmark(subcommand="teardown", flags={spec:…, …})` (mutating →
  SessionPlan/approval gate). It removes exactly what standup created. Use `-s`/`--step`
  (`read_knowledge('step_select')`) to restrict to specific teardown steps; note `-d`/`--deep`
  means a DESTRUCTIVE deep teardown (`read_knowledge('harness_debug')`).
- **Run as a K8s-native orchestrated Job** → `manage_orchestrated_runs(action='stop')` deletes the
  Job (`read_knowledge('orchestrator')`); `cancel_run` only stops the in-process turn, it does NOT
  remove the stack (`read_knowledge('run_lifecycle')`).
- **The local kind cluster itself** → the quickstart teardown deletes the kind cluster
  (`read_knowledge('quickstart_playbook')`); never leave a created cluster up after a flow.

## When to follow the skill's helm/kustomize procedure
For a stack deployed by a **published guide** (deploy path 3 — helm + kustomize, not the CLI) or for
**partial cleanup** (e.g. remove only the model server, keep the gateway), the CLI teardown doesn't
apply — follow the **teardown-llm-d skill's** detect → confirm → remove (helm uninstall / `kubectl
delete -k` / label-selector fallback) → verify procedure (`fetch_key_docs(task='teardown_skill')`),
driving each of its commands via `run_shell` (classifier + approval gate). Don't restate its steps
here — read the skill and apply the deltas below (which are NOT in it).

## Non-negotiable deltas vs the skill
- **Namespace-scoped only** — every `helm uninstall` / `kubectl delete` carries `-n $NS`; NEVER
  touch cluster-level resources (ClusterRoles, StorageClasses, CRDs, Nodes). Matches `governance.md`.
- Every delete is **mutating → approval-gated**; read-only inspection (`helm list`, `kubectl get`)
  auto-runs.
- In **compare / A-B** flows, capture run state + vLLM logs BEFORE teardown (irretrievable after) —
  see `sweep_playbook.md` and the compare skill.
