# Convert an llm-d guide into a benchmark scenario (LLMDBENCH_* mapping + standard practices)

This is the JUDGMENT half of `convert_guide_to_scenario`. The tool is pure mechanism: it
emits an `ai.<name>.sh` (sorted, shell-quoted `export LLMDBENCH_*` lines) plus a validatable
`ai.<name>.yaml` scenario twin + `ai.<name>.spec.yaml` into the **session workspace** — never
the read-only repo. **WHICH** `LLMDBENCH_*` vars a guide maps to, and the standard practices to
apply, live here (mirroring upstream `skills/convert-guide/references/mappings.md` +
`templates.md`). You read the guide yourself, resolve the mapping with this file, then call the
tool with the resolved `env` map.

## Workflow

1. **Read the guide.** Use `read_repo_doc` for an in-repo guide path, or
   `run_shell("git clone ...")` then read the cloned files, or fetch the URL the user
   gave. A guide is either **Helm-values-based** (`ms-*/values.yaml` ModelService +
   `gaie-*/values.yaml` GAIE + `helmfile.yaml(.gotmpl)`) or **kustomize-based**
   (`kustomization.yaml` + JSON patches). Track each value's source file + line numbers for the
   `sources` provenance map.
2. **Read the live defaults.** Only set a var when the guide differs from the framework default
   (`config/templates/values/defaults.yaml` in the benchmark repo — read it at runtime;
   representative defaults below. The old `setup/env.sh` is gone; upstream's convert-guide
   skill still cites it).
3. **Map each value** to its `LLMDBENCH_*` var (tables below).
4. **Apply the standard practices** (next section) regardless of what the guide says.
5. **Call `convert_guide_to_scenario`** with `name`, the resolved `env` map, optional `sources`,
   and (when you want richer knobs gated) a `scenario` dotted-knob override for the YAML twin.
6. **Gate** the twin: `execute_llmdbenchmark(subcommand='plan', spec=<spec_path>,
   flags={'dry_run': True})`. A clean plan/--dry-run is the acceptance gate.

## Standard practices (ALWAYS apply, regardless of the guide)

- `LLMDBENCH_VLLM_MODELSERVICE_DECODE_MODEL_COMMAND=custom` — always use the custom command.
- `LLMDBENCH_VLLM_COMMON_PREPROCESS` = `python3 /setup/preprocess/set_llmdbench_environment.py;
  source $HOME/llmdbench_env.sh`; set `LLMDBENCH_VLLM_MODELSERVICE_DECODE_PREPROCESS` to it.
- The decode `EXTRA_ARGS` heredoc starts with the decode preprocess, then
  `vllm serve /model-cache/models/REPLACE_ENV_LLMDBENCH_DEPLOY_CURRENT_MODEL ...`.
- Include the `preprocesses` volume (configMap `llm-d-benchmark-preprocesses`, `defaultMode:
  0755`, FIRST in the list) and its mount at `/setup/preprocess`.
- **REPLACE_ENV placeholders**: inside any `EXTRA_ARGS` / `EXTRA_VOLUMES` /
  `EXTRA_VOLUME_MOUNTS` heredoc, any value that HAS a `REPLACE_ENV_*` placeholder MUST use the
  placeholder, never a literal — this enables experiment-level overrides / sweeps. The only
  exception is a value with NO corresponding `LLMDBENCH_*` var (e.g. `--kv-transfer-config`
  JSON, `--enable-prefix-caching` booleans, custom env names) — those stay literal.
- **Port architecture**: the proxy/sidecar listens on `INFERENCE_PORT` (8000) and forwards to
  vLLM on `METRICS_PORT` (8200). `vllm serve --port` uses
  `REPLACE_ENV_LLMDBENCH_VLLM_COMMON_METRICS_PORT`.
- **Network resource**: always `LLMDBENCH_VLLM_COMMON_NETWORK_RESOURCE=auto` (ignore the guide's
  literal) — enable runtime auto-detection. Same for the decode/prefill `_NETWORK_RESOURCE`.
- **GPU count is auto-calculated** from `tensor_parallelism * data_local_parallelism`. Do NOT
  set `resources.limits."nvidia.com/gpu"` — set the parallelism vars or
  `..._ACCELERATOR_NR=auto`.
