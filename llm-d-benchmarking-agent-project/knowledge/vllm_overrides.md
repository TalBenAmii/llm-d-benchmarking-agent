# Per-knob vLLM / scheduling / storage scenario overrides

Use this when the user wants a finer **scenario** edit than the high-level knobs the other
tools already cover — i.e. something `check_capacity` (model / parallelism / GPU memory) and
`generate_doe_experiment` (factor sweeps) don't express: a specific vLLM serve flag, a
KV-transfer/KV-events connector, a pod scheduling constraint (priority class, scheduler,
affinity), or a storage / network resource request.

The **mechanism** is one tool — `write_and_validate_config(artifact_type="scenario", …)`. It
deep-merges the per-knob OVERRIDES you supply onto a minimal `scenario: [ {name, …} ]`
skeleton, SHAPE-validates the knobs against the repo's own scenario examples (read live, so
it can never drift from upstream), and writes the file into the **session workspace only** —
never into the read-only repos. The **judgment** — *which* knobs to set, and to what — is
here. There is no knob list hardcoded in Python; this guide is the source of that decision.

## How to author one

Pass `content` as a flat map of **dotted upstream field paths → values**, plus a required
`name`. Example:

```
write_and_validate_config(
  artifact_type="scenario",
  target_filename="enforce-eager.yaml",
  content={
    "name": "kind-sim-eager",
    "vllmCommon.flags.enforceEager": true,
    "vllmCommon.flags.noPrefixCaching": true,
    "schedulerName": "custom-binpack-scheduler",
  },
)
```

That emits `scenario: [ {name: kind-sim-eager, vllmCommon: {flags: {enforceEager: true,
noPrefixCaching: true}}, schedulerName: custom-binpack-scheduler} ]`.

The tool returns two paths: `path` (the authored scenario) and **`spec_path`** — a companion
`<name>.spec.yaml` it ALSO writes into the workspace, wiring `scenario_file` to your authored
scenario and `values_file`/`template_dir` to the repo's stock copies. `spec_path` is the
thing you feed to `--spec` to gate the scenario (next section); you never hand-build it.

**Read the repo truth first** — don't guess flag names. The authoritative shapes live in:
- `read_repo_doc(path="llm-d-benchmark/config/templates/values/defaults.yaml")` — every
  legal scenario field with its default (`vllmCommon.*`, `affinity`, `routing.servicePort`,
  `decode`/`prefill` per-section overrides, `schedulerName`, …).
- `list_catalog(kinds=["scenarios"])` then `read_repo_doc` a near match (e.g.
  `config/scenarios/guides/wide-ep-lws.yaml` shows `schedulerName` + `vllmCommon.networkResource`;
  `config/scenarios/guides/workload-autoscaling.yaml` shows `vllmCommon.kvTransfer.*`;
  `config/scenarios/examples/gpu.yaml` shows `vllmCommon.flags.*` and `affinity.*`).

The tool refuses any top-level scenario-item key the repo's examples don't use, so a typo or
a stale field name fails fast (no file written) and you can self-correct.

## Then GATE it on the CLI's own determinism check (required)

The authored file is a *candidate*, not a deployment. **Always** preview it before any
mutation, exactly like the rest of the workflow. Pass the returned **`spec_path`** straight to
`--spec` — that companion spec points the CLI at your authored scenario:

```
# `result` is the write_and_validate_config(...) return; use result["spec_path"].
execute_llmdbenchmark(subcommand="plan", spec=<spec_path>, flags={"dry_run": true})   # or
execute_llmdbenchmark(subcommand="run",  spec=<spec_path>, flags={"dry_run": true})
```

The CLI resolves a full file path for `--spec`, and the allowlist admits a workspace
`*.spec.yaml` (so this gate is reachable without touching the read-only repo). A clean
dry-run/plan is the acceptance gate. If it errors, fix the knob and re-author — don't stand
up a scenario that didn't pass the plan.

