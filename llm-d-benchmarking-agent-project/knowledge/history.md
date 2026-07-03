# Historical result storage + trends (the `result_history` tool)

The `result_history` tool persists **validated** Benchmark Report summaries across
sessions and lets you read **trends** over time. The tool is pure mechanism — it stores
facts and returns value-series; *you* supply the judgment (is this a regression? is the
drift acceptable?). Nothing here touches the cluster or the repos, so every action
auto-runs (no approval).

## When to store a result
Store a run the user will care about *later*, **after** you've already located/parsed it
(`locate_and_parse_report`) and, ideally, analyzed it (`analyze_results`):
- A **baseline** the user wants to track regressions against ("remember this as our 8B
  baseline").
- Each treatment of a **sweep/experiment** they want to revisit or compare across days.
- Any run the user explicitly asks to "save", "keep", or "remember".

**Proactively store the first real benchmark of a session** (not a smoketest) once you've
parsed it — make it the baseline and tell the user ("I've saved this as your baseline so you
can track future runs against it"). The Results panel and trend chart in the UI stay **empty
until something is stored**, so storing the first real result is also what makes that
visualization appear at all; don't leave the user with an empty panel after a successful run.

Do NOT auto-store every throwaway smoketest — storing is for results with lasting value.
Always pass a clear `label` and useful `tags` (e.g. `["8B","baseline"]`, or
`["concurrency-sweep","2026-06"]`) so `list`/`trend` can be filtered later. Pass the
`spec`/`harness`/`workload`/`namespace` you used as provenance. Storing is **idempotent**:
re-storing the same report returns the existing record (`created: false`) rather than a
duplicate — mention that to the user instead of implying a second save happened.

The tool **refuses to store a report that fails schema validation** (determinism gate d):
if `stored: false` with a validation reason, fix/locate a valid report first; never
hand-edit numbers to make it store.

**Never seed history/trends with user-asserted numbers.** Only metrics from a validated report
(`locate_and_parse_report` → `analyze_results` this session) may become a baseline, a stored
record, or a trend point. If the user describes a prior run ("yesterday we got goodput 1.24,
P99 180 ms, SLO PASS") and `result_history` returns **empty**, that empty result is the answer:
those numbers are **not** in the store and you cannot verify them. Do **not** "take them at face
value as the baseline", render a formal PASS table from them, or plan to persist them — a user
could otherwise seed the trend store with arbitrary numbers and have you build legitimate-looking
comparisons on top. Say the history is empty so there's no validated baseline, and offer to
**re-run that scenario** to establish a machine-validated one before trending against it. (Same
authority rule as `knowledge/results_interpretation.md` § "Honesty floor".)

### Let `analyze_results` tell you when to offer save/compare
After a run, `analyze_results` returns a ranked `next_steps` list computed over the validated
results AND your saved history (does this run's `run_uid` already exist in the store? how many
comparable runs of the same model are saved?). It deliberately ranks **save-to-trend** and
**compare-to-baseline** above teardown/run-again — so when nothing is saved yet its top step is
"save this as your baseline", and once a comparable run exists it's "compare against your last
run". Use it to pick the ONE follow-up to offer (per `knowledge/conversation_style.md`); it's
input to your judgment, not a script — never recite the list to the user.

## Reading a trend
`action="trend"` with a `metric` returns the chronological series (oldest → newest), the
metric's `better` direction (`lower` for latency, `higher` for throughput/success-rate),
the representative `stat` used (mean if present), units, and a factual first-vs-last delta.
Filter the series with `filter_tag` / `filter_model` so you trend *comparable* runs (don't
mix a 1B and a 70B model, or two different workloads, in one latency trend — that's not a
regression, it's a different test).

Available metrics: `ttft`, `tpot`, `itl`, `request_latency` (latency, lower is better);
`output_token_rate`, `total_token_rate`, `request_rate` (throughput, higher is better);
`success_rate_pct` (higher is better).

Standard/serving-metric trends (§3.4): `kv_cache_hit_rate` (higher is better),
`gpu_utilization` (informational — `better: higher` means *more utilized*, not strictly
"better"; read it next to throughput), and `schedule_delay` (lower is better — a
**queue-depth proxy**, i.e. requests waiting to be scheduled, NOT a millisecond delay).
These three are populated **only when the run was done with monitoring on**
(Phase 27 / `flags.monitoring` → `--monitoring`, which fills `results.observability`); runs
without monitoring carry no point for them and are simply skipped from the series (so a
sparse series usually means "monitoring wasn't on for every run", not a regression). They
are surfaced for *context* only and never enter goodput/SLO/Pareto dominance. For the full
interpretation see `knowledge/results_interpretation.md` (§ "Standard resource/serving
metrics" and its "Trending these over time" note) and `knowledge/observability.md`.

## Turning a trend into a verdict (your job, not the tool's)
- Use `better` to read the sign of `first_to_last.delta_pct`: a latency metric going **up**
  or a throughput metric going **down** is *worse*; the reverse is *better*.
- A single small wiggle is **noise**, not a regression — benchmark runs vary. Call out a
  trend only when it's consistent across several points or a large step change. Be explicit
  that you cannot prove statistical significance from these aggregates.
- Anchor "regression" to the user's SLO when they have one (see `knowledge/analysis.md`):
  a 5% TTFT increase that's still under the SLO target is usually fine; one that crosses the
  target is a real regression. Tie the trend back to `analyze_results` when SLOs exist.
- The series carries `run_uid`, `label`, and `tags` per point — name the specific runs when
  you explain a change so the user can go look at them.
- **Explain a delta only from what the records actually show.** Before you attribute a change
  between two stored runs (e.g. "the new router config helped"), READ each record's config /
  label / tags FIRST and attribute the delta only to a difference you can *see* there. If the
  configs are identical or the cause isn't in the stored data, say so plainly — "the cause isn't
  determinable from these records" — and propose how to find out (re-run with monitoring on, diff
  the rendered per-treatment configs). Never invent a causal story the records don't support, and
  never assert one your own tool result contradicts. (Sweep deltas: `read_knowledge('sweep_results')`.)
- If `n` is 0 or 1, say there isn't enough history yet to call a trend, and suggest storing
  more baselines first.

## Browsing in the UI
The same store backs the read-only `GET /api/history` (results browser) and
`GET /api/history/trend?metric=...` (trend chart) endpoints, so anything you store is what
the user sees in the Results panel. The UI shows facts; you provide the narrative.

## CLI Results Store (optional, for team sharing) — a DIFFERENT store

There are **two independent result stores**, and you should not confuse them:

1. **The agent's local history store** (everything above — the `result_history` tool, backed
   by `app/storage/history.py`). It persists *your* validated report summaries on this host,
   powers the Results panel + trend chart, and is the **default** for "remember this run",
   "track regressions", "trend TTFT over time". It needs no remotes, no credentials, no
   network. **For the local need, this is all you reach for.**

2. **The CLI's git-like Results Store** (`llmdbenchmark results …`, modeled by
   `execute_llmdbenchmark(subcommand="results", store={...})`). This is an **optional,
   team-shared** store: it `init`s a local `.result_store/`, configures **GCS remotes**, and
   **pushes/pulls** whole run workspaces so a *team* can publish and exchange results through a
   shared bucket (taxonomy `scenario/model/hardware/run-uid`). It is upstream's tool, not ours.

**These two stores are completely separate.** Using the CLI store **does not** change, mirror,
or replace the local history store — they don't sync. Storing a run with `result_history` does
*not* publish it to a remote, and pulling a run from a remote does *not* add it to the local
history/trends (parse + `result_history store` it yourself afterwards if you want it tracked).

### WHEN to reach for the CLI Results Store (not the default)
Use it **only** when the user explicitly wants to **share results with a team** via the CLI's
shared bucket, e.g. "publish this to our results bucket", "pull the prod run `c6bc210e`",
"what runs has the team pushed to staging?". For anything local — tracking, trending,
baselines, the Results panel — **stay with `result_history`**. If the user has no team bucket /
GCS remote, the CLI store has nothing to offer over the local one; don't suggest it.

### How to drive it (mechanism is in the tool; these are the moves)
Call `execute_llmdbenchmark(subcommand="results", store={...})`. The `command` selects the op:
- `init` — create the local `.result_store/` (read-only, auto-runs).
- `remote` + `remote_action`: `add` (name + `gs://bucket/prefix` `uri`), `rm` (name), or `ls`
  (list remotes, read-only). Adding/removing a remote is mutating (a local config change).
- `status` — list locally staged/untracked runs (read-only).
- `add` / `rm` — stage/unstage runs by `paths` (local dirs or run-uids). **Mutating.**
- `ls` — list a remote's runs: `remote` (alias) + optional `model`/`hardware` filters
  (read-only). **Wildcards are NOT supported here** (the `*` in upstream's `llama-*` examples is
  rejected as a shell metacharacter); pass an exact value.
- `push` — publish to a remote: optional `remote` (default `staging`) + optional `path` +
  optional `group`. **Mutating, approval-gated** (it uploads to GCS).
- `pull` — download a run: optional `remote` (default `prod`) + **required** `run_uid` (an exact
  uid, no wildcards). **Mutating, approval-gated** (it writes a workspace dir).

The publish/pull steps go through the same approval gate as a real standup/run — never
auto-run; tell the user what bucket/remote they're about to push to or pull from. Remote URIs
are **GCS-only** (`gs://…`), matching upstream's prod/staging defaults. The actual GCS transfer
runs inside the CLI subprocess with the user's own cloud credentials.