- **Completeness**: never silently drop guide config. ALL `env:` -> `..._ENVVARS_TO_YAML`, ALL
  `volumeMounts:` -> `..._EXTRA_VOLUME_MOUNTS`, ALL `volumes:` -> `..._EXTRA_VOLUMES`, ALL
  vLLM `args:` -> `..._EXTRA_ARGS`. If a value can't be mapped, note it in a `sources` comment.

## Common REPLACE_ENV placeholders

`REPLACE_ENV_LLMDBENCH_DEPLOY_CURRENT_MODEL`, `..._VLLM_COMMON_MAX_MODEL_LEN`,
`..._VLLM_COMMON_BLOCK_SIZE`, `..._VLLM_COMMON_METRICS_PORT`, `..._VLLM_COMMON_INFERENCE_PORT`,
`..._VLLM_COMMON_NIXL_SIDE_CHANNEL_PORT`, `..._VLLM_COMMON_SHM_MEM`,
`..._VLLM_COMMON_ACCELERATOR_MEM_UTIL`, `..._VLLM_MODELSERVICE_DECODE_TENSOR_PARALLELISM`,
`..._VLLM_MODELSERVICE_DECODE_ACCELERATOR_MEM_UTIL`, `..._VLLM_MODELSERVICE_DECODE_PREPROCESS`
(and the matching `PREFILL_*`).

## Mapping tables (Helm/kustomize path -> LLMDBENCH_*)

### Model (ModelService `modelArtifacts.*`)

| Guide path | LLMDBENCH var |
|---|---|
| `modelArtifacts.name` | `LLMDBENCH_DEPLOY_MODEL_LIST` |
| `modelArtifacts.size` | `LLMDBENCH_VLLM_COMMON_PVC_MODEL_CACHE_SIZE` |
| `modelArtifacts.authSecretName` | `LLMDBENCH_VLLM_COMMON_HF_TOKEN_NAME` |

### Decode / Prefill stage (replace `DECODE` with `PREFILL` for the prefill stage)