## Which knobs, and when

Pick the **minimum** set that expresses the user's intent; leave everything else to the
scenario/`defaults.yaml` defaults (don't over-specify).

### vLLM serve flags — `vllmCommon.flags.*`
These shape the auto-generated `vllm serve` command. Common ones:
- `enforceEager: true` — disable CUDA-graph capture. Slower steady state, but lower startup
  memory and deterministic — handy on tiny/CPU-sim or memory-pinched nodes.
- `noPrefixCaching: true` — disable automatic prefix caching. Set it when you are measuring
  the *cold* path or when prefix caching would mask the effect you're studying.
- `disableLogRequests` / `disableUvicornAccessLog` — quiet per-request logging for cleaner
  throughput runs.
- `allowLongMaxModelLen`, `serverDevMode` — only when a long context / dev image needs them.
Match the flags to the image: upstream `vllm-openai` (standalone path) and the llm-d-cuda
modelservice image accept different flags — read the scenario comments / defaults first.

### KV transfer & events — `vllmCommon.kvTransfer.*`, `vllmCommon.kvEvents.*`
For prefill/decode **disaggregation** and **prefix-cache-aware routing**:
- `vllmCommon.kvTransfer.enabled: true` + `.connector` (e.g. `NixlConnector`) + `.role`
  (`kv_both`/`kv_producer`/`kv_consumer`) — wires the cross-pod KV cache transfer. Set this
  when the user wants P/D disaggregation benefits in the numbers.
- `vllmCommon.kvEvents.enabled: true` (+ `.publisher`, `.port`, `.topicPrefix`) — emits KV
  cache events so the router can do precise prefix-cache-aware routing. Pair with a routing
  connector that consumes them.
These only matter on a real multi-pod stack; on the single-pod kind/CPU-sim quickstart they
add config noise without changing the measurement — don't set them there.

### Scheduling & priority — `schedulerName`, `*.priorityClassName`, `affinity.*`
- `schedulerName` (top-level, or per-section `decode.schedulerName`/`prefill.schedulerName`)
  — route pods to a custom scheduler (e.g. a bin-packing or gang scheduler). The named
  scheduler **must already exist** on the cluster.
- `vllmCommon.priorityClassName` (or `decode.priorityClassName`/`prefill.priorityClassName`)
  — give the model-server pods a PriorityClass so they preempt lower-priority work on a busy
  cluster. The PriorityClass **must already exist**; leave empty to inherit.
- `affinity.enabled: true` + `affinity.nodeSelector` (exact node-label matches) pins pods to
  a node pool (e.g. a GPU type). `affinity.podAffinity` / `affinity.podAntiAffinity` co- or
  anti-locate against other pods (raw K8s affinity blocks, merged verbatim). Use anti-affinity
  to spread decode replicas across nodes, or to keep the load generator off the SUT nodes.

### Storage, network & ports — `vllmCommon.ephemeralStorage`, `vllmCommon.networkResource`, `routing.servicePort`
- `vllmCommon.ephemeralStorage` (e.g. `"20Gi"`) — bump the pods' ephemeral-storage
  request/limit when model download / scratch needs it (only added when non-empty).
- `vllmCommon.networkResource` (e.g. `"rdma/roce_gdr"` or `"auto"`) — request an RDMA/RoCE
  extended resource for fast inter-node KV transfer on real GPU fabrics. Pointless on
  kind/CPU-sim — omit it there.
- `routing.servicePort` — change the service port the router exposes (default matches the
  helm chart's vLLM service port). Only touch it on a real port conflict.

## Guardrails
- One scenario edit at a time when you're attributing a result to it — change the knob, plan,
  run, compare. Bundling many knobs makes the delta unattributable.
- The repos stay **read-only**: this authors into the session workspace; it never edits the
  spec under `config/scenarios/`.
- If a knob you want isn't accepted, you guessed a field name — re-read `defaults.yaml` and a
  real scenario example, then re-author.
