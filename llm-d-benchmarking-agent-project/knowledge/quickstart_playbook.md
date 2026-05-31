# Playbook: the quickstart (local kind, CPU-only sim)

This is the primary supported path. It stands up a tiny llm-d stack on a local **kind**
cluster using a **simulated** inference engine (no GPU, no model download), then runs a
small benchmark. Authoritative source: `llm-d-benchmark/docs/quickstart.md` — read it with
`read_repo_doc` if you need to confirm exact steps/flags.

## The flow (each step is a tool call)
1. **Sense** — `probe_environment` (checks="all"). Confirm: a container runtime is up,
   the `llm-d-benchmark` repo is present, the venv exists, and whether a kind cluster /
   stack is already running.
2. **If a stack already runs** for the target namespace (`stack.detected == true`) — do
   NOT redeploy. Tell the user, and offer to benchmark the existing stack (skip to step 7).
3. **Propose a plan** — `propose_session_plan` with:
   `spec=cicd/kind`, `deploy_path=kind_sim`, `namespace=llmd-quickstart`,
   `harness=inference-perf`, `workload=sanity_random.yaml`,
   `expected_steps=[ensure_repos, run_setup, standup, smoketest, run, report, teardown?]`.
   Wait for approval.
4. **Prepare** —
   - `ensure_repos` (clones `llm-d-benchmark` if missing).
   - `run_setup` (use_uv=true) — runs `install.sh --uv`, building the venv with Python 3.11.
     Skip if the venv already exists (the tool reports `already_setup`).
5. **Stand up** — `execute_llmdbenchmark subcommand=standup spec=cicd/kind
   namespace=llmd-quickstart flags={skip_smoketest:true}`. This can take a few minutes;
   the output streams live.
6. **Smoketest** — `execute_llmdbenchmark subcommand=smoketest spec=cicd/kind
   namespace=llmd-quickstart`. Confirms the endpoint answers.
7. **Benchmark** — `execute_llmdbenchmark subcommand=run spec=cicd/kind
   namespace=llmd-quickstart harness=inference-perf workload=sanity_random.yaml`.
   (Results are written into the session workspace automatically.)
8. **Report** — `locate_and_parse_report`. Summarize the metrics in plain language
   (see `results_interpretation.md`), tied to the user's goal.
9. **Offer teardown** — ask before running `execute_llmdbenchmark subcommand=teardown`.
   Mention `kind delete cluster --name <name>` is the deeper cleanup (the user runs that
   themselves; it is outside the allowlist).

## Notes / gotchas
- The sim model for `cicd/kind` is `facebook/opt-125m` via `llm-d-inference-sim` — no HF
  token needed (nothing is downloaded from a gated repo).
- A previous quickstart may already be running (namespace `llmd-quickstart`, context
  `kind-llmd-quickstart`). Always probe before deploying.
- `plan` and any `--dry-run`/`--list-endpoints` invocation is read-only and previews
  without changing the cluster — use it to show the user what would happen.