| Guide path | LLMDBENCH var |
|---|---|
| `decode.create=false` / `decode.replicas` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_REPLICAS` (0 if create=false) |
| `decode.parallelism.tensor` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_TENSOR_PARALLELISM` |
| `decode.parallelism.data` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_DATA_PARALLELISM` |
| `decode.parallelism.dataLocal` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_DATA_LOCAL_PARALLELISM` |
| `decode.parallelism.workers` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_NUM_WORKERS_PARALLELISM` |
| `decode.containers[0].resources.requests.cpu` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_CPU_NR` |
| `decode.containers[0].resources.requests.memory` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_CPU_MEM` |
| `decode.containers[0].modelCommand` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_MODEL_COMMAND` (always `custom`) |
| `decode.containers[0].args` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_EXTRA_ARGS` (heredoc, REPLACE_ENV) |
| `decode.containers[0].env` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_ENVVARS_TO_YAML` |
| `decode.containers[0].volumeMounts` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_EXTRA_VOLUME_MOUNTS` |
| `decode.volumes` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_EXTRA_VOLUMES` |
| `decode.volumes[?name=dshm].emptyDir.sizeLimit` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_SHM_MEM` |
| `decode.schedulerName` | `LLMDBENCH_VLLM_COMMON_POD_SCHEDULER` |
| `decode.containers[name='vllm'].image` | `LLMDBENCH_VLLM_MODELSERVICE_DECODE_IMAGE` (stage-specific override) |

### Common vLLM + launch args

| Guide path / vLLM arg | LLMDBENCH var |
|---|---|
| `multinode` (LWS CRD present) | `LLMDBENCH_VLLM_MODELSERVICE_MULTINODE=true` |
| `routing.servicePort` / `inferencePool.targetPortNumber` | `LLMDBENCH_VLLM_COMMON_INFERENCE_PORT` |
| `routing.proxy.enabled` | `LLMDBENCH_LLMD_ROUTINGSIDECAR_ENABLED` |
| `routing.proxy.connector` | `LLMDBENCH_LLMD_ROUTINGSIDECAR_CONNECTOR` |
| `accelerator.type` | `LLMDBENCH_VLLM_COMMON_ACCELERATOR_RESOURCE` |
| `--port` | `LLMDBENCH_VLLM_COMMON_METRICS_PORT` |
| `--max-model-len` | `LLMDBENCH_VLLM_COMMON_MAX_MODEL_LEN` |
| `--block-size` | `LLMDBENCH_VLLM_COMMON_BLOCK_SIZE` |
| `--gpu-memory-utilization` | `LLMDBENCH_VLLM_COMMON_ACCELERATOR_MEM_UTIL` |
| `--enable-prefix-caching` / `--kv-transfer-config` / `--enforce-eager` | (literal in `DECODE_EXTRA_ARGS`) |

### GAIE (`gaie-*/values.yaml`)

| Guide path | LLMDBENCH var |
|---|---|
| `inferenceExtension.image.tag` | `LLMDBENCH_LLMD_INFERENCESCHEDULER_IMAGE_TAG` |
| `inferenceExtension.pluginsConfigFile` | `LLMDBENCH_VLLM_MODELSERVICE_GAIE_PLUGINS_CONFIGFILE` |
| `inferenceExtension.pluginsCustomConfig` | `LLMDBENCH_VLLM_MODELSERVICE_GAIE_CUSTOM_PLUGINS` (extract the ENTIRE YAML) |
| `provider.name` / `gateway.gatewayClassName` | `LLMDBENCH_VLLM_MODELSERVICE_GATEWAY_CLASS_NAME` (istio/kgateway/gke) |

### Chart versions (from `helmfile.yaml(.gotmpl)` `releases[].version`)

| Release | LLMDBENCH var |
|---|---|
| `llm-d-infra` | `LLMDBENCH_VLLM_INFRA_CHART_VERSION` |
| `llm-d-modelservice` | `LLMDBENCH_VLLM_MODELSERVICE_CHART_VERSION` |
| `inferencepool` (GAIE) | `LLMDBENCH_VLLM_GAIE_CHART_VERSION` |

### Container image parsing

`ghcr.io/llm-d/llm-d-cuda:v0.5.0` -> `LLMDBENCH_LLMD_IMAGE_REGISTRY=ghcr.io`,
`LLMDBENCH_LLMD_IMAGE_REPO=llm-d`, `LLMDBENCH_LLMD_IMAGE_NAME=llm-d-cuda`,
`LLMDBENCH_LLMD_IMAGE_TAG=v0.5.0`. Always include `_IMAGE_TAG` if the guide pins an explicit
version; only set registry/repo/name when they differ from the default `ghcr.io/llm-d/llm-d-cuda`.

## Benchmark framework defaults (recorded into the .sh)

- `LLMDBENCH_HARNESS_NAME=inference-perf` (the converter default; pass `harness` to override).
- `LLMDBENCH_HARNESS_EXPERIMENT_PROFILE=sanity_random.yaml` (pass `profile` to override).
- Representative defaults — only override when the guide differs (read
  `config/templates/values/defaults.yaml` for the authoritative live values): `MAX_MODEL_LEN=16384`, `BLOCK_SIZE=64`, `ACCELERATOR_MEM_UTIL=0.95`,
  `ACCELERATOR_NR=auto`, `INFERENCE_PORT=8000`, `METRICS_PORT=8200`, `MULTINODE=false`,
  `GATEWAY_CLASS_NAME=istio`.

## Unmappable values (note in `sources`, don't fabricate a var)

`modelArtifacts.labels` (framework-set), `modelArtifacts.uri` (derived `pvc://`/`hf://`),
`fullnameOverride`, liveness/readiness/startup probes (framework-managed),
`resources.limits."nvidia.com/gpu"` (auto-calculated), `monitoring.podmonitor.*` (omitted).

## The validatable YAML twin (`scenario` override)

`ai.<name>.sh` is the upstream-shaped artifact but a bare `.sh` is NOT consumable by the
policy-allowed determinism gate (that gate takes a YAML `--spec` whose `scenario_file.path` is a
YAML). So the tool ALSO authors a YAML scenario twin + companion spec it CAN gate. Omit
`scenario` for a minimal twin (just the name). To gate richer knobs, pass `scenario` as
dotted upstream scenario field paths (e.g. `{'model.shortName': 'qwen3-32b',
'decode.parallelism.tensor': 2, 'vllmCommon.flags.enforceEager': true}`) — the SAME shape as
`write_and_validate_config(artifact_type='scenario')`; see `vllm_overrides`. The twin is
SHAPE-validated against the repo's live scenario examples before it is written.

## Deploying the converted guide

After a clean plan/--dry-run, stand up DIRECTLY off the workspace YAML twin:
`execute_llmdbenchmark(subcommand='standup', spec=<spec_path>)` (mutating — approval-gated).
The upstream `standup -c/--scenario ai.<name>.sh` route is NOT modeled by this agent; the
gate-able YAML `--spec` is the supported path.
