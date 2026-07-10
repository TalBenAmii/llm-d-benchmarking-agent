# Per-phase CLI timeouts (`--*-timeout`) — give a slow phase more rope without losing the outer leash

The `llmdbenchmark` CLI has its OWN per-phase timeout flags (all integer **seconds**). They are
a **DEEPER bound** than the host's per-command runner deadline: each one tells the CLI how long
to wait *inside* one specific phase — a deploy step, a PVC bind, the harness wait, a teardown
drain — before it gives up on that phase and fails fast with a useful, phase-specific error.

Set them from the agent through `flags` on `execute_llmdbenchmark`, e.g.
`flags={'pvc_bind_timeout': 600}` on a `standup`, or `flags={'wait_timeout': 5400}` on a `run`.
`build_argv` emits the matching `--*-timeout <seconds>` token **only on the subcommand(s) that
upstream accepts it on** — an out-of-place key emits nothing.

## The two timeout layers — and the ONE rule that keeps them from fighting

There are two independent timeouts in play. Keep them straight:

| Layer | Where it lives | What it bounds | How long |
|-------|----------------|----------------|----------|
| **Runner deadline** (outer) | `security/allowlist.yaml` `timeout_s` per subcommand (DATA), enforced by `asyncio.wait_for` in the runner | The **whole** CLI process — if it overruns, the child process group is SIGKILLed | standup/run **3600**, teardown **900**, experiment **14400**, plan **300**, results **600** |
| **CLI per-phase timeout** (inner, Phase 38) | The `--*-timeout` flags here | One **phase** inside the CLI run (one deploy / bind / wait / drain) | Whatever you pass |

**THE RULE: every per-phase timeout MUST stay strictly BELOW the runner `timeout_s` ceiling for
that subcommand.** The inner timeout is supposed to trip *first*, so the CLI fails the phase
cleanly with a phase-specific diagnostic. If you set a per-phase timeout at or above the runner
ceiling, the outer `asyncio.wait_for` SIGKILLs the process first — you lose the CLI's clean
error and get a blunt "timed out" instead, and the two layers are effectively fighting. So:

- A per-phase timeout on **standup** or **run** must be **< 3600**.
- `fma_teardown_timeout` on **teardown** must be **< 900**.
- `wait_timeout` / `data_access_timeout` on **experiment** must be **< 14400** (and really much
  less — they apply *per treatment*, and the sweep also stands up + tears down each treatment).

Leave a margin (the phase is rarely the only thing happening in the command). A good habit:
keep a per-phase timeout to **at most ~70–80%** of the runner ceiling, never right up against it.

If a phase genuinely needs **more** time than the runner ceiling allows, the per-phase flag is
the wrong lever — that is a `timeout_s` policy change in `security/allowlist.yaml` (Phase 13
governance), not a CLI flag. Do not try to out-set the runner deadline with a CLI flag; you
can't, and you shouldn't want to.

## The flags — what each one bounds, and on which subcommand

**standup** (the deploy/bind phases):

- `standalone_deploy_timeout` → `--standalone-deploy-timeout`: wait for the vLLM pods to deploy
  in **standalone** mode.
- `gateway_deploy_timeout` → `--gateway-deploy-timeout`: wait for the **gateway infrastructure**
  pods to deploy (modelservice path).
- `modelservice_deploy_timeout` → `--modelservice-deploy-timeout`: wait for the decode / prefill
  / inference-pool pods to deploy (modelservice path — the model-server pods themselves).
- `kustomize_deploy_timeout` → `--kustomize-deploy-timeout`: wait for pods to deploy in
  **kustomize** mode.
- `pvc_bind_timeout` → `--pvc-bind-timeout`: wait for each PVC (workload / model / extra) to
  reach the **Bound** phase. Upstream default **240**. It fails fast on a missing default
  StorageClass instead of letting a stuck PVC masquerade as a downstream pod/job timeout.

**run + experiment** (the harness phases):

