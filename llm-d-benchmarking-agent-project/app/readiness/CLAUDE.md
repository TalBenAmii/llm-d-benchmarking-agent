# app/readiness/ — "is the inference endpoint actually serving?"

Endpoint/stack readiness as one deep module with a clean seam: **`diagnostics.py` does ZERO I/O** (pure
analysis of `kubectl get -o json` strings → structured verdicts, fully unit-testable on canned JSON) under
a thin probe layer **`probes.py` that does ALL I/O** (read-only policy-allowed `kubectl`/`curl` via
`ToolContext.run_readonly`). Goes beyond pod-presence: gates on a Service having a *ready backing endpoint
address*, classifies a Running-but-NotReady pod's model-load signals, and reads the Gateway-API control
plane. The wait-vs-stand-up-vs-config-error *judgment* is deferred to `knowledge/readiness_probes.md` /
`knowledge/gateway_readiness.md` / `knowledge/preconditions.md`.

## Invariants (don't break)
- **The Kubernetes endpoint-address readiness is the authoritative gate**; the CLI `run --list-endpoints`
  count is corroborating only.
- **Every analyzer degrades gracefully on empty/garbage input — NEVER raises** (`_parse_items` swallows
  JSON errors → `[]`; `_as_int` coerces forged counts to 0). The default `kubernetes` Service is skipped.
- All probes are **read-only, auto-run, best-effort** — a kubectl/curl failure degrades, never breaks the
  gate. `_gateway_readiness` reads its four objects concurrently; gatewayclass is cluster-scoped (NO `-n`);
  curl tries port 8000 (prefill) then 8200 (decode), per `_ROLE_BY_PORT`.
- `control_plane_ready` / `not_ready_reason` are DERIVED FACTS, not decisions. When not ready, the result
  carries a `standup_suggestion` that is an **OFFER** (`approval_required: True`) — never an action.
- `now` is injectable on the age helpers for deterministic tests.

## Key files
- `diagnostics.py` — pure: `EndpointReadiness` / `GatewayReadiness` / `ServingReadiness` + `analyze_endpoints`,
  `analyze_gateway`, `classify_serving_readiness`.
- `probes.py` — the `check_endpoint_readiness` tool handler (does the I/O, feeds the analyzers).
- `__init__.py` — re-exports both surfaces.

## Scoped tests
```bash
pytest tests/orchestrator/test_endpoint_readiness.py tests/orchestrator/test_gateway_readiness.py tests/orchestrator/test_serving_readiness.py
```
