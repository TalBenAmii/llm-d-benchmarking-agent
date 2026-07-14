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

The CLI resolves a full file path for `--spec`, and the command policy admits a workspace
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

### Engine sizing — `vllmCommon.maxNumSeq`, `.maxNumBatchedTokens`, `.gpuMemoryUtilization` (NOT under `.flags`)
These are TOP-LEVEL `vllmCommon.*` (or per-section `model.*`) knobs — **not** `vllmCommon.flags.*`.
The flags renderer is a **fixed whitelist**, so putting one of these under `.flags` (e.g.
`vllmCommon.flags.maxNumSeqs`) renders to **nothing** and surfaces in `unrecognized_flags`. Set
them at the `vllmCommon`/`model` level instead. (Grounded in `config/templates/values/defaults.yaml`
+ `config/scenarios/examples/spyre.yaml`, which set them this way.)
- `vllmCommon.maxNumSeq` — the engine's **max concurrent sequences** (per-replica batch size /
  in-flight request cap). **Note the spelling: singular `maxNumSeq`, no trailing "s"** — and the
  repo emits the **singular** serve arg `--max-num-seq $VLLM_MAX_NUM_SEQ` (which differs from
  vLLM's canonical `--max-num-seqs`). Default **256**. Raise it to admit more in-flight requests
  per replica (more concurrency, more KV-cache pressure); lower it to bound batch memory.
- `vllmCommon.maxNumBatchedTokens` → `--max-num-batched-tokens`, default **256** — the
  chunked-prefill token budget per engine step.
- `vllmCommon.gpuMemoryUtilization` (0.0–1.0) → `--gpu-memory-utilization`. **llm-d-benchmark's
  default is `0.95`** (higher than vLLM's own upstream default of **`0.9`**). More GPU memory to
  the KV cache ⇒ higher concurrency, but raise cautiously — too high OOMs on load. On the
  **kind/CPU-sim path it is `0`, which SKIPS the flag entirely** (no GPU) — don't set it there.

### KV transfer & events — `vllmCommon.kvTransfer.*`, `vllmCommon.kvEvents.*`
For prefill/decode **disaggregation** and **prefix-cache-aware routing**:
- `vllmCommon.kvTransfer.enabled: true` + `.connector` (e.g. `NixlConnector`) + `.role`
  (`kv_both`/`kv_producer`/`kv_consumer`) — wires the cross-pod KV cache transfer. Set this
  when the user wants P/D disaggregation benefits in the numbers.
  - `vllmCommon.kvTransfer.extraConfig` is rendered into `kv_connector_extra_config` inside the
    generated `--kv-transfer-config` JSON — this is the knob for **`guides/tiered-prefix-cache`**
    CPU/disk **offloading** (connector `OffloadingConnector`, `kv_role: kv_both`, with
    `{num_cpu_blocks, cpu_bytes_to_use}`). Grounded in
    `config/scenarios/guides/tiered-prefix-cache.yaml` (it renders
    `--kv-transfer-config '{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":$(CPU_BYTES_TO_USE)}}'`).
- `vllmCommon.kvEvents.enabled: true` (+ `.publisher`, `.port`, `.topicPrefix`) — renders the
  `--kv-events-config` flag on the `vllm serve` command (defaults `publisher: zmq`, `port: 5557`,
  `topicPrefix: kv` per `config/templates/values/defaults.yaml`), emitting KV-cache events so the
  router can do precise prefix-cache-aware routing. This is what the
  **`guides/precise-prefix-cache-routing`** scenario turns on (read
  `config/scenarios/guides/precise-prefix-cache-routing.yaml`). Pair with a routing connector
  that consumes them.
**Tiered-KV / CPU-offload TRAPS — keys that silently do NOTHING (get these wrong and the run
looks fine but offloads nothing):**
- The CPU-block count for `OffloadingConnector` is **`vllmCommon.kvTransfer.extraConfig.num_cpu_blocks`**
  (renders into `kv_connector_extra_config` in the generated `--kv-transfer-config` JSON). The
  look-alike **`vllmCommon.flags.numCpuBlocks` is a no-op** — no template reads that path, so it is
  silently dropped (upstream `experiments/tiered-prefix-cache.yaml` documents it "silently no-op'd").
- **`vllmCommon.flags.cpuOffloadGb` does not exist** in the repos — don't author it. (vLLM's own
  `--cpu-offload-gb` offloads model **WEIGHTS**, not KV cache, and is not the tiered-KV mechanism
  nor exposed as a benchmark scenario knob.)
- The tiered CPU/disk KV path is the **`OffloadingConnector`** via `vllmCommon.kvTransfer.*`
  (`connector: OffloadingConnector` + `extraConfig {num_cpu_blocks, cpu_bytes_to_use}`), **NOT**
  `kvTransfer.connector: NixlConnector` — Nixl is the P/D-disaggregation transfer, a different path.

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

## Validation only checks SHAPE — it does NOT verify a flag NAME is real

This is the most important caveat. `write_and_validate_config` returning `valid: true` means
the authored YAML **passed structural (shape) validation** — top-level keys match the repo's
scenario format and it re-parses. It does **NOT** mean a vLLM flag name actually exists, nor
that the target vLLM version accepts it. In SIMULATE mode the downstream `plan`/`--dry-run`
no-ops too, so a fabricated flag can sail through the whole flow untouched.

**Never tell the user their supplied flags were "authored correctly", "valid", or "accepted"
on the strength of `valid: true` alone.** A flag the agent invented
or mis-remembered (e.g. `enablePrefixCachingV2`, `kvCacheSharingStrategy`,
`speculativeDecodeTokenBudget`, `enableChunkedPrefillV3` — none of which exist in the repo)
will pass shape validation and then fail at vLLM startup with `error: unrecognized arguments`.

To make this checkable, the tool result carries an additive **`unrecognized_flags`** list: the
override keys whose leaf name appears in **no** repo scenario example **and** not in stock
`defaults.yaml`. It is **advisory, non-fatal** — the tool never blocks on it (the agent warns,
it doesn't gate). When `unrecognized_flags` is non-empty:
- **Warn the user explicitly**: name the flags and say you could not corroborate them against
  the repo, so they may be typos or flags that don't exist in the target vLLM version, and that
  unknown flags are passed as-is and **fail at runtime** (`unrecognized arguments`).
- Do **not** claim the flags are valid. Offer to re-read `defaults.yaml` + a real scenario
  example to find the correct name, or to drop the flag.
- An empty `unrecognized_flags` means the name is at least used upstream
  (corroboration-against-repo-truth); an absent one (older result) means no corroboration.
  Either way you STILL cannot certify flag existence in the user's vLLM version — add the
  runtime-verification caveat whenever the user supplied flags you didn't read out of the
  repo yourself.

## Guardrails
- One scenario edit at a time when you're attributing a result to it — change the knob, plan,
  run, compare. Bundling many knobs makes the delta unattributable.
- If a knob you want isn't accepted, you guessed a field name — re-read `defaults.yaml` and a
  real scenario example, then re-author.
- `valid: true` is a SHAPE pass, not a flag-existence guarantee — surface any
  `unrecognized_flags` before claiming a flag is correct (see the validation caveat above).
