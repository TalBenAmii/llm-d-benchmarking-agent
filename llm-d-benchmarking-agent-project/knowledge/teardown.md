# Teardown: removing a deployed llm-d stack

Canonical procedure → the upstream **teardown-llm-d skill**. Read it with
`fetch_key_docs(task='teardown_skill')` (or
`read_repo_doc('llm-d-skills/skills/teardown-llm-d/SKILL.md')`) whenever the user wants to remove,
undeploy, clean up, or free resources from an llm-d deployment — even if they don't say "teardown".

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