- `wait_timeout` → `--wait-timeout`: seconds to wait for **harness completion** (`0` = do not
  wait — fire-and-forget; only do that deliberately).
- `data_access_timeout` → `--data-access-timeout`: wait for the harness **data-access pod** to
  become Ready (relevant when a dataset replay needs the data-access sidecar up first).

**teardown**:

- `fma_teardown_timeout` → `--fma-teardown-timeout`: wait for the FMA launcher/requester pods to
  terminate before the Helm uninstall removes the controller. Upstream default **120**.

## WHEN to raise one — your judgment

The CLI defaults are tuned for fast paths; raise a specific per-phase timeout when you have a
concrete reason that *that phase* will legitimately take longer:

- **Big model weights / slow image pulls → `modelservice_deploy_timeout` (or
  `standalone_deploy_timeout`).** A large model server can take many minutes to pull its image
  and load weights before it reports Ready. If `check_endpoint_readiness` /
  `knowledge/readiness_probes.md` says a pod is *still loading* (not wedged), the deploy phase
  needs more rope — raise the matching deploy timeout rather than letting standup fail a
  genuinely-healthy-but-slow rollout.
- **Slow dynamic provisioner → `pvc_bind_timeout`.** Some CSI provisioners take 1–3 minutes per
  volume; with several PVCs the default 240 can be tight. Raise it on a cluster known to be slow
  to provision. But if there is **no default StorageClass at all**, the PVC will *never* bind —
  do not paper over that with a huge timeout; fix the StorageClass (see
  `knowledge/preconditions.md`). Failing fast here is a feature.
- **Long benchmark → `wait_timeout`.** A high-concurrency or long-duration workload can run
  longer than the default harness wait. Size `wait_timeout` to the expected run length plus
  margin — but keep it under the run ceiling (3600); a single run that genuinely needs more than
  an hour is a `timeout_s` policy question, not a flag.
- **Dataset replay with a data-access pod → `data_access_timeout`.** When replaying a real
  dataset (`flags['dataset']`, see `knowledge/dataset_replay.md`) the data-access pod must come
  up first; on a slow cluster give it more time.
- **Sticky teardown → `fma_teardown_timeout`.** If FMA launcher/requester pods are slow to drain
  and the uninstall races them, give the drain more time so the controller isn't yanked early.

## WHEN to LOWER one — fail fast on a small cluster

On the Kind / CPU-sim quickstart you usually want the OPPOSITE: shorter per-phase timeouts so a
misconfiguration surfaces in seconds, not after the full default wait. If you *know* a tiny
model on a local cluster should deploy in well under a minute, a tight `modelservice_deploy_timeout`
turns a silent stall into a fast, actionable failure. `wait_timeout: 0` makes a `run`
fire-and-forget (it returns immediately and does not wait for harness completion) — only use that
deliberately, e.g. when you will poll results yourself.

## What NOT to do

- **Do not set a per-phase timeout ≥ the runner ceiling** for that subcommand (see the rule
  above) — the runner would kill the process first and you'd lose the CLI's clean error.
- **Do not set a timeout key on a subcommand that doesn't accept it.** `build_argv` drops it
  silently and the allowlist would refuse it anyway; just don't.
- **Do not use these to "make a deploy reliable."** A timeout buys *patience*, not *capacity*.
  If a deploy is failing because the cluster can't fit the model, raising the timeout just makes
  the failure slower — run `check_capacity` / `advise_accelerators` and fix the real constraint
  (`knowledge/capacity.md`, `knowledge/accelerators.yaml`).

## Mechanism vs. judgment

Emitting the `--*-timeout` flags (and guarding each on the accepting subcommand) is **mechanism**
— a static table in `app/tools/run/execute.py`, no `if/elif` on the value. The allowlist permits each
flag value-pinned to a positive integer (DATA, `security/allowlist.yaml`). WHETHER to set one,
to WHAT value, and the keep-it-below-the-runner-ceiling reconcile that stops the two layers from
fighting — that judgment lives **here**, never in Python.
