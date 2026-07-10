# Security model & threat model

The llm-d Benchmarking Agent is an LLM-driven assistant that **runs real commands** on behalf
of a user described in natural language (kubectl, the `llmdbenchmark` CLI, git, docker, kind,
…). That is inherently sensitive: an LLM proposes actions, and the system executes some of
them. This document describes the trust boundaries, the controls that keep an LLM (or a
malicious input) from doing damage, how secrets are handled, and what you must do before
exposing the agent beyond a single trusted user on `localhost`.

This is a description of the *implemented* model — every control named here exists in the code
referenced. It is not a roadmap.

## Trust boundaries

```
 ┌──────────┐   WS/HTTP    ┌──────────────────────────────────────────────┐
 │ Browser  │ ───────────▶ │ FastAPI backend (app/main.py)                 │
 │  (UI)    │ ◀─────────── │  — CORS (Phase 12)                            │
 └──────────┘   events      │  — agent loop (app/agent/loop.py)            │
   UNTRUSTED               │     ▲ tool calls                              │
   (never holds secrets)   │     │                                         │
                           │  ┌──┴───────────┐   prompts/responses         │
                           │  │ LLM provider │ ◀───────────────────────▶  (external API)
                           │  └──────────────┘   UNTRUSTED OUTPUT          │
                           │     │ proposes argv                           │
                           │  ┌──▼──────────────────────────────────────┐ │
                           │  │ Allowlist validator (security/)         │ │  ← THE trust gate
                           │  │  data: security/allowlist.yaml          │ │
                           │  └──┬──────────────────────────────────────┘ │
                           │     │ validated argv only                     │
                           │  ┌──▼──────────────┐  shell=False, scrubbed   │
                           │  │ CommandRunner   │ ───────────────────────▶ host / cluster
                           │  └─────────────────┘                          │
                           └──────────────────────────────────────────────┘
```

Three boundaries matter:

1. **Browser ↔ backend.** The browser is untrusted and **never receives secrets** (LLM API
   keys, HF token). It can only send chat messages and approve/deny proposed mutations. The
   backend has no auth/rate-limit of its own on this surface — see Network exposure, below.
2. **LLM output ↔ execution.** The LLM is treated as an **untrusted source of proposed
   actions**. Nothing the LLM emits runs directly: every command is validated by the allowlist
   and every *mutating* command additionally needs explicit human approval. Prompt injection in
   a user message, a repo doc, or a tool result can at worst cause the LLM to *propose* a
   command — it cannot widen what is allowed or auto-run a mutation.
3. **Backend ↔ host/cluster.** Commands run as argv lists with `shell=False`, a scrubbed
   environment, and a timeout (`app/security/runner.py`). In-cluster, the deploy grants a
   namespaced least-privilege Role (see `docs/DEPLOYMENT.md` and the packaging contract).

## The allowlist + approval model (the core control)

This is the heart of the security model and the project's **thin-code / thick-agent** law:
**the policy is DATA, the validator is mechanism.**

- **`security/allowlist.yaml`** is the deny-by-default policy. It enumerates the executables
  that may run, their permitted subcommands/flags, value constraints, and per-command execution
  limits (`timeout_s`). **You widen capability by editing this YAML — never by adding a
  per-command branch in Python.**
- **`app/security/allowlist.py`** (`Allowlist.validate`) is a *pure validator* with no embedded
  per-command knowledge. Given a logical argv it returns a `Decision`:
  - **denied** — `argv[0]` not allowlisted, an unpermitted subcommand/value, an empty command,
    or a token containing a shell metacharacter (rejected on *every* token as defense in depth,
    even though the runner never uses a shell).
  - **`read_only`** — a probe (e.g. `kubectl get`, `git status`). **Auto-runs**, no prompt.
  - **`mutating`** — anything that changes state (e.g. `kind create cluster`, `kubectl apply`).
    `requires_approval` is `True`; it runs **only after explicit UI approval**.
- The default mode is **`mutating` (conservative)**: an entry must *opt in* to read-only.
- The agent cannot escalate. The LLM never sees or edits the allowlist; it can only *request*
  an argv, which is then validated. Approval is a human decision relayed over the WebSocket
  (`approval` frame, Phase 15 schema-validated).

