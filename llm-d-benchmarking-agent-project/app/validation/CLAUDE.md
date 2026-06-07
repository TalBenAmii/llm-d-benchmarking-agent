# app/validation/ — the determinism gates

This is where "determinism via validation, not scripting" is implemented. The LLM is
constrained at the boundaries; the gates here are the boundaries.

## The gates implemented here
1. **SessionPlan + catalog gate** (`session_plan.py::validate_plan`) — the plan's spec/harness/workload
   must exist in the **live catalog**; namespace must be RFC1123. No mutation runs until a SessionPlan
   is approved (the approval itself is wired in `app/tools/plan.py` + the loop). Reject with a catalog
   hint on mismatch — never silently default.
2. **Tool-arg schema gate** — every tool input is a Pydantic model validated at `dispatch()` (see
   `app/tools/`). Schemas are the contract; args are never scraped from prior text.
3. **DoE / generated-config gate** (`doe.py`) — `build_doe_experiment()` is a **pure** cross-product
   (no benchmarking judgment); `validate_structure()` checks the emitted YAML against the repo's format.
4. **Report gate** (`report.py::load_report` + `validate_report`) — results come **only** from a
   schema-validated **Benchmark Report v0.2**, never scraped from logs.

## Local invariants
- **Never fabricate numbers.** `summarize_report()` omits absent fields; it never invents latency/
  throughput. SLO/goodput (`analysis.py`) use only *reported* percentile rungs — goodput is an
  upper-bound *estimate* (returned with `is_estimate=True` + method) because the report hides per-request
  correlation.
- **Keep the full percentile ladder** (`report.py` `_PCTL_KEYS`, `analysis.py`). Dropping a low rung
  (p0p1, p1) silently floors sub-p50 SLO targets to 0% goodput — there's a regression test for exactly this.
- **Fail loud, but degrade gracefully on newer reports.** A report newer than the committed schema
  surfaces as a **non-fatal** deviation flagged in the summary ("report is newer than the schema…"),
  not a silent drop and not a crash.
- **Unit tables are string-matched** (`analysis.py` `_TO_MS`/`_TO_TOK_S`). An unknown unit → `met=None`
  (unchecked, not failed). Add new harness units to the tables.
- **`SessionPlan.flags` is intentionally untyped** — per-tool validation owns flag shape (thin code, thick agent).

## Key files
- `session_plan.py` — SessionPlan model + catalog validation (gate 1).
- `report.py` — BR v0.2 load / validate / parse / summarize / metric extraction (gate 4).
- `doe.py` — pure DoE cross-product + structural validation (gate 3).
- `analysis.py` — SLO evaluation, goodput estimation, Pareto/DoE analysis (consumes gate 4; no new gate).

## Scoped tests
```bash
pytest tests/test_sessions.py tests/test_schemas.py tests/test_report_validation.py \
       tests/test_analyze.py tests/test_doe.py tests/test_session_performance.py
```
