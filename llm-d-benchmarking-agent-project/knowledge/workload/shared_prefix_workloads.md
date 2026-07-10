# Shared-prefix workloads: match the SHARED:UNIQUE ratio, not the total input length

The inference-perf `shared_prefix` workloads (RAG / long-system-prompt shapes) are keyed under
`data.shared_prefix` with these fields (verified against
`llm-d-benchmark/workload/profiles/inference-perf/shared_prefix_*.yaml.in`):

- `system_prompt_len` — length (tokens) of the **shared** prefix (system prompt / retrieved context
  reused across a group). This is the part prefix caching can hit.
- `question_len` — length (tokens) of the **unique** per-request part.
- `output_len` — target generated tokens per request.
- `num_groups` — number of distinct shared prefixes.
- `num_prompts_per_group` — unique questions per prefix (users ≈ `num_groups * num_prompts_per_group`).
- (`enable_multi_turn_chat: true` on the multi-turn variant only — appends chat context per turn.)

There is **no `shared_prefix_length` key** — don't invent one. The four stock shapes:

| profile | system_prompt_len (shared) | question_len (unique) | output_len | groups × prompts | shared fraction of input |
|---|---|---|---|---|---|
| `shared_prefix_synthetic` | 2048 | 256 | 256 | 32 × 32 | 2048/2304 ≈ **89%** |
| `shared_prefix_synthetic_short` | 2048 | 256 | 256 | 32 × 32 (one 60s stage) | ≈ **89%** (smoke) |
| `shared_prefix_synthetic_heavy` | 4000 | 256 | 256 | 32 × 32 | 4000/4256 ≈ **94%** |
| `shared_prefix_multi_turn_chat` | 1000 | 200 | 200 | 1 × 20 (multi-turn) | 1000/1200 ≈ **83%** |

## The honesty rule: the SHARED:UNIQUE ratio is the whole point

Prefix-cache benefit is set by **what fraction of the input is reused**, NOT by total input length.
Every stock shape is 83–94% shared — far heavier than a typical RAG query, where the retrieved chunks
are large and *unique per request* and only a small system prompt is shared. Picking a stock profile by
total-input-length alone (or defaulting to `..._heavy`) **overstates** the KV-cache hit rate and makes
the benchmark dishonest.

So: identify the user's shared vs unique split and **override the keys to their ratio**. Example — a RAG
shape of "6k in (1k shared) / 300 out" is only ~17% shared (1000/6000), nowhere near heavy's 94%.
Override to **1k shared / 5k unique / 300 out**:

```yaml
data:
  type: shared_prefix
  shared_prefix:
    system_prompt_len: 1000   # the shared/retrieved-once prefix (~17% of input)
    question_len: 5000        # the unique per-request retrieved chunks + query
    output_len: 300
    # num_groups / num_prompts_per_group set the working-set size (how much distinct
    # prefix must fit in cache) — keep the stock 32×32 unless the user's scale differs.
```

Always `inspect_workload_profile` first (a filename is not its shape), then confirm the override with
the user. When they can't state a split, ASK — don't assume the stock (heavy) ratio.
