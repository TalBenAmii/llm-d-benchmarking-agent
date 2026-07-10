# SPEC: Reproducibility artifact — "Reproduce this run" + provenance bundle

> **Status (2026-06): IMPLEMENTED & merged.** This spec is preserved as the design record for
> shipped code. The capability now lives in `app/storage/provenance.py`, `app/tools/analyze/reproducibility.py`
> (tools `export_run_bundle` + `reproduce_run`), `app/packaging/report_card.py`, and
> `knowledge/reproducibility.md`. File-level details below reflect the original design intent and may
> differ slightly from the final code (e.g. the agent now exposes **36 tools**, not the 28→30 noted
> in §4.4/§6.4).

## 0. Confirmation of invariants

- **Thin code, thick agent** — `CLAUDE.md:31-35`; mechanism/judgment split enforced (e.g. `app/tools/run/execute.py` is pure argv mechanism, `knowledge/runconfig_roundtrip.md` is the judgment).
- **Determinism gates** — four gates in `app/validation/CLAUDE.md`: SessionPlan+catalog (`session_plan.py`), tool-arg schema (`registry.py:dispatch`), DoE/config, BR-v0.2 report (`report.py`). Reproduce must pass through gate 1 + the CLI `--dry-run` gate.
- **Deny-by-default allowlist (DATA)** — `security/allowlist.yaml`; `app/security/allowlist.py` is a pure validator.
- **Sibling repos READ-ONLY** — writes hard-blocked. The bundle only WRITES under `ctx.workspace`, only READS `git rev-parse` against the repos.
- **Hermetic pytest** — git-SHA capture must degrade gracefully when repos are absent (the worktree case, `CLAUDE.md:148-151`).

A new capability is added the way every prior phase did: a new tool (handler + Pydantic schema + registry wiring), knowledge for judgment, a backend export endpoint, a UI affordance, allowlist DATA edits, hermetic tests. No decision logic in Python.

---

## 1. Goal & user-facing behavior

A benchmark result becomes **credible** only if someone can regenerate it. Today the agent produces a validated BR-v0.2 summary (`renderReportSummary`, `ui/app.js:1920`) and can store it to history (`app/storage/history.py`), but nothing captures *the exact inputs that produced it* — no repo SHAs, no resolved config, no environment snapshot. This adds a one-click **provenance bundle** + a **"Reproduce this run"** path that goes back through the existing approval + dry-run gates.

### 1a. Where the affordances appear
1. **Report-summary card** (`renderReportSummary`, `ui/app.js:1920-1963`) gains a footer action row:
   - **"Reproduce this run"** — sends a canned user message over the existing `/ws` (e.g. `"Reproduce this run from its provenance bundle <bundle_id>"`). NOT a direct mutation: it prompts the agent to call `reproduce_run`, which re-derives a SessionPlan and routes the rerun through `execute_llmdbenchmark` — straight through SessionPlan-approval + dry-run gates.
   - **"Export report card"** — `window.open('/api/sessions/{sid}/bundle/{bundle_id}/report-card.html')`, a download of the self-contained HTML.
2. **Results sidebar** (`renderHistory_`, `ui/app.js:562`; `/api/history`): each stored record with a bundle gets the same two affordances.

### 1b. The regenerate command / tool call
The bundle stores a **single copy-paste CLI command** from the captured resolved run-config — the upstream round-trip from `knowledge/runconfig_roundtrip.md:23-26`:
```
llmdbenchmark run -c <bundle>/run-config.yaml -p <namespace>
```
plus a note: *"requires `llm-d`@`<sha>` + `llm-d-benchmark`@`<sha>` and a stack serving `<model>` (`-c` is run-only)."* In-agent, the equivalent is the **one tool call** the agent makes after the user clicks Reproduce: `reproduce_run(bundle_id=...)`.

