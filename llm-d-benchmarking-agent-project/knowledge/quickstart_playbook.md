# Playbook: the quickstart (local kind, CPU-only sim)

This is the primary supported path. It stands up a tiny llm-d stack on a local **kind**
cluster using a **simulated** inference engine (no GPU, no model download), then runs a
small benchmark. Authoritative source: `llm-d-benchmark/docs/quickstart.md` — fetch it
(and the `cicd/kind` scenario) with `fetch_key_docs task="quickstart"` BEFORE you plan, so
the steps/flags come from the real procedure rather than memory.

## The flow (each step is a tool call)
0. **Read the procedure** — `fetch_key_docs task="quickstart"`. Confirm the exact flow,
   flags, and prerequisites from the live docs.
1. **Sense** — `probe_environment` (checks="all"). Confirm: a container runtime is up,
   the `llm-d-benchmark` repo is present, the venv exists, and whether a kind cluster /
   stack is already running.
2. **If a stack already runs** for the target namespace (`stack.detected == true`) — do
   NOT redeploy. Tell the user, and offer to benchmark the existing stack (skip to step 8).
3. **Propose a plan** — `propose_session_plan` with:
   `spec=cicd/kind`, `deploy_path=kind_sim`, `namespace=llmd-quickstart`,
   `harness=inference-perf`, `workload=sanity_random.yaml`,
   `expected_steps=[install_prereqs?, ensure_repos, run_setup, create_cluster?, install_metrics_server?, standup, smoketest, run, report, teardown?]`
   (include `install_metrics_server?` when the user wants the live resource-stats panel — see step 5b).
   Wait for approval.
4. **Prepare** —
   - **Prerequisites** — if `probe_environment` showed `tools.docker == false` and/or
     `tools.kind == false`, install them with
     `run_command argv=["install_prereqs.sh","--docker","--kind"]` (use the subset you
     need, or `--all`). Mutating (prompts); needs root or passwordless sudo. install.sh
     does NOT install Docker or the kind binary — this is how you fill that gap. Relay any
     warning it prints (e.g. the Docker daemon couldn't auto-start on WSL).
   - `ensure_repos` (clones `llm-d-benchmark` if missing).
   - `run_setup` (use_uv=true) — runs `install.sh --uv`. It builds the venv with Python 3.11
     and installs the system tools the framework needs (kubectl, helm, helmfile, jq, yq, …).
     Skip if the venv already exists (the tool reports `already_setup`).
4b. **Ensure a cluster** — if `probe_environment` showed `kind_clusters.clusters` is empty
   (no `llmd-quickstart`), create one:
   `run_command argv=["kind","create","cluster","--name","llmd-quickstart"]`. This is
   mutating (it prompts) and can take a while on first run (it pulls the kind node image).
5. **Stand up** — `execute_llmdbenchmark subcommand=standup spec=cicd/kind
   namespace=llmd-quickstart flags={skip_smoketest:true}`. This can take a few minutes;
   the output streams live.
5b. **Live resource stats (optional — OFFER it).** kind does NOT ship the in-cluster
   **metrics-server**, so the live CPU/mem panel during a run will read
   `live resource stats unavailable (no metrics-server)`. If the user wants that live view,
   OFFER to install it (approval-gated, idempotent):
   `run_command argv=["install_metrics_server.sh","--kubelet-insecure-tls"]`
   — `--kubelet-insecure-tls` is REQUIRED on kind (self-signed kubelet certs). It's a
   PER-CLUSTER add-on: install once and every run on this cluster gets stats. Best done now
   (right after the cluster is up) so the first run already shows live stats, but it can be run
   any time before a run. SKIP if stats are already available (e.g. GKE/OpenShift); the full
   judgment + SKIP cases are in `read_knowledge('observability')`.
6. **Smoketest** — `execute_llmdbenchmark subcommand=smoketest spec=cicd/kind
   namespace=llmd-quickstart`. Confirms the endpoint answers.
7. **Benchmark** — `execute_llmdbenchmark subcommand=run spec=cicd/kind
   namespace=llmd-quickstart harness=inference-perf workload=sanity_random.yaml`.
   (Results are written into the session workspace automatically.)
8. **Report** — `locate_and_parse_report`. Summarize the metrics in plain language
   (see `results_interpretation.md`), tied to the user's goal.
9. **Offer teardown** — ask before running `execute_llmdbenchmark subcommand=teardown`
   (removes the llm-d stack but keeps the cluster). For the deeper cleanup of the whole
   cluster, you can also run `run_command argv=["kind","delete","cluster","--name","llmd-quickstart"]`
   (mutating — it prompts). Always confirm with the user before deleting their cluster.

## Notes / gotchas
- The sim model for `cicd/kind` is `facebook/opt-125m` via `llm-d-inference-sim` — no HF
  token needed (nothing is downloaded from a gated repo).
- A previous quickstart may already be running (namespace `llmd-quickstart`, context
  `kind-llmd-quickstart`). Always probe before deploying.
- `plan` and any `--dry-run`/`--list-endpoints` invocation is read-only and previews
  without changing the cluster — use it to show the user what would happen.
