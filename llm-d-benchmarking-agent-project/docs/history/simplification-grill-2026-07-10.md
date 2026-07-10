# Simplification / YAGNI Grill — decision log (2026-07-10)

Running record of the simplification grilling session: every question asked and the
user's answer, so the phased removal plan is traceable to explicit decisions.

## Scope (the frame that gates every decision)

**REVISED (wave-3 clarification).** The user deploys this app **into their own real
GPU cluster** and wires it to their inference stack: a **long-lived, in-cluster,
SINGLE-USER** service on **real (expensive, multi-hour) GPU** hardware, with the user's
own LLM token. The local `kind` path (`install.sh` → `scripts/install_service.sh` Helm,
+ `testing/`) is the **dev/POC environment and STAYS in the project.**

Consequence — the dividing line is **single-USER vs multi-USER**, NOT local-vs-prod:
- **Justified by the real long-lived cluster deployment (KEEP):** Prometheus/metrics
  (clusters run Grafana), K8s-Job orchestrator + resilience (expensive multi-hour GPU
  sweeps), retention/GC (a long-lived service accumulates), Gateway-API readiness (real
  gateways), provenance/reproducibility, Helm.
- **Genuinely multi-USER → removal candidates:** rate-limiting + per-user quotas, public
  share links. (Optional Bearer auth is reconsidered — an in-cluster endpoint may want
  it even single-user.)

This flips several wave-3 "remove" recommendations to "keep" (see revised Q10–Q18).

## Ground rules for this pass

- **Appetite (Q1 = #1):** dead code + mechanical dedup + thin-code fixes + the
  explicitly-agreed feature removals below. No speculative rewrites.
- **Tests (Q3/Q4):** do **not** trim tests of *kept* features. BUT removing a whole
  feature necessarily removes *that feature's own tests* (mechanical fallout, not
  "test right-sizing") — that is in-scope.
