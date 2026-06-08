# metrics-server Pre-flight Check + Agent Offer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect the in-cluster metrics-server deterministically *before* a benchmark run (a probe fact), have the agent offer the approval-gated install in chat via a HARD_RULE, and retire the colliding mid-run install button.

**Architecture:** Add a fact-only `metrics_server` check to `probe_environment` (mechanism, mirrors `_probe_prometheus_crds`) and to the connect-time pre-warm list, so the fact is present from turn 1. Add one `HARD_RULES` line driving the pre-run offer (judgment stays in prompt/knowledge). Replace the busy-only UI install button with a passive note and remove its now-dead queueing infra.

**Tech Stack:** Python (FastAPI backend, `app/tools/probe.py`, `app/agent/prompt.py`), markdown/yaml knowledge files, vanilla JS UI (`ui/app.js`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-08-metrics-server-preflight-offer-design.md`

**Test command (run from the worktree — primary `.venv`, worktree `PYTHONPATH`, populated sibling repos):**
```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight/llm-d-benchmarking-agent-project
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/ -q
```
Healthy baseline ≈ 1820 passed / ~38 skipped in ~15–40s. (Both probe argvs — `kubectl top nodes` and `kubectl get deployment -n kube-system -l k8s-app=metrics-server -o json` — were verified `read_only` against the real allowlist; no allowlist change is needed.)

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `app/tools/probe.py` | read-only environment facts | add `metrics_server` to `_ALL_CHECKS`, a `_probe_metrics_server` helper, an `_items_from_json` helper, and wiring in `probe_environment` |
| `app/main.py` | connect-time pre-warm probe | add `"metrics_server"` to the `_prewarm_env` checks list |
| `tests/test_metrics_server_probe.py` | NEW — probe-fact unit tests + HARD_RULE guard | 3 cluster states + no-kubectl + rule-present assertion |
| `app/agent/prompt.py` | system prompt HARD_RULES | one new rule: offer install before the run when `metrics_server.available==false` on kind |
| `knowledge/quickstart_playbook.md` | quickstart judgment (CORE) | tie step 5b trigger to the probe fact + the run boundary |
| `knowledge/observability.md` | metrics-server judgment (on-demand) | key the offer off the probe fact (pointer tweak) |
| `ui/app.js` | chat UI | unavailable-panel button → passive note; remove dead queue infra (`sendOrQueueUserMessage`/`flushPendingUserSend`/`pendingUserSend`/`metricsInstallRequested`) |
| `ui/styles.css` | UI styles | drop `.resource-fix-btn`; add a small `.resource-note-hint` |
| `ui/preview.html` | static UI mock | update the 6a comment (button → passive note) |
| `tests/test_ui_frontend.py` | UI string contract | rewrite the offer test to assert the hint; DELETE the queue regression test |
| `tests/test_static_cache.py` | on-the-wire asset guard | repoint the marker from the button to the hint |
| `FEATURES.md` | feature inventory | refresh rows 181-182 to mention the proactive pre-run offer |

---

## Task 1: `metrics_server` probe fact (backend mechanism)

**Files:**
- Modify: `app/tools/probe.py` (`_ALL_CHECKS` ~line 34; `probe_environment` ~line 118; new helpers)
- Modify: `app/main.py:662-664` (`_prewarm_env` checks list)
- Test: `tests/test_metrics_server_probe.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_metrics_server_probe.py`:

```python
"""metrics-server pre-flight fact in probe_environment — the deterministic "do we have live
resource stats?" signal that lets the agent OFFER the install BEFORE a run (instead of the old
mid-run button that collided with the in-flight-turn guard).

MECHANISM ONLY: facts (available/installed/ready_replicas). WHETHER/when to offer the install is
the agent's HARD_RULE + knowledge/observability.md — there is no install branch in the probe.
No live cluster, no network — `kubectl` is mocked via shutil.which + a CaptureRunner."""
from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.prompt import HARD_RULES
from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext
from app.tools.probe import probe_environment
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CannedResult, CaptureRunner

_DEPLOY_READY = json.dumps({"items": [{"metadata": {"name": "metrics-server"},
                                       "status": {"availableReplicas": 1}}]})
_DEPLOY_NOTREADY = json.dumps({"items": [{"metadata": {"name": "metrics-server"},
                                          "status": {"availableReplicas": 0}}]})
_DEPLOY_ABSENT = json.dumps({"items": []})
_GET_DEPLOY_ARGV = ["kubectl", "get", "deployment", "-n", "kube-system",
                    "-l", "k8s-app=metrics-server", "-o", "json"]


def _ctx(tmp_path, *, canned):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_metrics_server_available(tmp_path):
    """`kubectl top nodes` succeeds → available True; the Deployment is present + ready."""
    ctx, runner = _ctx(tmp_path, canned={
        "top nodes": "NAME   CPU   MEM\nnode1  100m  500Mi\n",
        "get deployment": _DEPLOY_READY,
    })
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": True, "installed": True, "ready_replicas": 1}
    argvs = [c["argv"] for c in runner.calls]
    assert ["kubectl", "top", "nodes"] in argvs
    assert _GET_DEPLOY_ARGV in argvs  # label-selector form (get permits one positional)


async def test_metrics_server_absent(tmp_path):
    """No metrics-server: `kubectl top` fails (Metrics API not available) and no Deployment."""
    ctx, _ = _ctx(tmp_path, canned={
        "top nodes": CannedResult(output="error: Metrics API not available", exit_code=1),
        "get deployment": _DEPLOY_ABSENT,
    })
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": False, "installed": False, "ready_replicas": None}


async def test_metrics_server_installed_but_not_ready(tmp_path):
    """kind gotcha: installed WITHOUT --kubelet-insecure-tls — Deployment exists but
    availableReplicas 0 and `kubectl top` still fails, so the agent can phrase it precisely."""
    ctx, _ = _ctx(tmp_path, canned={
        "top nodes": CannedResult(output="error: Metrics API not available", exit_code=1),
        "get deployment": _DEPLOY_NOTREADY,
    })
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": False, "installed": True, "ready_replicas": 0}


async def test_metrics_server_no_kubectl(tmp_path):
    """No kubectl on PATH → degrade to all-absent, no raise, no command issued."""
    ctx, runner = _ctx(tmp_path, canned={})
    with patch("app.tools.probe.shutil.which", return_value=None):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": False, "installed": False, "ready_replicas": None}
    assert runner.calls == []


def test_hard_rule_drives_the_pre_run_offer():
    """The offer is guaranteed by a HARD_RULE (not buried playbook prose): the system prompt
    references the probe fact AND the vetted install command, so the agent offers before running."""
    assert "metrics_server" in HARD_RULES
    assert "install_metrics_server.sh" in HARD_RULES
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight/llm-d-benchmarking-agent-project
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/test_metrics_server_probe.py -q
```
Expected: FAIL — `KeyError: 'metrics_server'` (probe doesn't emit the fact yet) and the HARD_RULE assertion fails (`"metrics_server" not in HARD_RULES`). The HARD_RULE test is fixed in Task 2; the probe tests are fixed by Step 3.

- [ ] **Step 3: Add the probe helper + wiring in `app/tools/probe.py`**

In `_ALL_CHECKS` (currently lines 34-41), insert `"metrics_server"` right after `"prometheus_crds"`:

```python
_ALL_CHECKS = [
    "container_runtime", "repos", "tools", "venv",
    "kind_clusters", "kube_context", "cluster_info", "namespaces", "stack",
    "prometheus_crds",
    "metrics_server",
    "node_capacity",
    "cluster_preconditions",
    "provider_detection",
]
```

In `probe_environment`, immediately after the `prometheus_crds` block (currently lines 118-119), add:

```python
    if "metrics_server" in wanted:
        out["metrics_server"] = await _probe_metrics_server(ctx)
```

Add the helper next to `_probe_prometheus_crds` (after it, ~line 252):

```python
async def _probe_metrics_server(ctx: ToolContext) -> dict[str, Any]:
    """Detect whether the in-cluster **metrics-server** is present and serving — the add-on that
    powers the live CPU/memory panel (``kubectl top``). kind and the ``cicd/kind`` spec do NOT
    install it, so on a fresh kind cluster live stats are unavailable until it is added (on kind,
    with ``--kubelet-insecure-tls``).

    PURE MECHANISM — facts only, never a verdict and never the install decision:
      - ``available``       ``kubectl top nodes`` exits 0 (metrics actually flowing — the SAME
                            signal the live resource poller uses during a run).
      - ``installed``       the metrics-server Deployment exists in kube-system (queried by LABEL,
                            since ``kubectl get`` permits a single positional — ``get deployment
                            metrics-server`` would be two positionals and the allowlist rejects it).
      - ``ready_replicas``  the Deployment's ``status.availableReplicas`` (0 == installed-but-
                            NotReady, the kind missing-``--kubelet-insecure-tls`` case), else None.

    WHETHER/when to OFFER the install (and the ``--kubelet-insecure-tls`` / GKE-OpenShift SKIP
    judgment) is the agent's, grounded in knowledge/observability.md — there is NO install branch
    here. Mirrors ``_probe_prometheus_crds``: never raises, the cluster is only read, and it
    degrades to all-absent when kubectl is missing / no cluster is reachable."""
    if not shutil.which("kubectl"):
        return {"available": False, "installed": False, "ready_replicas": None}
    top = await ctx.run_readonly(["kubectl", "top", "nodes"], timeout=12.0)
    dep = await ctx.run_readonly(
        ["kubectl", "get", "deployment", "-n", "kube-system",
         "-l", "k8s-app=metrics-server", "-o", "json"], timeout=12.0)
    installed = False
    ready_replicas: int | None = None
    if dep.exit_code == 0:
        items = _items_from_json(dep.output)
        if items:
            installed = True
            ready_replicas = items[0].get("status", {}).get("availableReplicas", 0) or 0
    return {
        "available": top.exit_code == 0,
        "installed": installed,
        "ready_replicas": ready_replicas,
    }
```

Add the small JSON helper next to `_names_from_json` (~line 426):

```python
def _items_from_json(text: str) -> list[dict[str, Any]]:
    """``.items`` from a ``kubectl get … -o json`` list, defensively (returns [] on bad JSON)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = data.get("items", []) if isinstance(data, dict) else []
    return [it for it in items if isinstance(it, dict)]
```

- [ ] **Step 4: Add `metrics_server` to the connect-time pre-warm list in `app/main.py`**

In `_prewarm_env` (lines 661-664), add `"metrics_server"` so the fact is in the turn-1 pre-probe snapshot:

```python
            s.env_snapshot = await probe_environment(s.ctx, checks=[
                "container_runtime", "repos", "tools", "venv",
                "kind_clusters", "kube_context", "cluster_info", "namespaces",
                "metrics_server",
            ])
```

- [ ] **Step 5: Run the probe tests to verify they pass**

```bash
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/test_metrics_server_probe.py -q
```
Expected: the 4 probe tests PASS; `test_hard_rule_drives_the_pre_run_offer` still FAILS (fixed in Task 2).

- [ ] **Step 6: Commit**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight
git add llm-d-benchmarking-agent-project/app/tools/probe.py \
        llm-d-benchmarking-agent-project/app/main.py \
        llm-d-benchmarking-agent-project/tests/test_metrics_server_probe.py
git commit -m "feat(probe): metrics_server pre-flight fact (available/installed/ready_replicas)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Agent offer — HARD_RULE + playbook + observability pointer

**Files:**
- Modify: `app/agent/prompt.py` (`HARD_RULES`, ends ~line 78)
- Modify: `knowledge/quickstart_playbook.md:45-54` (step 5b)
- Modify: `knowledge/observability.md` (the install-offer section, ~line 67)
- Test: `tests/test_metrics_server_probe.py::test_hard_rule_drives_the_pre_run_offer` (already written in Task 1)

- [ ] **Step 1: Add the HARD_RULE** in `app/agent/prompt.py`

Append this bullet to the end of the `HARD_RULES` string (after the `sanity_random.yaml` bullet, before the closing `"""` on line 79):

```
- Live resource stats (the CPU/memory panel) need the in-cluster metrics-server, which kind and
  the `cicd/kind` spec do NOT install. probe_environment reports it as `metrics_server`
  (`available`/`installed`/`ready_replicas`). On a local kind cluster, BEFORE the first benchmark
  `run`, if `metrics_server.available` is false make a SINGLE one-line offer to install it with
  run_command(["install_metrics_server.sh","--kubelet-insecure-tls"]) and let the user approve it
  BEFORE you run — it is a per-cluster add-on, so one install covers every later run. SKIP the
  offer if it is already available, the user already declined, or it is a managed cluster that
  ships metrics (GKE/OpenShift). Do NOT defer this to a mid-run action. See
  read_knowledge('observability').
```

- [ ] **Step 2: Run the guard test to verify it now passes**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight/llm-d-benchmarking-agent-project
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest \
  tests/test_metrics_server_probe.py::test_hard_rule_drives_the_pre_run_offer -q
```
Expected: PASS.

- [ ] **Step 3: Verify prompt-cache byte-stability still holds**

```bash
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/test_context_mgmt.py -q
```
Expected: PASS unchanged. `test_context_mgmt.py` asserts the prefix is identical *across turns* (and that the catalog body is not inlined); it does NOT pin a literal length/hash of HARD_RULES, so a stable addition keeps it green.

- [ ] **Step 4: Tie playbook step 5b to the probe fact** in `knowledge/quickstart_playbook.md`

Replace the step-5b paragraph (lines 45-54) with:

```markdown
5b. **Live resource stats — OFFER the metrics-server install BEFORE the run (don't wait to be
   asked, don't defer to mid-run).** kind does NOT ship the in-cluster **metrics-server**, so the
   live CPU/mem panel during a run reads `live resource stats unavailable (no metrics-server)`.
   `probe_environment` reports this up front as `metrics_server.available`. On a fresh kind
   cluster where `metrics_server.available == false`, make a single one-line offer to install it
   (approval-gated, idempotent) BEFORE you start the benchmark `run`:
   `run_command argv=["install_metrics_server.sh","--kubelet-insecure-tls"]`
   — `--kubelet-insecure-tls` is REQUIRED on kind (self-signed kubelet certs). It is a
   PER-CLUSTER add-on: install once and every run on this cluster gets stats. Best right after the
   cluster is up; at the latest, before the first run. SKIP if `metrics_server.available` is
   already true (e.g. GKE/OpenShift); full judgment + SKIP cases in `read_knowledge('observability')`.
```

- [ ] **Step 5: Key the observability offer off the probe fact** in `knowledge/observability.md`

In the "Making live stats work" section, change the trigger sentence (currently begins "When `observe_run_metrics` (or the live sparklines) reports `available: false`…", ~line 67) to:

```markdown
When the up-front `probe_environment` `metrics_server` fact (or `observe_run_metrics` / the live
sparklines mid-run) reports `available: false` AND the user wants live resource stats, OFFER to
install it BEFORE the run — proactively, not as a mid-run action — don't do it silently (it is
mutating and approval-gated). The vetted installer is a project script run via `run_command`:
```

And update the timing sentence near the end of that section (currently "Best timing: right after the cluster is created / at standup…") to:

```markdown
The script applies the pinned metrics-server manifest into `kube-system`, waits for the rollout,
and verifies `kubectl top` responds. It is idempotent (safe to re-run). Best timing: right after
the cluster is created, and in any case BEFORE the first benchmark `run`, so the run already has
live stats — keyed off the `probe_environment` `metrics_server.available == false` fact.
```

- [ ] **Step 6: Run the knowledge-scoped checks**

```bash
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest \
  tests/test_knowledge_meta_excluded.py tests/test_deterministic_msgs.py -q
```
Expected: PASS (knowledge files still load; nothing meta leaked).

- [ ] **Step 7: Commit**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight
git add llm-d-benchmarking-agent-project/app/agent/prompt.py \
        llm-d-benchmarking-agent-project/knowledge/quickstart_playbook.md \
        llm-d-benchmarking-agent-project/knowledge/observability.md
git commit -m "feat(agent): HARD_RULE to offer metrics-server install before the run

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Retire the mid-run button → passive note (UI)

**Files:**
- Modify: `ui/app.js` (panel branch ~1051-1082; state ~95-102; `done` handler line 426; `clearResourceStats` ~1107; `sendOrQueueUserMessage`/`flushPendingUserSend` ~2620-2644)
- Modify: `ui/styles.css:492-493`
- Modify: `ui/preview.html:335-340` (comment only)
- Test: `tests/test_ui_frontend.py` (rewrite one test, DELETE one), `tests/test_static_cache.py` (repoint marker)

- [ ] **Step 1: Update the UI string-contract tests to the new behavior (they will fail against old `app.js`)**

In `tests/test_ui_frontend.py`, replace `test_metrics_server_install_offer_on_unavailable_panel` (lines ~109-126) with:

```python
def test_metrics_server_passive_hint_on_unavailable_panel():
    """When the live-resource panel reports unavailable (kind ships no metrics-server) it shows a
    PASSIVE hint — no actionable button. The mid-run install button was retired because it lived in
    a busy-only panel and collided with the in-flight-turn guard ("still working on the previous
    request"); the agent now offers the approval-gated install BEFORE the run (a deterministic
    probe fact + a HARD_RULE). Judgment/approval still live in the agent."""
    js = _ui("app.js")
    html = _ui("preview.html")
    # No clickable install control inside the busy-only panel anymore.
    assert "resource-fix-btn" not in js
    assert "Install metrics-server for live stats" not in js
    # A passive hint explains where live stats come from.
    assert "offers to install it" in js
    # The preview still renders the unavailable state so it's hand-verifiable with no backend.
    assert "available: false" in html and "no metrics-server" in html
```

DELETE the entire `test_metrics_server_button_queues_when_busy` test (lines ~129-148) — it locks the queue-on-busy behavior that is being intentionally removed.

In `tests/test_static_cache.py`, replace `test_served_app_js_carries_the_metrics_server_button` (lines 25-31) with:

```python
def test_served_app_js_carries_the_metrics_server_hint():
    # Guards that the asset on the wire is current. The mid-run install BUTTON was retired (it
    # collided with the in-flight-turn guard); the agent now offers the install before the run, so
    # the unavailable panel shows a passive hint instead.
    with TestClient(main.app) as client:
        body = client.get("/static/app.js").text
    assert "resource-fix-btn" not in body
    assert "offers to install it" in body
```

- [ ] **Step 2: Run the UI tests to verify they fail**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight/llm-d-benchmarking-agent-project
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest \
  tests/test_ui_frontend.py tests/test_static_cache.py -q
```
Expected: FAIL — old `app.js` still contains `resource-fix-btn` / the button text and lacks "offers to install it".

- [ ] **Step 3: Replace the unavailable-panel branch in `ui/app.js`**

Replace the whole `if (data.available === false) { … return; }` block (lines 1051-1082) with:

```javascript
  if (data.available === false) {
    body.appendChild(el("div", "resource-note", data.note || "live resource stats unavailable"));
    // No actionable control here: this panel is shown ONLY during a run, so a button would collide
    // with the backend's single-turn-in-flight guard ("still working on the previous request").
    // Live stats need the in-cluster metrics-server, which the agent now PROACTIVELY offers to
    // install BEFORE the run — driven by a deterministic probe fact (app/tools/probe.py
    // `metrics_server`) + a HARD_RULE (app/agent/prompt.py). A passive hint is enough here.
    body.appendChild(el("div", "resource-note resource-note-hint",
      "Live CPU/memory needs the in-cluster metrics-server — the assistant offers to install it " +
      "before a run."));
    return;
  }
```

- [ ] **Step 4: Remove the now-dead queueing infra in `ui/app.js`**

(a) Delete the comment block + state at lines 95-102 (keep `let busy = false;` on line 94). Remove these lines:

```javascript
// A queued user message to auto-send the instant the current turn ends. Some UI actions can ONLY be
// clicked WHILE a turn is in flight (busy) — notably the "Install metrics-server" button, which lives
// on the live-resource panel that is shown ONLY during a benchmark run. sendUserMessage() refuses to
// send while busy, so without this the click would silently no-op. The agent is blocked executing the
// run mid-turn and can't act anyway, so we remember the request and flush it on `done` (see
// sendOrQueueUserMessage / flushPendingUserSend). Shape: {text, session}.
let pendingUserSend = null;
let metricsInstallRequested = false;  // sticky "metrics-server install queued" flag (survives panel re-renders)
```

(b) In the `done` case (line 426), remove the ` flushPendingUserSend();` call. The line becomes:

```javascript
    case "done": resetStreamBubble(); setEnabled(true); activeConsole = null; if (cur) cur.running = false; clearPhaseActive(); appendTurnTokens(); clearResourceStats(); loadSessions(); loadHistory(); stopWorking(); break;
```

(c) In `clearResourceStats` (lines 1106-1107), remove the `metricsInstallRequested = false;` line and its comment so it reads:

```javascript
function clearResourceStats() {
  resourceActive = false;
  if (cur) cur.resourceActive = false;
  renderResourceSide();
}
```

(d) Delete both functions and their doc comments (lines 2620-2644): `sendOrQueueUserMessage` and `flushPendingUserSend`. (Grep confirmed these and `pendingUserSend`/`metricsInstallRequested` have no other callers.)

- [ ] **Step 5: Drop the button CSS and add the hint style in `ui/styles.css`**

Replace lines 492-493:

```css
/* One-click "Install metrics-server" offer shown under the unavailable note (reuses .chip). */
.resource-fix-btn { margin-top: 8px; font-size: 12px; }
```
with:
```css
/* Passive hint under the "unavailable" note — the agent offers the metrics-server install pre-run. */
.resource-note-hint { margin-top: 6px; opacity: .85; }
```

- [ ] **Step 6: Update the preview comment in `ui/preview.html`**

Replace the 6a comment (lines 335-339) with:

```html
    // 6a) The "no metrics-server" state — kind ships without it, so this is what a quickstart run
    // shows. The panel now shows a PASSIVE hint (no button): the agent proactively offers the
    // approval-gated install BEFORE the run (deterministic probe fact + HARD_RULE), so a mid-run
    // button is no longer needed. Rendered LAST so the unavailable state is the visible panel here.
```

(The `A.renderResourceStats({ available: false, … })` call below stays — it now renders the hint.)

- [ ] **Step 7: Run the UI tests to verify they pass**

```bash
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest \
  tests/test_ui_frontend.py tests/test_static_cache.py -q
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight
git add llm-d-benchmarking-agent-project/ui/app.js \
        llm-d-benchmarking-agent-project/ui/styles.css \
        llm-d-benchmarking-agent-project/ui/preview.html \
        llm-d-benchmarking-agent-project/tests/test_ui_frontend.py \
        llm-d-benchmarking-agent-project/tests/test_static_cache.py
git commit -m "fix(ui): retire mid-run metrics-server button for a passive hint (offer moves pre-run)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Doc sync + full-suite gate

**Files:**
- Modify: `FEATURES.md:181-182`
- Modify: `docs/superpowers/specs/2026-06-08-metrics-server-preflight-offer-design.md` (status)

- [ ] **Step 1: Refresh the FEATURES.md rows**

Update row 182 (the installer row) to mention the proactive pre-run detection. Replace its status cell text with:

```
🔵 `probe_environment` reports `metrics_server.available` up front; on kind where it is false the agent OFFERS `run_command(["install_metrics_server.sh","--kubelet-insecure-tls"])` BEFORE the run (mutating → approval). Judgment in `knowledge/observability.md`; rule in `app/agent/prompt.py` HARD_RULES.
```

- [ ] **Step 2: Flip the spec status to `implemented`**

In `docs/superpowers/specs/2026-06-08-metrics-server-preflight-offer-design.md` change `**Status:** approved-pending-review` to `**Status:** implemented`.

- [ ] **Step 3: Run the FULL suite (the gate)**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight/llm-d-benchmarking-agent-project
PYTHONPATH=$PWD REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/ -q
```
Expected: ≈ 1820 passed / ~38 skipped, 0 failed. (Do NOT set `LLM_EVAL_LIVE=1` — that spends Max-plan quota.) Investigate any failure before proceeding.

- [ ] **Step 4: ruff + mypy (project gate)**

```bash
PYTHONPATH=$PWD /home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m ruff check app/ tests/
PYTHONPATH=$PWD /home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m mypy app/tools/probe.py app/main.py
```
Expected: clean (matches the project's ruff+mypy-clean convention). Fix any finding.

- [ ] **Step 5: Commit**

```bash
cd /home/tal/kind-quickstart-guide/.claude/worktrees/metrics-server-preflight
git add llm-d-benchmarking-agent-project/FEATURES.md \
        llm-d-benchmarking-agent-project/docs/superpowers/specs/2026-06-08-metrics-server-preflight-offer-design.md
git commit -m "docs: FEATURES + spec status for proactive metrics-server pre-flight

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (completed during planning)

- **Spec coverage:** (1) deterministic detection → Task 1 (probe fact + pre-warm); (2) reliable agent offer → Task 2 (HARD_RULE + playbook + observability); (3) retire button → Task 3 (passive note + dead-infra removal). Testing + thin-code + doc-sync → Tasks 1-4. All spec sections map to a task.
- **Placeholder scan:** none — every code/edit step shows the actual content; commands include expected output.
- **Type/string consistency:** the fact dict `{available, installed, ready_replicas}` is identical across the probe helper, all four probe tests, and the spec table. The marker string `"offers to install it"` is the same in `app.js`, `test_ui_frontend.py`, and `test_static_cache.py`. The argv `["kubectl","get","deployment","-n","kube-system","-l","k8s-app=metrics-server","-o","json"]` matches the validated allowlist form and the test assertion.
- **Allowlist:** both probe argvs verified `read_only` against the real allowlist — no `security/allowlist.yaml` change.

## Integration (after the gate is green)

Per project rules, do NOT push. Verify `merge-base feature/metrics-server-preflight main == main HEAD` (re-check, concurrent-session hazard), then fast-forward/merge `feature/metrics-server-preflight` → `main` locally, re-verify the merged SHA is an ancestor of `main`, delete the branch + worktree, and update the auto-memory metrics-server entry. Leave pushing for explicit user go-ahead.
