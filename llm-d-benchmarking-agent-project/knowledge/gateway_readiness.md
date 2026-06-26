# Gateway-mode readiness — interpreting the Gateway-API control plane

**When to read this:** a `check_endpoint_readiness` result carries a `gateway` block (gateway-mode
deploy) and especially when it carries `gateway_readiness_guidance` — i.e. the model pods may be
Ready, but the Gateway-API control plane is **not wired** yet, so *traffic cannot reach the pods*.
Source: `llm-d/guides/prereq/gateways/gke.md` (GKE), plus the Istio / agentgateway sibling guides.

This is the **judgment layer**. The Python analyzer (`app/readiness/diagnostics.py:analyze_gateway`)
only extracts conditions into facts; it never decides wait-vs-stand-up-vs-config-error. YOU make that
call here, by reading the facts the tool gives you.

> Deploy-time endpoint verification (the post-deploy connectivity check) lives in the upstream
> **deploy-llm-d skill** — `fetch_key_docs(task='deploy_skill')`, file
> `references/connectivity-verification.md`. This file is the gateway control-plane readiness judgment.

## The two distinct "not ready" states

Endpoint readiness (`reason: endpoints_ready` and a non-empty `ready_endpoints`) answers **"are the
model pods Ready?"**. The `gateway` block answers a *different* question: **"can traffic actually
reach them?"** In a gateway-mode deploy the data path is:

```
client → Gateway (LoadBalancer/controller) → HTTPRoute → InferencePool → EPP → model pods
```

So a benchmark that targets the **Gateway address** can fail with no usable endpoint **even though
every model pod is Ready**, because a link in that chain isn't up. Say this plainly to the user:
> "The model pods are up, but the Gateway is still `PROGRAMMED:False` (or the InferencePool isn't
> `ResolvedRefs:True`), so no traffic reaches them yet."

This is a common, distinct state that the pod/endpoint check alone misses.

## The facts on `gateway` (all mechanism, no decision baked in)

| Fact | Source condition | Meaning |
|------|------------------|---------|
| `programmed` (bool/None) | Gateway `status.conditions[type=Programmed].status` | the controller has provisioned the data plane (LB address allocated, listeners up). `None` = not stamped yet / no Gateway. |
| `gatewayclass_exists` (bool) | any GatewayClass object present | the controller class the Gateway binds to exists. |
| `inferencepools[].accepted` / `.resolved_refs` | InferencePool `status.parents[].conditions` (Accepted / ResolvedRefs) | the pool is attached to the Gateway (Accepted) and its EPP/backend refs resolve (ResolvedRefs). |
| `httproutes[].accepted` / `.reconciled` | HTTPRoute `status.parents[].conditions` (Accepted / Reconciled) | the route binds to the Gateway (Accepted) and the controller programmed it (Reconciled / `ReconciliationSucceeded`). |
| `control_plane_ready` (derived) | all of: PROGRAMMED:True + a GatewayClass exists + every InferencePool ResolvedRefs:True | "traffic CAN reach the pods". A FACT, not an instruction. |
| `not_ready_reason` (token) | the first unmet condition | `gatewayclass_missing` / `gateway_not_programmed` / `inferencepool_unresolved`. Names WHICH gap — you decide the action. |

## How to act on each `not_ready_reason`

### `gatewayclass_missing` — surface a CONFIG error, do **not** just wait
No GatewayClass means the Gateway has nothing to bind to; it will **never** become PROGRAMMED on its
own. This is a missing prerequisite, not a slow rollout. Tell the user the Gateway-API controller /
GatewayClass isn't installed and point at the guide's prerequisite steps (GKE: "Enable Gateway API in
your cluster"; Istio/agentgateway: install the controller + its GatewayClass). Waiting will not fix it.

### `gateway_not_programmed` — usually WAIT (a short rollout), then escalate
`PROGRAMMED:False` while a GatewayClass exists is normally a **transient provisioning** state: the
controller is allocating the LoadBalancer/address and bringing up listeners. On GKE this is typically
**~30s–2min** (the guide shows `PROGRAMMED True` at ~30s age). Recommended:
- **Keep waiting / re-poll** `check_endpoint_readiness` for the first **~2–3 minutes** of Gateway age.
- If it stays `PROGRAMMED:False` well **beyond ~5 minutes**, stop waiting and treat it as a config
  problem. Per the GKE guide's troubleshooting, the usual causes are: Gateway API not enabled, a
  missing **proxy-only subnet**, an unsupported GKE version, or no external-LB support in the cluster.
  Also check `status.addresses` is non-empty — an empty address means it's still waiting on the LB.
- Do **not** stand up a new model stack to "fix" a not-programmed Gateway — the model pods are a
  separate concern; this is a networking/control-plane issue.

### `inferencepool_unresolved` — diagnose, then wait-or-fix
The Gateway is programmed but an InferencePool isn't `ResolvedRefs:True`:
- `accepted:False` → the pool isn't attached to the Gateway (parentRef / Gateway-name mismatch). Config error.
- `accepted:True, resolved_refs:False` → the pool is attached but its **backend/EPP refs don't
  resolve** (the selector matches no pods, the EPP Service/Deployment is missing, or the model
  Deployment hasn't created pods yet). If the model pods are still coming up this can resolve on its
  own — **briefly wait** and re-poll; if the pods are already Ready and it stays unresolved, it's a
  **misconfiguration** (wrong selector/EPP ref) — surface it.

### HTTPRoute facts — the `fault filter abort` symptom
`fault filter abort` from the Gateway IP (GKE guide troubleshooting) means the request matched no
route or the backend routing is misconfigured. The signal: the HTTPRoute should have a `Reconciled`
condition with reason `ReconciliationSucceeded`. If `httproutes[].reconciled` is False/None (or a
route is `accepted:False`), the route → InferencePool wiring is wrong — verify the route's `parentRefs`
match the Gateway name and its `backendRefs` match the InferencePool name. This is a **config error**,
not something to wait out.

## Decision summary

- **Everything green** (`control_plane_ready:true`): traffic can reach the pods — proceed (subject to
  the usual endpoint/serving readiness gate).
- **`gateway_not_programmed` and the Gateway is young (<~3min)** *and a GatewayClass exists*: **wait**
  and re-poll.
- **`inferencepool_unresolved` while model pods are still coming up**: **wait** briefly and re-poll.
- **`gatewayclass_missing`, a long-stuck `PROGRAMMED:False`, an unresolved pool with pods already
  Ready, or an unreconciled/unaccepted HTTPRoute**: **surface a config error** — name the specific
  unmet condition and the guide's prerequisite/troubleshooting step. Do **not** stand up a model stack
  to fix a networking-layer problem.

Never invent an action the facts don't support, and never auto-run a mutating fix — any standup /
apply is mutating and requires the user's explicit, approval-gated go-ahead.