### 1c. The shareable HTML
A single `.html`, **no external assets** (system-stack fonts, CSS inlined in `<style>`, the agent's hex logo as an inline SVG data URI as in `ui/preview.html:8`). Sections: Header (model/harness/workload/spec/timestamp/agent version); Results (BR-v0.2 headline tiles + full percentile ladder from `summarize_report`, `report.py:329-405`, + SLO verdicts if an `analyze_results` result was captured); Provenance (both repo SHAs + dirty flags, resolved config in a collapsed `<details>`, env snapshot, knowledge hash); Reproduce (copy-paste command + "requires same SHAs" caveat); Honesty banner (loud warning if either repo was dirty). Produced by a backend export endpoint + a pure-Python string template (no jinja dependency — keeps it dependency-light + hermetic).

---

## 2. The provenance schema

A new dataclass `ProvenanceBundle` in `app/storage/provenance.py` (mechanism). Fields:

| Field | Source | Notes |
|---|---|---|
| `bundle_id` | content hash (sha256, 16 hex) of `run_uid + report_path + repo SHAs` | mirrors `compute_record_id`, `history.py:108-124` |
| `created_at` | `time.time()` | |
| `agent_version` | `importlib.metadata.version("llm-d-benchmarking-agent")` (pyproject `0.1.0`) | new `Settings.agent_version` property |
| `knowledge_version` | sha256 over the sorted `knowledge/*.md|*.yaml` glob (reuse `app/agent/prompt.py::_knowledge_sections` glob) | captures the agent's "brain" |
| `repos` | `{"llm-d": {sha, dirty, ref}, "llm-d-benchmark": {sha, dirty, ref}}` | from `git rev-parse HEAD` + `git status --porcelain` per repo |
| `resolved_config` | workspace path + inlined YAML body from `run --generate-config` (`knowledge/runconfig_roundtrip.md:10-16`) | canonical "exact resolved config" |
| `spec`/`harness`/`workload`/`namespace`/`model` | approved SessionPlan (`session.approved_plan`, `session.py:75`) + report summary | |
| `slo` | SessionPlan's `slo` block, if any | for re-deriving SLO verdicts |
| `env_snapshot` | captured `probe_environment` result (`probe.py:89`) | what cluster/provider/K8s version it ran against |
| `report_digest` | sha256 of validated report bytes + `summarize_report` output | ties bundle to the report gate |
| `report_summary` | `summarize_report(report)` (`report.py:329`) | validated, log-free numbers (gate d) |
| `regenerate_command` | computed argv string from `resolved_config` + `namespace` | the copy-paste line |

### 2a. Storage (two locations, mirroring history)
1. **Alongside the run** — `ctx.workspace / "bundles" / f"{bundle_id}.json"` (per-session workspace; never the repos). The generated `run-config.yaml` already lands via `--generate-config` (`execute.py:484-486`); the bundle references that path + inlines a copy.
2. **In the history store** — `HistoryRecord` gains optional `bundle_id: str | None` + `provenance: dict | None`. Additive; `_read` already ignores unknown fields (`history.py:222-228`).

No new managed area: `workspace/bundles/` lives under the per-session dir (GC'd via `sessions`, `retention.py:56`); records GC'd via `history`. No `retention.py` change.

---

## 3. Architecture — reuse, don't reinvent

### 3a. Reuse the config round-trip (do NOT re-serialize config)
The "exact resolved config" is **already** produced by `run --generate-config` (`execute.py` `_SUBCOMMAND_FLAGS["generate_config"]` at line 156, anchored under the session workspace at `execute.py:484-486`). Provenance capture **references/inlines** that YAML — it does NOT build its own serializer. Guarantees byte-identical config for replay.

### 3b. Reuse report parsing (gate d)
`report_digest`/`report_summary` come from existing `load_report` + `validate_report` + `summarize_report`. The bundle **refuses to capture** an unvalidated report — as `history.py::_store` refuses (`app/tools/analyze/history.py:71-78`). Honest: only certifies a schema-valid run.

### 3c. "Regenerate" maps to the existing run/orchestrate path
`reproduce_run` does **not** shell out directly. It:
1. Loads the bundle; extracts `spec/harness/workload/namespace/slo` + the captured `run-config.yaml` path.
2. Returns a structured proposal the agent turns into a `propose_session_plan` call (gate 1: catalog-validated, approval-gated).
3. The agent calls `execute_llmdbenchmark(subcommand="run", flags={"run_config": "<path>", "dry_run": True})` first (CLI `--dry-run` gate), and only on a clean dry-run does the approved mutating `-c` replay proceed (`knowledge/runconfig_roundtrip.md:18-20`).
Reproduce reuses the SessionPlan-approval + dry-run gates; no new mutation path.

### 3d. Mechanism vs judgment
- **Python (mechanism)**: capture SHAs/env/config/report into a `ProvenanceBundle`; serialize JSON; render the self-contained HTML; compute the copy-paste command; refuse invalid report / missing SHA.
- **Knowledge (judgment)**: WHEN to offer a bundle; how to explain a **dirty repo** caveat to a non-expert; how to sequence a reproduce (dry-run → approve → replay); what to say on env drift.

### 3e. Self-contained HTML
New `app/packaging/report_card.py` (`app/packaging/` exists) holds `render_report_card(bundle: dict) -> str` — one HTML string, all CSS inlined, zero external references. The endpoint streams it with `Content-Disposition: attachment`. No jinja: `format()`/escaped-interpolation helpers (hermetic-testable: assert the string contains the SHAs and no `http://`/`https://` asset links).

---

## 4. Exact new / changed files

### 4.1 New: `app/storage/provenance.py` (capture + data model)
- `@dataclass ProvenanceBundle` (§2 fields) + `to_json()`.
- `capture_repo_state(repo_path, run_readonly) -> dict` — runs the **already-allowlisted** `git rev-parse HEAD` + `git status --porcelain` via `ctx.run_readonly`; returns `{sha, dirty, ref}`. Missing/empty repo → `{sha: None, dirty: None, unavailable: True}`, never raises.
- `knowledge_hash(knowledge_dir) -> str` — sha256 over the sorted knowledge glob.
- `build_bundle(...)` — pure assembly from gathered inputs (only I/O = reading the run-config the CLI wrote). Refuses if the report-validation object isn't valid.
- `BundleStore` — `write(ctx.workspace, bundle)` + `read(workspace, bundle_id)` under `workspace/bundles/`, with the same `_safe_id`-style guard as `history.py:231`.

### 4.2 New tool: `app/tools/analyze/reproducibility.py` (one tool, action-dispatched, mirrors `result_history`)
- `export_run_bundle(ctx, *, source, namespace=None, label=None, env_snapshot=None)` — locates+validates the report (reuse `find_reports`/`load_report`/`validate_report`), reads the session's generated run-config (or notes its absence → tells the agent to run `--generate-config` first), captures both repo SHAs via `ctx.run_readonly`, captures `agent_version`/`knowledge_version`, writes the bundle, optionally attaches to a history record, returns `bundle_id` + `regenerate_command` + a `dirty` flag. Auto-runs (read-only: git reads + a workspace write).
- `reproduce_run(ctx, *, bundle_id)` — reads the bundle, returns the structured rerun proposal (spec/harness/workload/namespace/slo + run-config path + dry-run-first instruction + dirty-state caveat). Auto-runs (proposes, mutates nothing). The agent then drives `propose_session_plan` → dry-run → approved `-c` replay.

### 4.3 Changed: `app/tools/schemas.py`
Add `ExportRunBundleInput` + `ReproduceRunInput` (gate 2). Field descriptions cue `read_knowledge('reproducibility')`.

### 4.4 Changed: `app/tools/registry.py`
Import `reproducibility`; two `_DESCRIPTIONS` entries (cue `read_knowledge('reproducibility')` + the dry-run-first sequence); two `ToolSpec` rows. (These two tools landed; the registry now exposes **36 tools** total. As built, count refs were updated in `CLAUDE.md`, `app/tools/CLAUDE.md`, `docs/reference/API.md`, `FEATURES.md`.)

### 4.5 New knowledge: `knowledge/reproducibility.md` (judgment, on-demand)
When to offer a bundle; how to explain dirty-repo caveats to non-experts; the exact reproduce sequence (generate-config → bundle → for replay: propose_session_plan → dry-run → approve `-c`); env-drift caveats; the boundary that `-c` is run-only (needs a live stack — cross-ref `read_knowledge('runconfig_roundtrip')`). On-demand only — NOT CORE.

### 4.6 Changed: `app/main.py` — export endpoint
Add `@app.get("/api/sessions/{sid}/bundle/{bundle_id}/report-card.html", dependencies=[Depends(rate_limit)])`. Resolve the bundle with the **same path-traversal hardening** as `session_artifact` (`main.py:368-381`: `base.parent == sessions_root`, `is_relative_to`, `_safe_id`). Call `render_report_card(bundle)`, return `Response(media_type="text/html", headers={"Content-Disposition": ...})`. Also add `GET /api/sessions/{sid}/bundle/{bundle_id}` (JSON) for UI metadata; have `/api/history` include `bundle_id` in `_history_record_view` (`main.py:270-275`).

### 4.7 New: `app/packaging/report_card.py`
`render_report_card(bundle: dict) -> str` — the self-contained HTML template (§3e).

### 4.8 Changed: `ui/app.js` + `ui/styles.css` (+ mirror in `ui/preview.html` fixture)
- `renderReportSummary` (`app.js:1920`): append a `.report-actions` footer with **Reproduce** + **Export** buttons. Reproduce sends a user message; Export `window.open(...)`. Wire when the tool result carries a `bundle_id`, OR add a "Save provenance bundle" button.
- `renderHistory_` (`app.js:562`): same two actions per record with a `bundle_id`.
- Reuse `copyText`/copy-button helper (`app.js:2066-2092`) for the command.
- Add `renderReproducibilityCard(r)` dispatch in `finishTool` (`app.js:1842+`) for the `export_run_bundle` result (bundle id, dirty banner, command + copy, Export link).
- Minimal CSS for `.report-actions`, `.prov-dirty-banner`.

### 4.9 Changed: `app/storage/history.py`
Add `bundle_id: str | None = None` + `provenance: dict | None = None` to `HistoryRecord` (additive; `_read` tolerates). `add()` stores them; `_record_view` in `app/tools/analyze/history.py:27` + `main.py:270` surface `bundle_id`.

### 4.10 Changed: `app/config.py`
Add `agent_version` property: `importlib.metadata.version("llm-d-benchmarking-agent")` with a `"0.0.0+unknown"` fallback.

### 4.11 Allowlist (`security/allowlist.yaml`) — small, additive
Regenerate **reuses the existing approval-gated `-c` replay** (`run_config` flag, pinned to `run_config_path`) — **no new mutating path**. Only read-only DATA changes:
- `git rev-parse` (`allowlist.yaml:470-475`) pins its positional to the `git_ref` regex (`^[A-Za-z0-9._/-]+$`, line 215) which already admits `HEAD`, but doesn't declare `--short`. Add `--short: {}` so a short SHA is permitted. (`rev-parse HEAD` works today.)
- `git status` (`allowlist.yaml:469`) already allows `--porcelain`. No change for dirty detection.

So: **one tiny flag addition** (`git rev-parse --short`). Note in the PR: reproduction does NOT widen mutation capability.

---

## 5. Determinism note — keeping the bundle honest

- **Dirty/uncommitted state recorded, not hidden.** `capture_repo_state` runs `git status --porcelain`; non-empty → `dirty: True`. Bundle, tool result, and HTML all surface a prominent caveat. Knowledge teaches the agent to say it plainly.
- **An exact rerun needs the same SHAs.** The `regenerate_command` ships with the required SHAs; reproduce captures current SHAs and the knowledge instructs the agent to warn if they differ.
- **Passes through gates, doesn't bypass them:** capture refuses an unvalidated report (gate d); numbers only from `summarize_report` (never log-scraped). Reproduce → `propose_session_plan` (gate 1) → CLI `--dry-run` → approval-gated `-c` replay — never a direct subprocess. `env_snapshot` lets the agent flag env drift.
- **Honest about missing SHAs:** in a worktree with empty sibling repos, capture returns `unavailable: True` rather than fabricating; the bundle is still produced (results are real) but flagged non-reproducible-as-captured. Never fabricate a SHA.

---

## 6. Hermetic test plan, acceptance, effort, risks

### 6.1 Tests (all hermetic)
`tests/test_provenance.py`:
- `build_bundle` over a fixture BR-v0.2 report (reuse `tests/test_report_validation.py` fixtures) + a fake run-config → every §2 field present; `report_digest` stable; refuses an invalid report.
- `knowledge_hash` deterministic; changes when a knowledge file changes (tmp dir).
- `capture_repo_state` against a tmp git repo → `dirty=False`; touch a file → `dirty=True`; missing dir → `unavailable=True`, no raise.
- `BundleStore` round-trip; `_safe_id` rejects traversal.

`tests/test_report_card.py`:
- `render_report_card(bundle)` HTML contains both SHAs, the model, the regenerate command, and **no** `http://`/`https://` asset link, no `<link href=` to a non-data URL.

`tests/test_reproducibility_tools.py`:
- `export_run_bundle` over a fixture report dir → returns `bundle_id` + `regenerate_command`; idempotent.
- `reproduce_run` returns a proposal carrying the run-config path + dry-run-first instruction; never emits a mutating command (assert no `ctx.run_command`).

Extend: `test_schemas.py` (two new models), `test_new_tools.py` (registry + descriptions cue knowledge), `test_allowlist.py` (`git rev-parse --short HEAD` allowed read-only; `-c` replay stays mutating/approval-gated), a `main.py` endpoint test (TestClient: `text/html` + `Content-Disposition: attachment`; traversal in `sid`/`bundle_id` 404s), `test_history*.py` (`bundle_id`/`provenance` round-trip; old records still load).

### 6.2 Acceptance criteria
1. After a validated run, the agent can produce a bundle capturing both repo SHAs (+dirty), resolved config, env snapshot, knowledge hash, agent version, validated report digest.
2. The report-summary card + history sidebar expose Reproduce + Export when a bundle exists.
3. Export yields a single self-contained `.html` (results + full provenance + copy-paste command, no external assets).
4. Reproduce drives `propose_session_plan` → dry-run → approval-gated `-c` replay (verified it doesn't bypass gates).
5. Dirty repo state recorded + surfaced everywhere.
6. Full suite green (~1650 baseline).

### 6.3 Effort: **M** (capture/model/store + tool + schema/registry = S each; HTML template + UI + history threading → M). No new heavy deps.

### 6.4 Risks & open questions
- **`run --generate-config` needs the bench venv + a coherent run context.** If no config this session, `export_run_bundle` instructs the agent to run it first. Open: auto-trigger `--generate-config`, or only when absent? (Recommend: report "no run-config found; generate one" — keeps the tool thin.)
- **`env_snapshot` plumbing.** `session.env_snapshot` is runtime-only (`session.py:109-112`). The tool gets it from the agent passing the last `probe_environment` result, or re-probing read-only at capture. (Recommend re-probe for freshness + hermetic testability.)
- **Tool-count doc drift.** Several docs hard-code a tool count; grep + update so doc-consistency tests don't fail. (Resolved as built — the registry is now at **36 tools**.)
- **HTML size.** Inlining chart PNGs as base64 could bloat; v1 embeds numeric tables + percentile ladder, lists chart filenames rather than embedding images.
- **Knowledge-hash sensitivity.** Any knowledge edit bumps the version even if behavior-neutral — acceptable (coarse provenance signal); document in the knowledge file.

### Critical files
- `app/tools/run/execute.py` (the `--generate-config`/`-c` round-trip the bundle reuses; lines 156, 273-283, 484-486)
- `app/storage/history.py` (record model + content-hash pattern; additive `bundle_id`)
- `app/validation/report.py` (the report gate — `load_report`/`validate_report`/`summarize_report`)
- `app/main.py` (export endpoint; reuse `session_artifact` traversal hardening at 368-381)
- `app/tools/registry.py` (tool wiring) + `security/allowlist.yaml` (the one read-only `git rev-parse --short` addition; lines 461-475)
