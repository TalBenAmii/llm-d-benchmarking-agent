# Design: proactive metrics-server pre-flight check + agent offer

**Date:** 2026-06-08
**Status:** implemented
**Branch:** `feature/metrics-server-preflight`

## Problem

On the quickstart (local **kind**) path, the in-cluster **metrics-server** is not present, so
the live CPU/memory panel during a benchmark reads *"live resource stats unavailable (no
metrics-server)"*. Today the only way the agent learns metrics-server is missing is the
**resource poller**, which runs **during** a benchmark run (`app/observability/resource_poller.py:70-84`,
polling `kubectl top pods` every 5s). Because that signal only exists mid-run, the UI's
*"Install metrics-server for live stats"* button is rendered only inside the live-resource
panel — which is shown only while `busy === true` (`ui/app.js:1051-1082`).

Clicking that button mid-run sends a normal user message, but a benchmark turn is already
in flight (often paused on the run-command approval card). The backend's single-turn-in-flight
guard rejects it:

```
still working on the previous request — please wait.
```
(`app/main.py:781`).

So the user cannot install metrics-server *before* the run — the only affordance appears at
the worst possible time and collides with the run it is supposed to precede.

The quickstart playbook **already** instructs the agent to "PROACTIVELY OFFER" the install
(`knowledge/quickstart_playbook.md:45`), but that is CORE-knowledge prose and the model
skipped it this session. Prompt prose alone is not a reliable guarantee.

## Goal

Detect metrics-server availability **deterministically and early** — before any benchmark
run, when no turn is in flight — and have the agent **offer the install in chat** at that
point (the existing approval-gated install). Retire the conflicting mid-run button.

This is the "**deterministic check + agent offer**" approach (chosen over a hard backend gate
and over prompt-only): the *check* is made deterministic (a probe fact that is always
present); the *offer* stays an agent decision rendered as the normal approval card.

## Non-goals

- No hard backend gate that blocks the run independent of the agent (explicitly rejected —
  keeps the offer conversational, avoids new approval-flow plumbing).
- No change to the `install_metrics_server.sh` script or its allowlist entry (already
  registered as a mutating, approval-gated command in `security/allowlist.yaml`).
- No change to how the live poller works during a run (it keeps emitting `available:false`;
  only the UI rendering of that state changes).

## Design

Three coordinated changes.

### 1. Deterministic, early detection — a `metrics_server` fact in `probe_environment`

Add a `metrics_server` entry to `_ALL_CHECKS` in `app/tools/probe.py:34` and a
`_probe_metrics_server(ctx)` helper, mirroring the existing `_probe_prometheus_crds`
(`probe.py:226-251`) — a read-only, fact-only probe that never raises and degrades cleanly
with no cluster.

It reports (both commands verified already-allowlisted — **no `security/allowlist.yaml`
change needed**):

| field           | source command                                                                         | meaning |
|-----------------|----------------------------------------------------------------------------------------|---------|
| `available`     | `kubectl top nodes` → exit code `== 0`                                                  | metrics actually flowing (the same `kubectl top` family the live poller uses) |
| `installed`     | `kubectl get deployment -n kube-system -l k8s-app=metrics-server -o json` → `.items` non-empty | the metrics-server Deployment exists |
| `ready_replicas`| `.items[0].status.availableReplicas` from that same get                                | distinguishes "not installed" from "installed but NotReady" (the kind `--kubelet-insecure-tls` gotcha) |

> **Allowlist note:** `kubectl get` permits only ONE positional (`kubectl_resource`), so the
> probe queries by **label selector** (`-l k8s-app=metrics-server`), not by name
> (`get deployment metrics-server` would be two positionals and is rejected). `deployment` is
> in the `kubectl_resource` enum; `k8s-app=metrics-server` passes the `label_selector` regex;
> `kubectl top nodes` is permitted (positional enum includes `node`/`nodes`). All verified
> against `security/allowlist.yaml` — reuse only, no widening.

