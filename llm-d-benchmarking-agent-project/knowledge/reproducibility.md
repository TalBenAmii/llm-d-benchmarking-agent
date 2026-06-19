# Reproducibility — provenance bundles + "Reproduce this run"

A benchmark number is only **credible if someone can regenerate it**. Two on-demand tools make a
result reproducible and shareable:

- `export_run_bundle` — captures a **provenance bundle**: both read-only repo SHAs (+ dirty flags),
  the exact resolved run-config, an environment snapshot, the knowledge hash, the agent version,
  and the *schema-validated* report digest + summary. Read-only; auto-runs (git reads + a workspace
  write). It returns a `bundle_id` + a copy-paste `regenerate_command` + a `dirty` flag.
- `reproduce_run` — reads a saved bundle and returns a **rerun proposal**. It mutates nothing. YOU
  then drive the replay through the existing gates (below).

This is judgment for WHEN and HOW; the Python is pure mechanism (it captures, hashes, renders, and
refuses an invalid report — it makes no decision).

> **Upstream provenance, for grounding.** The bench repo's own `docs/reproducibility.md` says that
> everything collected under `LLMDBENCH_CONTROL_WORK_DIR` — `environment/variables` (all applied
> `LLMDBENCH_*` params), `setup/` yamls, results, workload/profiles, analysis — is sufficient for
> someone else to reproduce the run with the same parameters. The agent's bundle captures the same
> provenance (repo SHAs + the resolved run-config + an env snapshot), so a user can regenerate the
> run without needing that work-dir.

## When to OFFER a bundle

Offer one **after** you've parsed and analyzed a run the user cares about — a baseline they'll
compare against, a result they want to share, or a config they'll re-run. Don't offer it for a
throwaway smoketest. The proactive moment is the same as "save to history": once a validated result
is worth keeping, a bundle makes it *reproducible*, not just *stored*. Make it ONE concise offer
(see `read_knowledge('conversation_style')` for the one-offer cadence), e.g. "Want me to capture a
reproducibility bundle (repo versions + exact config) so this run can be regenerated or shared?"

## Make it byte-reproducible FIRST (generate the run-config)

The bundle inlines the CLI's own **resolved run-config** so a replay is byte-identical. If this
session has no generated run-config, `export_run_bundle` records `run_config_found: false` and the
bundle is still valid but not byte-reproducible. To fix that, FIRST run
`execute_llmdbenchmark(subcommand="run", flags={"generate_config": True})` (read-only; writes
`run-config.yaml` under the session workspace — see `read_knowledge('runconfig_roundtrip')`), THEN
export the bundle. Prefer doing this so the user gets a real, replayable artifact.

## The reproduce SEQUENCE — through the gates, never around them

`reproduce_run` only PROPOSES. To actually reproduce, follow the gated order IN FULL:

1. **`propose_session_plan`** with the captured spec / harness / workload / namespace / slo — the
   catalog-validated, **approval-gated** plan (determinism gate 1). Never skip this.
2. **Dry-run preview**: `execute_llmdbenchmark(subcommand="run", flags={"run_config": "<path>",
   "dry_run": True})` — the CLI `--dry-run` gate. Read-only; mutates nothing. Confirm it's clean.
3. **Only on a clean dry-run**, the approved replay:
   `execute_llmdbenchmark(subcommand="run", flags={"run_config": "<path>"})` — the
   approval-gated `-c` replay.

`-c` is **run-only**: it replays the resolved config against a stack that is **already serving the
captured model** (it does NOT stand one up). If no stack is up, that's a precondition to resolve
first (stand up, or target a live endpoint) — see `read_knowledge('runconfig_roundtrip')`. Never
shell out to reproduce directly; reproduction reuses the SAME gates a normal run does and adds no
new mutation path.

## Honesty: dirty repos, missing SHAs, env drift

State these plainly to a non-expert — never bury them:

- **Dirty repo** (`dirty: true`, or a repo's `dirty: true`): there were **uncommitted changes** when
  the run was captured. An exact re-run needs the *same working tree*, not just the recorded SHA.
  Say so: "this run was captured with local uncommitted changes, so it's only exactly reproducible
  on that same checkout." The report card shows a loud warning banner for this — mirror it in chat.
- **Unavailable SHA** (`unavailable: true` for a repo): the repo was empty/absent at capture, so no
  SHA could be recorded. **Never fabricate one.** The results are real, but the run was NOT captured
  as exactly reproducible — tell the user that honestly.
- **SHA drift**: when you reproduce, compare the *current* repo SHAs to the bundle's captured ones.
  If they differ, warn that the upstream tooling has changed since the original run, so the replay
  may not match exactly. Suggest checking out the captured SHAs for a true reproduction.
- **Env drift**: the bundle carries an `env_snapshot` (cluster/provider/K8s context). If today's
  environment differs (different cluster, provider, node sizes), flag that the result may differ for
  reasons unrelated to the config.

## What the bundle does NOT do

It does not widen any capability: the only new allowlist entry is the read-only `git rev-parse
--short`. It refuses to certify an **unvalidated** report (determinism gate d) and reads numbers
ONLY from `summarize_report` — never scraped from logs. The knowledge hash is a *coarse* signal: any
knowledge edit bumps it even if behavior-neutral. The HTML report card is fully self-contained (no
external assets) and safe to share.
