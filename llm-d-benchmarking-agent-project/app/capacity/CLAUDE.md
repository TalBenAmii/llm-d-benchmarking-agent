# app/capacity/ — capacity pre-flight (feasibility check at the plan gate)

The **pure, no-I/O half** of the capacity pre-flight: render the exact `plan_config` the benchmark
repo's standup would build (scenario deep-merged over repo defaults + agent overrides), then classify
the repo planner's own flat diagnostic strings into a structured `CapacityVerdict`. The subprocess that
actually invokes the repo planner lives in `app/tools/setup/capacity.py`; **this package touches no
network/cluster/subprocess** — it reads only on-disk repo truth. The verdict-reading *judgment* (PUBLIC
vs GATED+AUTHORIZED, what to change to make a plan feasible, whether to offer secret-provisioning) lives
in `knowledge/capacity.md`.

## Invariants (don't break)
- **`_deep_merge` must match the repo's own `RenderSpecification.deep_merge` byte-for-byte** — in
  particular a `None` override value is **SKIPPED**, never written. A bare YAML key (`decode:`) parses to
  `None`; clobbering the rich default block with it crashes the upstream planner and spuriously bypasses sizing.
- **Feasibility keys on the repo's OWN marker strings, not on absence of facts** (`classify_diagnostics`):
  `feasible=False` on any `DEPLOYMENT WILL FAIL` / `ERROR:` / GPU-shortfall line; `feasible=None`
  (inconclusive) only on POSITIVE bypass evidence (fma-skip / gpu-or-size-skip / 0-replica-skip with
  nothing else sized); else `feasible=True`. All marker constants are faithful echoes of the repo's
  `capacity_validator.py` log strings — read, never invented.
- The GPU-shortfall marker is treated as **hard-fail** even though upstream only WARNs it under
  `ignoreFailedValidation` (BUG-030 false-fit defense).
- `apply_overrides` keeps `model.huggingfaceId` in lockstep when only `model` is overridden (both the
  sizing path and the gating check prefer `huggingfaceId`); unknown override keys raise `CapacityError`
  (loud, no silent no-op); `_OVERRIDE_PATHS` is the closed allowlist of what an override may touch.

## Thin-code watch
`classify_diagnostics` looks judgment-adjacent (feasible / infeasible / inconclusive) but is **faithfully
transcribing upstream's halt contract**, not inventing sizing policy. Keep it that way: new sizing rules
belong in the repo / `knowledge/`, never here.

## Key files
- `planner.py` — `plan_config_for_spec` (render), `classify_diagnostics` / `merge_gated_access` →
  `CapacityVerdict`, `resolve_scenario_file`, `apply_overrides`, `CapacityError`.
- `__init__.py` — re-exports the public surface above.

## Scoped tests
```bash
pytest tests/orchestrator/test_capacity.py tests/orchestrator/test_capacity_gated.py
```
