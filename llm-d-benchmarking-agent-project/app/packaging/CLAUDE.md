# app/packaging/ — deploy-artifact contract + shareable HTML/gist export

Two pure-mechanism jobs: (1) `assets.py` is the **single source of truth** that asserts the deploy artifacts
(Dockerfile / Helm / Kustomize under `deploy/`) agree with the running app — container port, health/ready/
metrics paths, least-privilege RBAC; (2) the other three modules render **self-contained, zero-external-asset
HTML** (provenance report card, shared-chat viewer) and publish a chat as a **secret GitHub gist**. The
whether/how-to-deploy *judgment* lives in `knowledge/packaging.md`. (Note: this is NOT where deploy YAML is
generated — that's `deploy/`; this package only *asserts the contract* with it.)

## Invariants (don't break)
- **`assets.py` is the contract hub.** `ORCHESTRATOR_RBAC_RULES` is hand-derived from the verbs in
  `app/orchestrator/kube.py` (`RealKubeClient`) — **no Secrets, no configmap delete**. If the orchestrator
  gains a kube verb, update this tuple or in-cluster runs hit Forbidden. Tests pin artifact↔constant agreement
  and the port/path agreement with `app/main.py`.
- **`gist_publish` writes the `<token>.gist` mapping the MOMENT the gist is created, BEFORE deriving render
  URLs** — so a transient failure can't orphan an unrevocable gist. Idempotent per token; `gh` runs as fixed
  argv `shell=False`; the token lives in `gh` config, never in argv/env. `_GIST_DESC` + the `<token>.gist`
  filename are a shared contract with `scripts/publish_shared_chat.sh`.
- **Both HTML renderers guarantee "zero external assets"** and fail LOUD rather than ship a broken file:
  `shared_chat` raises if `ui/index.html` lost its CSS/JS refs or `app.js` lost the `__LLMD_SHARED__` boot
  marker; both escape `</script>` breakout (incl. U+2028/U+2029). Only `_PUBLIC_FIELDS` are embedded —
  deliberately NOT `source_session_id`. `report_card` HTML-escapes every value and never fabricates a metric.
- **Dependency direction: packaging → storage** (imports `ShareStore`), never the reverse. Publishing is an
  outward, owner-gated, user-triggered action — never an autonomous agent tool.

## Key files
- `assets.py` — deploy-artifact constants + `required_rbac_rules` / `deploy_dir` / `helm_chart_dir` / `kustomize_base_dir`.
- `gist_publish.py` — `publish` / `revoke` (secret gist via `gh`). · `report_card.py` — `render_report_card`.
- `shared_chat.py` — `render_shared_chat` (+ `python -m app.packaging.shared_chat` CLI).

## Scoped tests
```bash
pytest tests/test_packaging.py tests/test_gist_publish.py tests/test_report_card.py tests/test_shared_chat_export.py
```
