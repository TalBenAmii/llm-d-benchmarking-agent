# Playbook: choosing a deploy path

There are two ways an llm-d stack comes into being. For the MVP, only the first is
supported end-to-end; the others are described so you can set expectations honestly.

## 1. kind + simulated engine (MVP — supported)
`spec=cicd/kind`. Local kind cluster, CPU-only, `llm-d-inference-sim`. No GPU, no model
download, no HF token. This is the quickstart (`quickstart_playbook.md`). Use this for
"try it on my laptop", demos, and plumbing/SLO sanity checks.

## 2. Real deployment via llmdbenchmark specs (future)
`spec=examples/gpu` or `spec=guides/<name>` (e.g. `guides/optimized-baseline`). The
benchmark CLI stands up a real stack. These need GPUs and often a HuggingFace token for
gated models. Confirm specs with `list_catalog` and read the spec/guide with
`read_repo_doc` before promising anything. Do NOT attempt on the local kind node — it has
no GPUs and not enough CPU/RAM for the default sizes.

## 3. Hand-run llm-d guides + run_only.sh (not automated here)
The `llm-d` repo `guides/*` deploy via helm+kustomize and then benchmark an EXISTING stack
with `existing_stack/run_only.sh`. This is a different entry point than `llmdbenchmark` and
is out of MVP scope. Mention it exists if the user asks about the published guide numbers.

## How to decide
- User says "on my laptop / locally / just try it / no GPU" → path 1 (cicd/kind). Default.
- User has a GPU cluster and names a well-lit path → path 2 (future); set expectations that
  the agent's automated support is the kind/sim path for now.
- Always `probe_environment` first: if there's no GPU and we're on kind, path 1 is the only
  realistic option.
