# app/validation/ — the determinism gates

This is where "determinism via validation, not scripting" is implemented. The LLM is
constrained at the boundaries; the gates here are the boundaries.

## The gates implemented here
**This list is the single source of truth for "the four determinism gates"** — the root `CLAUDE.md`
and `docs/reference/CONTEXT.md` point here, in this order (a–d).

1. **Tool-arg schema gate** (a) — every tool input is a Pydantic model validated at `dispatch()` (see
   `app/tools/`). Schemas are the contract; args are never scraped from prior text.
2. **SessionPlan + catalog gate** (b) (`session_plan.py::validate_plan`) — the plan's spec/harness/workload
   must exist in the **live catalog**; namespace must be RFC1123. Reject with a catalog hint on mismatch —
   never silently default. The approval is wired in `app/tools/setup/plan.py` + the loop.
   **What this gate is not:** a precondition on mutation. Nothing keys off `session.approved_plan` (written
   in `agent/loop.py`; read only as a namespace fallback in `agent/session.py`). The plan is a *second human
   checkpoint*; what actually stops an unapproved mutation is the **per-command approval gate**
   (`tools/command_exec.py`, `tools/run/shell.py`). "Plan first" ordering comes from the system prompt + the
   skill-grounding gate. If you want it to be a hard precondition, it has to be written.
3. **DoE / generated-config gate** (c) (`doe.py`) — `build_doe_experiment()` is a **pure** cross-product
   (no benchmarking judgment); `validate_structure()` (in `app/tools/run/doe.py`) checks the emitted YAML
   against the repo's format.
4. **Report gate** (d) (`report.py::load_report` + `validate_report`) — results come **only** from a
   schema-validated **Benchmark Report v0.2**, never scraped from logs.

**Not a gate:** previewing a config through the CLI's own `--dry-run`/`plan` is asked for by the prompt
and the tool descriptions, but **nothing in code enforces it** (`config_artifact.py` returns
`"structural check only in MVP; deep validation via CLI --dry-run is deferred"`). Older docs listed it as
gate (c); they were wrong. Treat it as an agent convention — if you want it to be a gate, it has to be
written.

## Local invariants
- **Never fabricate numbers.** `summarize_report()` omits absent fields; it never invents latency/
  throughput. SLO/goodput (`analysis.py`) use only *reported* percentile rungs — goodput is an
  upper-bound *estimate* (returned with `is_estimate=True` + method) because the report hides per-request
  correlation.
- **Keep the full percentile ladder** (`report.py` `_PCTL_KEYS`, `analysis.py`). Dropping a low rung
  (p0p1, p1) silently floors sub-p50 SLO targets to 0% goodput — there's a regression test for exactly this.
- **Fail loud, but degrade gracefully on newer reports.** A report newer than the committed schema
  surfaces as **non-fatal** deviations — the raw jsonschema messages, exposed under `schema_deviations`
  (`app/tools/analyze/report_locate.py`) — not a silent drop and not a crash.
- **Unit tables are string-matched** (`analysis.py` `_TO_MS`/`_TO_TOK_S`). An unknown unit → `met=None`
  (unchecked, not failed). Add new harness units to the tables.
- **`SessionPlan.flags` is intentionally untyped** — per-tool validation owns flag shape (thin code, thick agent).

## Key files
- `session_plan.py` — SessionPlan model + catalog validation (gate 1).
- `report.py` — BR v0.2 load / validate / parse / summarize (gate 4).
- `report_metrics.py` — §3.4 standard-metric + session-performance extraction (split from `report.py`; owns `_PCTL_KEYS`/`_stat`).
- `doe.py` — pure DoE cross-product + structural validation (gate 3).
- `analysis.py` — SLO evaluation, goodput estimation, Pareto/DoE analysis (consumes gate 4; no new gate).

## Scoped tests
```bash
pytest tests/agent/test_sessions.py tests/tools/test_schemas.py tests/tools/test_report_validation.py \
       tests/tools/test_analyze.py tests/tools/test_doe.py tests/platform/test_session_performance.py
```
