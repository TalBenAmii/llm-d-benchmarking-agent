# app/storage/ — disk-backed persistence under <workspace>/

Four best-effort, defensive stores under the shared `<workspace>/`: validated benchmark **history** + pure
trend math, reproducibility **provenance** bundles, read-only shared-chat
**share** snapshots, and workspace **retention**/GC + a startup self-check. Pure mechanism — every store is
defensive I/O; all "is this a regression / what to prune" *judgment* lives in
`knowledge/` + the LLM.

## Shared pattern (all four stores)
Filesystem-safe id/token guard **before** any path use → atomic temp-then-replace write → defensive read that
skips corrupt files rather than crash. All share one workspace root (`settings.resolved_workspace_dir`).

## Invariants (don't break)
- **`retention.py` is the coupling hub:** `MANAGED_AREAS` must stay in sync with where each store writes
  (`history/`, `shares/`, orchestrator `jobs/`). It **NEVER prunes an active/running session**
  (active ids passed in, skipped first). Caps are DATA on
  `Settings` (0/None = unlimited); `_select_for_removal` is pure (oldest-first). Self-check probes (`_CHECKS`)
  are a callable list — adding one is a list edit, not a branch — and hermetic (config-only). `readiness` feeds `/readyz`.
- **Content-addressing mirrors across history↔provenance:** `compute_record_id` (history) and
  `_compute_bundle_id` (provenance) deliberately mirror each other's collision-avoidance (the report digest is
  mixed in so two `run_uid=None` runs to a reused path don't silently overwrite). `bundle_id` / `provenance_view`
  thread onto a `HistoryRecord`. Re-storing the same report is idempotent.
- **`provenance.knowledge_hash` glob (`*.md`/`*.yaml`/`*.yml` minus `CLAUDE.md`/`README.md`) MUST mirror**
  `app/agent/prompt.py::_knowledge_sections` + `knowledge_access.EXCLUDED_KNOWLEDGE_FILES`. `build_bundle`
  refuses to certify an unvalidated report (`InvalidReportError`); a missing/empty repo (worktree case) degrades
  to `{unavailable: True}` — never fabricates a SHA.
- **`share.py` tokens are uuid4 hex** (the unguessable bearer credential); `_TOKEN_RE`
  guards traversal before disk; `source_session_id` is kept server-side, never echoed to the public viewer.

## Key files
- `history.py` (`HistoryStore`, `trend`) · `provenance.py` (`BundleStore`, `build_bundle`, `knowledge_hash`)
- `retention.py` (`run_gc`, `self_check`, `readiness`) · `share.py` (`ShareStore`, `is_valid_token`)

## Scoped tests
```bash
pytest tests/test_history.py tests/test_provenance.py tests/test_retention.py tests/test_share.py
```
