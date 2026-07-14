# Gateway class / provider selection (--gateway-class)

The **gateway class** is the inference-gateway PROVIDER — the control plane (or
"no control plane") that fronts the model pool and routes requests to the
EPP/InferencePool. A scenario normally pins it in its `gateway.className` field;
`flags.gateway_class` lets you **override** that per command **without editing
the scenario YAML**, emitted as `--gateway-class <provider>` by
`execute_llmdbenchmark`. This is mechanism — the WHICH-provider judgment below is
yours, grounded here, not in any Python branch.

> The upstream **deploy-llm-d skill** auto-detects the gateway provider during a real deploy
> (`fetch_key_docs(task='deploy_skill')`); this file is the per-command `--gateway-class` override.

- **Valid on every subcommand** (`plan`, `standup`, `smoketest`, `run`,
  `teardown`, `experiment`). It is a value-pinned enum in the command policy, so an
  out-of-enum value is refused.
- **Override, not a default.** Omit it to inherit the spec's `gateway.className`.
  Precedence (highest wins): `--gateway-class` flag → scenario `gateway.className`
  → upstream `defaults.yaml` (`istio`). The same effect is available via the
  `LLMDBENCH_GATEWAY_CLASS` env var, but prefer the flag (auditable in argv).
- **Effective only on the `modelservice` deploy path.** For `kustomize`,
  `standalone`, and `fma` the gateway block is ignored by every rendered template,
  so the override has no effect there (upstream even accepts any value in those
  modes). When `methods='kustomize'` / `-t kustomize`, do NOT bother setting it.
- **Setting it does not change a command's mode.** A `standup`/`run`/`teardown`
  with `--gateway-class` is still mutating and stays approval-gated; only
  `--dry-run`/`-n` previews it. `plan --gateway-class …` stays read-only and is a
  good way to *preview* the rendering under a different provider before standing up.

## The five providers — what each deploys, and when to pick it

| `gateway_class` | What it deploys | Pick it when |
|---|---|---|
| `istio` | istio-base + istiod control plane, a Gateway + HTTPRoute, the `inferencepool` GAIE chart | The upstream default; the roomy/production-like choice ("How to choose" §2). |
| `agentgateway` | agentgateway-crds + agentgateway controller, a Gateway + HTTPRoute, the `inferencepool` GAIE chart | You want agentgateway's lightweight Envoy-based data plane instead of Istio; the **Kind MVP** (`cicd/kind`) default ("How to choose" §2). |
| `gke` | Uses the **GKE-managed** Gateway controller; same `inferencepool` GAIE chart (nothing installed by the tool) | Running on **GKE** — the platform already provides the Gateway controller, so don't install Istio/agentgateway. |
| `data-science-gateway-class` | The **OpenDataHub / OpenShift AI managed** Gateway; same `inferencepool` GAIE chart | Running on **OpenShift AI / OpenDataHub** — the platform manages the Gateway. |
| `epponly` | **No** Kubernetes Gateway, **no** HTTPRoute; the `standalone` GAIE chart — the EPP pod runs an Envoy sidecar that serves HTTP directly | You want llm-d's **standalone router topology** with no gateway/control plane at all — the leanest option; the `guides/optimized-baseline` default. Pick it on a constrained cluster ("How to choose" §2–3). |

## How to choose (judgment)

1. **Match the platform first.** On GKE → `gke`; on OpenShift AI/OpenDataHub →
   `data-science-gateway-class`. These are platform-managed; installing Istio or
   agentgateway there is wrong/redundant.
2. **Otherwise, match the cluster's headroom.**
   - **Small / single-node / Kind cluster:** prefer `agentgateway` (lean Envoy proxy —
     the `cicd/kind` default precisely because it is lean enough for a single-node
     cluster where EPP, model-server pods, and the gateway must all schedule together)
     or `epponly` (no Gateway at all). Avoid `istio` unless the node can clearly host
     istiod alongside everything else.
   - **Roomy / multi-node / production-like cluster:** `istio` is the most flexible,
     well-trodden default, closest to a production deployment — pick it when the user
     wants the standard, well-tested topology and the cluster can host the Istio
     control plane.
3. **Want no Gateway API control plane at all?** → `epponly` (standalone router).
   The readiness checks differ — see the `epponly` ↔ readiness guardrail below.
4. **Just want the scenario's intent?** → omit `gateway_class` and let the spec's
   `gateway.className` stand. Only override when the user names a provider or the
   platform/headroom clearly dictates a different one than the spec picked.

## Interplay & guardrails

- **A typo is caught loudly** on the modelservice path: an unsupported value (e.g.
  `isto`) makes the CLI raise `ValueError: --gateway-class='isto' is not a
  supported value … Choose one of: epponly, istio, agentgateway, gke,
  data-science-gateway-class.` The command policy enum refuses it even earlier, so only
  one of the five legitimate providers is expressible.
- **Preview before you commit.** `plan` (read-only) with `gateway_class` set lets
  you render under the chosen provider and confirm the topology before a real
  `standup`.
- **Composes with the other modeled flags** (`models`, `monitoring`, `stack`,
  `kubeconfig`, …) — it rides alongside, it doesn't replace them. In a multi-stack
  scenario the gateway provider is shared infra installed once per scenario; the
  override applies to that shared install.
- **`epponly` ↔ readiness:** `epponly` installs no Gateway/HTTPRoute and changes the
  routing topology and the Service the endpoint resolves to (the EPP's `…-gaie-epp`
  Service), so the Gateway-mode readiness gate (Gateway PROGRAMMED / InferencePool
  Accepted+ResolvedRefs / HTTPRoute — knowledge/gateway_readiness.md, gateway-backed
  classes only) does not apply — check the EPP Service / model-server readiness
  directly instead.