- **Delivery (Q9 = #1):** one branch, phased commits, single draft PR at the end.
  Local finish loop (review → `--no-ff` merge to main) runs; **push / PR opening waits
  for the user's explicit go-ahead.**

## Decisions locked (waves 1–2)

| # | Question | Decision |
|---|---|---|
| Q1 | Overall appetite | **#1** — dead code + mechanical dedup + thin-code fixes only |
| Q2 | CoT trace file (`app/agent/cot_trace.py`, write-only, no readers) | **#1 — DELETE** |
| Q3 | Cloud results sink (`-r gs://…` passthrough) | Don't touch (leave code + tests as-is) |
| Q4 | Test-DSL harness + over-testing ratios | Don't touch tests |
| Q5 | Thin-code violations | **#1** — move `recommend_next_steps` → `knowledge/`; **keep** `capacity/classify_diagnostics` (documented BUG-030 defense) |
| Q6 | LLM providers | Claude-only: keep `claude-agent-sdk` (default) + `anthropic` API-key (optional); **REMOVE the `openai`/vLLM provider** |
| Q7 | auth / rate-limit / Prometheus / Helm / provenance | Superseded by the scope clarification → auth/rate-limit **REMOVE**; Helm **KEEP** (it's the install mechanism); Prometheus/provenance → see wave 3 |
| Q8 | Provider-removal fallout (delete openai tests, drop dispatch, fix DEPLOYMENT/CLUSTER_SERVICE docs) | **#1 — do all of it** |
| Q9 | Delivery shape | **#1** — one branch, phased commits, single PR at end; push waits for go-ahead |

## Audit inputs (evidence base)

Six parallel sonnet audits produced the candidate list:
- Core (agent/tools/llm): lean; only ~35–165 LOC mechanical dead-weight; big items are
  product-scope, not padding.
- Determinism gates + orchestrator: gates are load-bearing; orchestrator
  sweep/dead-letter/checkpoint is the concentrated speculative robustness.
- Supporting infra: Prometheus stack unconsumed; retention-GC gold-plating; auth
  multi-tenant; CoT trace write-only.
- Feature/test proportionality: ~26% app LOC + ~18% test LOC ride speculative features.
- Tool/schema orphans: **zero** — everything wired.
- Knowledge orphans: 4 mis-homed docs (`api_trust.md`, `workspace_lifecycle.md`,
  `packaging.md`, `logging.md`) → belong in `docs/`, not the agent brain.

## Wave 3 — feature-cluster decisions (ANSWERED, revised-scope lens)

| # | Cluster | Decision |
|---|---|---|
| Q10 | Prometheus metrics stack | **#1 KEEP** — cluster runs Grafana; long-lived service |
| Q11 | Share-a-chat public link | **#2 KEEP** |
| Q12 | Provenance / reproducibility | **#1 KEEP** |
| Q13 | Workspace retention/GC | **#1 KEEP** (sanity-check caps are generous) |
| Q14 | K8s orchestrator + resilience | **#1 KEEP WHOLE** — expensive multi-hour GPU sweeps |
| Q15 | Gateway-API readiness depth | **#1 KEEP** — real inference-cluster gateways |
| Q16 | Multi-chat + reconnect/resume | **#1 KEEP** |
| Q17 | 4 non-agent-cited knowledge docs | **#1** — move `workspace_lifecycle`/`packaging`/`logging` → `docs/`; `api_trust.md` DELETED with auth (see Q18) |
| Q18 | Auth / rate-limit / quotas | **#2 REMOVE all three** — trusted in-cluster endpoint |

## FINAL simplification scope (what gets implemented)

Delivery: one branch (`worktree-simplify-yagni`), one phased commit per item, single draft
PR at the end; local finish loop runs; **push waits for go-ahead**.

1. **Delete CoT trace** — `app/agent/cot_trace.py` + call sites in `app/agent/loop.py` + its tests. (Write-only, zero readers, dead at any scope.)
2. **Remove `openai` provider** — `app/llm/openai_provider.py`, its dispatch branch in `app/llm/provider.py`, its dedicated tests, and the `LLM_PROVIDER=openai`/`OPENAI_BASE_URL` guidance in `docs/DEPLOYMENT.md` + `docs/CLUSTER_SERVICE_DEPLOY.md` + `.env.example`. Keep `claude-agent-sdk` (default) + `anthropic` (API-key optional).
3. **Remove auth + rate-limit + quotas** — `app/security/auth.py`, `app/security/quota.py`, their wiring in `app/main.py`/`app/web.py`, `.env.example` API-trust block, their tests, and `knowledge/api_trust.md`. **CAVEAT:** verify CORS handling — if CORS lives in `auth.py`, preserve whatever the browser UI needs; only the auth/rate-limit/quota bits go.
4. **Move 3 knowledge docs → `docs/`** — `workspace_lifecycle.md`, `packaging.md`, `logging.md` (operator/dev refs the agent can't act on).
5. ~~**Thin-code fix** — move `recommend_next_steps` to `knowledge/`~~ **REVERSED after reading the code (wave 4): KEEP it.** `recommend_next_steps` is deterministic ranking over schema-validated facts that feeds the results-panel action buttons and has regression tests; moving it to `knowledge/` makes the buttons LLM-decided and trades away determinism (rule #4 > rule #3 here). It's a determinism gate, not a thin-code violation. `capacity/classify_diagnostics` also kept (BUG-030).
6. **Mechanical dedup (~150 LOC, SAFE subset only)** — `execute_llmdbenchmark` description duplication (registry vs schema), `as_dict()` boilerplate → `dataclasses.asdict()` in capacity/readiness verdicts. **Skip** the metric-path/unit-table lists (two audits disagree on whether they're truly duplicative; the shapes differ by semantic — leave them).

Everything else audited is KEPT: it is right-sized for a real-cluster, long-lived,
single-user benchmarking service.

## Wave 4 — post-implementation decisions

- **quota (Q18 revisit):** blast radius is bigger than "surgical" — it threads through the
  `Decision` model, the allowlist governance schema-validator, `_allow()`, `command_exec`,
  `context`, the live `security/allowlist.yaml` cap (`run: quota.per_session: 25`), and
  `test_governance.py`. User confirmed **REMOVE** anyway (own careful commit, timeout_s +
  every other security gate untouched).
- **`recommend_next_steps`:** **KEEP** (reversed — see item 5 above).
- **Integration / merge:** main is being **actively restructured in parallel** (docs reorg
  committed at `6104a82`; a large `knowledge/` reorg is uncommitted-dirty on the main tree).
  Decision: **HOLD the branch — do NOT merge now.** Once the user's reorg lands on main, rebase
  `worktree-simplify-yagni` onto the new main, **remap the flat-`docs/` + `knowledge/` edits to
  the new subfolder layout**, let the merge-gate hook run ruff+pytest, then `--no-ff` merge.
  **Push/PR still waits for explicit go-ahead.**
- **Verification status:** the branch is committed but NOT yet test-verified — manual pytest/ruff
  are hook-blocked; the suite runs only at the merge gate, which is deferred. Pyright flagged a
  possible loose end in `tests/test_llm_caching_usage.py` from the openai removal — re-check at
  rebase time.

## Commits on `worktree-simplify-yagni` (as of wave 4)
- `bb3bd4d` docs: relocate operator/dev reference docs out of the agent knowledge brain
- `c38fbf5` refactor: delete unused chain-of-thought trace (write-only, no readers)
- `eada889` refactor: drop the openai/vLLM LLM provider (Claude-only: agent-sdk + anthropic)
- `db46f99` refactor: remove optional Bearer auth + rate-limiting (single-user in-cluster service)
- `6139249` refactor: dedup execute tool description + as_dict boilerplate
- _(pending)_ refactor: remove per-session/per-day command usage quotas
