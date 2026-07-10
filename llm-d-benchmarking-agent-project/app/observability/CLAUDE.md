# app/observability/ — dependency-free metrics + structured logging + traces

Mechanism only — a tiny **dependency-free** metrics registry + Prometheus text exposition (stdlib only: NO
`prometheus_client`/structlog), structured JSON logging with per-turn correlation ids, the agent/orchestrator
metric definitions wired into central record points, a per-session chain-of-thought debug trace, and a
backend-only live resource poller. What a metric *means* / when to act on it lives in `knowledge/observability.md`.

## Invariants (don't break)
- **`metrics.REGISTRY` is the process-wide singleton** the `/metrics` endpoint renders. Tests must isolate
  via `use_registry()` / `bind_registry()` — never mutate the global. Re-registering a name with a different
  type raises; same-type returns the existing handle (idempotent). The registry is **thread-safe** via
  `registry.lock` (parallel sweeps/sessions record concurrently); exposition is sorted/deterministic.
- **The dependency-free claim is load-bearing** — `metrics.py` / `logging.py` are hand-rolled (`JsonFormatter`,
  no json-logger). `setup_logging` is idempotent (replaces, doesn't stack root handlers); call once at the
  FastAPI lifespan. `LOG_CONTEXT_FIELDS` is the shared field order the formatter + `ContextFilter` + `bind`
  all read — keep them aligned. `bind` restores prior values exactly by token (nesting/concurrency safe).
- **`resource_poller` is self-limiting:** no-op when `ctx.emit is None` or `ctx.settings.simulate`; after
  `_MAX_CONSECUTIVE_FAILURES` (3) bad polls it STOPS `kubectl top` for the rest of the run (a good sample
  re-arms). Never enters the LLM stream; a failing poll never breaks the benchmark. `RESOURCE_STATS` is a bare
  string literal (NOT imported from `app.agent.events`) deliberately to avoid an import cycle — the UI and tests
  match the same literal. It reuses the private `app.tools.run.manage_runs._parse_top_table` — keep it stable.
- `cot_trace.event` never raises into the caller (best-effort, swallowed); a `disabled()` instance is a cheap
  no-op; `_clip` truncates oversized model-emitted bodies (`_BODY_LIMIT` 200k, depth-bounded).

## Key files
- `metrics.py` — `MetricsRegistry` / `Counter` / `Gauge` / `Histogram` + `render_prometheus`; also owns the
  metric defs + `record_*` helpers, the process-wide `REGISTRY`, and `bind_registry` / `use_registry`.
- `logging.py` (`setup_logging`, `JsonFormatter`); also owns the per-turn correlation context
  (`bind`, `new_corr_id`, `get_corr_id`, `LOG_CONTEXT_FIELDS`).
- `cot_trace.py` (`TurnTrace`) · `resource_poller.py` (`resource_stats_poller`).

## Scoped tests
```bash
pytest tests/test_metrics.py tests/test_logging.py tests/test_observability.py tests/test_cot_trace.py tests/test_resource_poller.py
```
