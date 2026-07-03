# Router / scheduler feature catalog + alias map + composability

Reach for this when a user names a **router / scheduler / KV feature** ("cpu-aware KV routing",
"HMA-aware", "tiered CPU+storage", "prefix-cache routing", "P/D") and you need the *canonical*
upstream name before designing a benchmark. This maps fuzzy user terms to real artifacts, flags
the ones that DON'T exist, and says which features compose. It is the router/scheduler side;
the SCENARIO side is `read_knowledge('welllit_path_advisor')`, the per-knob vLLM/KV authoring
side is `read_knowledge('vllm_overrides')`, and the experiment files are in
`read_knowledge('sweep_playbook')`. Always confirm names against `list_catalog` before use.

## Alias map — user term → real upstream artifact (or "does not exist")

- **"cpu-aware KV routing" / "CPU-tier KV routing"** → a real scorer plugin. In the EPP router
  config it is the scheduling plugin `type: prefix-cache-scorer` with instance `name:
  cpu-prefix-cache-scorer` (paired with a producer `type: approx-prefix-cache-producer`, `name:
  cpu-prefix-cache-producer`). See `llm-d/guides/tiered-prefix-cache/router/
  tiered-prefix-cache-cpu.values.yaml` (~lines 21-43) and the benchmark scenario
  `llm-d-benchmark/config/scenarios/guides/tiered-prefix-cache.yaml`. The identifier is always
  **hyphenated** (`cpu-prefix-cache-scorer`); there is no `cpu_prefix_cache` underscore form.
  The precise (KV-indexer) scorer is **tier-weighted**: a matched block's contribution is
  weighted by tier, defaults **gpu = 1.0, cpu = 0.8**, max weight across tiers if a block is on
  several (`llm-d/docs/architecture/advanced/kv-management/kv-indexer.md:133`).

- **"HMA" / "HMA-aware"** → **NO upstream artifact.** The acronym "HMA" (and "hybrid memory")
  has **zero hits** in both `llm-d/` and `llm-d-benchmark/`. The nearest *real* concept is
  **hybrid-attention-aware scoring**, which is explicitly **work-in-progress / target design**,
  not shipped (`kv-indexer.md:19` "Hybrid-attention-aware scoring is a work in progress"; `:151`
  "Hybrid attention (target design — work in progress)"). So do NOT design a benchmark around
  "HMA" from a guessed meaning — apply the unknown-term protocol
  (`read_knowledge('conversation_style')`): say plainly it has no upstream match and ask the user
  what they mean before proceeding.

- **"tiered CPU+storage" / "tiered cache" / "KV offload"** → these are **offload BACKENDS, not
  routing modes.** The real backends are native **OffloadingConnector**, **LMCache**,
  **MooncakeStore**, and **SGLang HiCache** (`llm-d/docs/architecture/advanced/kv-management/
  kv-offloader.md`; `llm-d/guides/tiered-prefix-cache/`). The KV-offload knob is
  `vllmCommon.kvTransfer.*` — read `read_knowledge('vllm_overrides')` for the exact override
  path (native offload is `connector: OffloadingConnector` + `kvTransfer.extraConfig`
  `{num_cpu_blocks, cpu_bytes_to_use}`); the ready-made sweep is
  `experiments/tiered-prefix-cache.yaml`. Don't conflate the offload backend (where KV bytes
  live) with the CPU-tier *scorer* above (how the router picks a pod).

## Composability matrix (verified in upstream EPP configs)

Scorers combine through the EPP **Filter-Score-Pick** pipeline (weighted sum;
`llm-d/docs/architecture/core/router/epp/scheduling.md`), so these compose:

- **precise (GPU-tier) prefix scoring + CPU-tier prefix scoring — COMPOSE.** One
  `schedulingProfiles` stack runs `gpu-prefix-cache-scorer` and `cpu-prefix-cache-scorer`
  side-by-side (plus queue-scorer, kv-cache-utilization-scorer, no-hit-lru-scorer) —
  `tiered-prefix-cache-cpu.values.yaml`.
- **P/D disaggregation + prefix scoring — COMPOSE.** `llm-d/guides/pd-disaggregation/router/
  pd-disaggregation.values.yaml` defines a `prefill` profile (prefill-filter → prefix-cache-scorer
  → queue-scorer → kv-cache-utilization-scorer) and a `decode` profile (decode-filter →
  active-request-scorer → prefix-cache-scorer) — both include the prefix scorer. Also composed in
  `agentic-serving`, `multimodal-serving/e-disaggregation`, and `wide-ep-lws` router values.

## Two weight axes — don't conflate them

- **Tier weights** (`gpu=1.0, cpu=0.8`) live *inside* the precise prefix scorer and weight a
  matched block by which memory tier holds it.
- **Scorer weights** (default `1:1:1:1:1` across Queue : KV-util : GPU-prefix : CPU-prefix : LRU,
  per `llm-d/guides/tiered-prefix-cache/benchmark-results/*.md`) weight the *scorers* against each
  other in the weighted sum. A user asking to "tune the CPU weight" could mean either — clarify.
