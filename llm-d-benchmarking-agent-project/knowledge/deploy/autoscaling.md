# Autoscaling an llm-d stack (Workload Variant Autoscaler / HPA)

Canonical procedure → the upstream **configure-wva-autoscaling-llm-d skill**. Read it with
`fetch_key_docs(task='wva_skill')` (or
`read_repo_doc('llm-d-skills/skills/configure-wva-autoscaling-llm-d/SKILL.md')`) whenever the user
wants llm-d to **scale automatically** under load — autoscaling, HPA/KEDA, the Workload Variant
Autoscaler (WVA), scale-to-N, or cost/latency-tuned scaling.

## What the skill owns (read it, don't restate)
The skill owns the full WVA setup for decode deployments — discovery, the **Low Latency / Balanced /
Cost Optimized** presets, preflight checks, controller deploy, the `inference.optimization/
acceleratorName` label, the `VariantAutoscaling` + HPA/ScaledObject objects, and verification. Its
`docs/REFERENCE.md` (thresholds, env vars, undeploy) + `docs/Troubleshooting.md` carry the detail
(same `fetch_key_docs` task).

## Ground it in our existing knowledge
- The llm-d **workload-autoscaling guide** is already a key-doc — `fetch_key_docs(task=
  'workload_autoscaling')` pairs the guide README with its benchmark spec
  (`--spec guides/workload-autoscaling`). HPA+EPP suits a homogeneous pool; **WVA** is for
  heterogeneous GPU types (proactive, SLO-aware signals — EPP queue depth, in-flight requests, KV
  pressure — not lagging GPU utilization). NOT OpenShift-only.
- GPU selection / node placement / anti-starvation for the benchmark itself stays in
  `read_knowledge('resource_management')`.

## Adapt to OUR tooling (architecture stays authoritative)
- The WVA config + controller deploy (`kubectl apply`, Makefile targets) run via `run_shell`
  (classifier + approval gate), **namespace-scoped**, never cluster-level. Notify before creating
  any resource; mutating → SessionPlan/approval gate (the skill's `ask_followup_question` maps onto
  our gate). Config snapshots / generated scripts go to the **session workspace**, never a repo.
- The **WVA controller repo** (`llm-d-workload-variant-autoscaler`) is NOT one of our three cloned
  read-only repos and is NOT in the dedicated clone tool's command policy — if the user proceeds, clone
  it via `run_shell('git clone …')` (approval-gated; run_shell isn't bound by the clone command policy).
- The payoff is a benchmark: once WVA is configured, **benchmark the autoscaled stack** under
  bursty / elastic load (`welllit_path_advisor.yaml` routes bursty + SLO-aware load here) and read
  whether scaling held the SLO.