### Why this is safe against a misbehaving LLM
A compromised/confused LLM (including via prompt injection) is confined to the union of
(allowlisted commands) ∩ (commands a human approves). It cannot run an arbitrary binary,
inject a shell, read a secret out of the environment, or escape into an un-vetted code path.

## Command execution hardening (`app/security/runner.py`)

Every validated command is executed by `CommandRunner`:

- **`shell=False`, argv list only.** No shell string is ever constructed, so command injection
  is structurally impossible — there is no shell to inject into.
- **Scrubbed environment.** The child process gets only an allowlisted passthrough set
  (`PATH`, `HOME`, `KUBECONFIG`, locale/TLS vars, `LLMDBENCH_*` config) plus any explicitly
  configured `HF_TOKEN`. **`ANTHROPIC_API_KEY` is excluded
  by construction** — it is never in the child's environment.
- **Pinned working directory.** A command that must run inside a repo is confined to that repo
  path (`cwd_must_be`), resolved through a `repo:<name>` reference, not a caller-supplied path.
- **Timeouts + process-group kill.** Each command has a deadline (`Decision.timeout_s` from the
  YAML, else a sane default). On timeout the runner SIGKILLs the child's **whole process group**
  (`start_new_session=True`) so a double-forked daemon can't outlive the run.
- **Path-traversal guards.** File arguments are constrained (e.g. orchestrator manifests must be
  workspace-confined `.yaml`, no `..`); project scripts must resolve inside the project root.

## Secret handling & scrubbing

- **Secrets live only in the backend env / `.env`** (gitignored): `ANTHROPIC_API_KEY`,
  `HF_TOKEN`. `app/config.py` reads them from the environment — never from the browser.
- **The browser never sees them.** No secret is sent in any WS/HTTP response.
- **Child processes never see the LLM secret** — see the env scrub above. Only `HF_TOKEN`
  (needed for gated real-model pulls) is forwarded, and only when explicitly configured.
- **Logs never contain secrets.** Structured logs (Phase 11) record `exe = argv[0]` only —
  never the full argv or the environment — so a token passed as an argument or env var cannot
  leak into a log line.
- **In-cluster**, secrets are mounted from a Kubernetes `Secret` via `secretKeyRef` (never
  inline manifest values); the Helm chart manages the Secret from values, or points at a
  pre-existing one via `secret.existingSecret`. See `docs/DEPLOYMENT.md`.

## Network exposure

The FastAPI surface defaults to **`127.0.0.1:8000`** and has **no Bearer auth or rate-limiting of
its own** — this is a single-user, in-cluster/localhost service. The one trust control the app
ships is CORS:

| Control | Env | Effect when enabled |
|---|---|---|
| CORS | `CORS_ALLOW_ORIGINS=https://app.example.com,…` | `CORSMiddleware` installed only when set; otherwise no CORS headers (today's default). |

Exposure guidance:

- **Local dev on `127.0.0.1`** — nothing to configure.
- **LAN / shared / internet-exposed** — put an authenticating reverse proxy (or your cluster's
  ingress auth) **in front** of the agent; the app itself trusts every request that reaches it.
  TLS termination is likewise out of scope for the app itself.

## What requires isolation

The agent executes commands that mutate real infrastructure. Run it where that blast radius is
acceptable:

- **Run as a dedicated, low-privilege user**, not as a human's primary account or root. The
  agent can create/delete kind clusters, build images, and `kubectl apply` Jobs.
- **In-cluster, grant only the namespaced least-privilege Role** the deploy ships (the exact
  kubectl verbs `RealKubeClient` uses — no `*`, no `secrets`/`exec`, no cluster scope). See the
  packaging contract and `docs/DEPLOYMENT.md`.
- **Isolate the target cluster.** Point the agent at a throwaway/dev cluster (the `cicd/kind`
  quickstart path) for experimentation; do not aim it at production from an untrusted prompt
  source.
- **Treat the LLM provider as a third party.** Conversation content (including any pasted logs)
  is sent to the configured provider. Don't paste real secrets into the chat.
- **Keep `.env`, `secret.env`, and `KUBECONFIG` off shared volumes** and out of version control
  (they are gitignored).

## Reporting

This is a research/quickstart project, not a hosted service. If you find a security issue in
the agent's own code (an allowlist bypass, a secret leak, a shell-injection path), open an
issue describing the boundary that is violated and a minimal reproduction. Do **not** include
real tokens or kubeconfigs in the report.