`available` is the authoritative "do we have live stats" signal and matches the poller's
notion exactly. `installed`/`ready_replicas` give the agent enough to phrase a precise offer
("metrics-server isn't installed" vs "it's installed but not ready — needs
`--kubelet-insecure-tls` on kind").

Guarded on `shutil.which("kubectl")`; when kubectl is absent or the cluster is unreachable
the probe returns `{available: False, installed: False, ready_replicas: None}`. Two cheap
read-only calls (bounded by ~12s timeouts, the existing pattern). **Mechanism only — no
judgment branch** (the `prometheus_crds` precedent: it reports facts; the
`--monitoring` decision lives in `knowledge/observability.md`).

Because `probe_environment` is the mandatory first step (ROLE step 2, HARD_RULES) **and** is
injected as the per-turn `[environment pre-probe …]` snapshot (the proactive/pre-warm path),
this fact is present in front of the agent from turn 1 — before any plan, deploy, or run.
The *check* therefore requires no LLM choice to happen.

### 2. Reliable offer — a HARD_RULE (not buried playbook prose)

Add one rule to `HARD_RULES` (`app/agent/prompt.py:44`):

> Before the FIRST benchmark `run` on a local kind cluster, read the probe's `metrics_server`
> fact. If it reports `available: false`, make a SINGLE one-line offer to install the
> in-cluster metrics-server via
> `run_command(["install_metrics_server.sh","--kubelet-insecure-tls"])` and let the user
> approve it BEFORE you run — it is a per-cluster add-on that powers the live CPU/memory
> panel. Skip the offer if metrics-server is already `available`, the user already declined,
> or the cluster is not kind/has no live-stats need (see `read_knowledge('observability')`).

HARD_RULES are the strongest always-on instructions and are the lever the recent
`harden-plan-workload` change used to remove phrasing-dependent inconsistency. This adds a
small, stable number of bytes to the prompt-cached prefix (acceptable — it is a permanent
addition, not per-turn-varying, so the cache still hits; `tests/test_context_mgmt.py`
re-baselines).

Tighten `knowledge/quickstart_playbook.md` step 5b (lines 45-54) so its trigger is the probe
fact (`metrics_server.available == false`) and its timing is "before the run" (it currently
says "right after the cluster is up", which is fine but should explicitly tie to the fact and
the run boundary). Keep `install_metrics_server?` in `expected_steps` (line 24).

`knowledge/observability.md` already carries the WHEN/HOW/skip judgment (the
`--kubelet-insecure-tls` requirement, per-cluster add-on, GKE/OpenShift skip cases) — only a
pointer tweak to key off the probe fact, no substantive rewrite.

### 3. Retire the mid-run button → passive note

In `ui/app.js:1051-1082`, the `data.available === false` branch currently renders the
actionable `Install metrics-server for live stats` button with `sendOrQueueUserMessage`
queueing and the `metricsInstallRequested` state flag. Replace the whole actionable block
with a **passive informational note only**, e.g.:

> Live resource stats need the in-cluster metrics-server. The assistant offers to install it
> before a run.

No clickable control inside the busy-only panel → no collision with the in-flight guard.
This is now a rare fallback (the offer fires pre-run), so an explanatory note is enough.

Cleanup of now-dead code (verify usages first, remove only if exclusively used by the button):
- `metricsInstallRequested` state flag (`ui/app.js:103`).
- `sendOrQueueUserMessage` / `flushPendingUserSend` (`ui/app.js:2626-2644`) — **only** if no
  other caller exists; if another control uses them, leave them and just stop calling them
  from this panel.

## Data flow (after)

```
turn 1: pre-probe runs probe_environment(checks="all")
        → snapshot includes metrics_server: {available:false, installed:false, ...}
        → injected as "[environment pre-probe …]" user message
   ...
agent reaches the deploy/run boundary on a kind cluster
        → HARD_RULE + playbook 5b key off metrics_server.available==false
        → agent: one-line offer + run_command(["install_metrics_server.sh","--kubelet-insecure-tls"])
        → renders the normal "Approve this command" card  ← no busy collision (nothing in flight)
        → user Approves → vetted idempotent install → metrics-server Ready
        → benchmark run proceeds; live poller now emits available:true → live panel shows stats
fallback: if a run somehow starts without stats, the live panel shows a passive note (no button)
```

## Testing

- **`tests/` probe test** (extend `tests/test_*probe*` or `test_new_tools.py`): mock `kubectl`
  to assert the `metrics_server` fact shape for three states — not installed (`available:false,
  installed:false`), installed-but-NotReady (`installed:true, ready_replicas:0, available:false`),
  and available (`available:true`). Assert no raise when kubectl is missing.
- **`tests/test_context_mgmt.py`**: re-baseline the byte-stable prefix snapshot (HARD_RULES
  grew by stable bytes). Confirms the cache-stability invariant still holds.
- **`tests/test_ui_frontend.py`**: drop/replace the assertion on the
  `"Install metrics-server"` button text; assert the passive note text instead. (No
  Node/Chromium — UI is verified via this test + `ui/preview.html`.)
- **Flow validation** (`tests/` flow harness): `install_metrics_server?` is already a
  conditional `expected_step`, so the offer should not break flow expectations; re-run to
  confirm.
- **Full suite** healthy baseline ≈ 1820 passed / ~38 skipped; establish green before and
  after. Do NOT run live-LLM eval (`LLM_EVAL_LIVE=1`) — quota.

## Files touched

| File | Change |
|------|--------|
| `app/tools/probe.py` | add `metrics_server` to `_ALL_CHECKS` + `_probe_metrics_server` helper (fact-only) |
| `app/agent/prompt.py` | one new `HARD_RULES` line (offer before run when `metrics_server.available==false` on kind) |
| `knowledge/quickstart_playbook.md` | tie step 5b trigger to the probe fact + the run boundary |
| `knowledge/observability.md` | pointer tweak to key the offer off the probe fact (no substantive rewrite) |
| `ui/app.js` | replace mid-run install button with a passive note; remove now-dead queueing/state if unused |
| `tests/…` | probe-fact test; re-baseline context-mgmt; update ui-frontend assertion |

## Thin-code / thick-agent compliance

- The new probe is **mechanism**: it reports facts (`available`/`installed`/`ready_replicas`),
  no decision branch — exactly like `_probe_prometheus_crds`.
- The **offer decision** (when to offer, how to phrase, when to skip) lives in the prompt
  HARD_RULE + `knowledge/` — the agent's reasoning, not Python `if/elif`.
- No allowlist change; reuses the existing vetted, approval-gated install command.

## Risks / mitigations

- *Two extra read-only kubectl calls in every probe* → cheap, bounded by timeout, only when
  kubectl is present; mirrors existing node/CRD probes.
- *Prompt prefix grows* → small, stable bytes; cache still hits; re-baseline test asserts it.
- *Agent still has discretion to skip the offer* → accepted by design (chosen approach is
  "agent offer", not a hard gate); reliability is maximized by the always-present fact + a
  HARD_RULE (strongest prompt lever), a strict improvement over today's buried prose.
