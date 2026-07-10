# Model override (`-m` / `ExecuteInput.models`) — serving a model the spec doesn't pin

Every spec carries a **scenario-default model** (e.g. `cicd/kind` serves `facebook/opt-125m`
on CPU-sim; a GPU spec serves whatever its scenario declares). Usually you pick the spec and
take its model. But when the user wants a *specific* model that no convenient spec pins —
"benchmark Llama-3.1-8B", "compare opt-125m vs opt-350m on the same kind cluster" — you don't
need a bespoke spec. Pass the model id directly:

- Set the top-level `models` field on `execute_llmdbenchmark` to a single model id (a
  HuggingFace id like `meta-llama/Llama-3.1-8B` or a short name like `facebook/opt-125m`).
- The tool emits `-m <id>` after the subcommand. (`-m` is the short flag that works on
  `standup`, `plan`, `run`, and `experiment`; upstream spells it `--models` on
  standup/plan/experiment and `--model` on run, but `-m` is unambiguous everywhere.)
- Omit `models` to keep the spec's default. Don't pass `-m` "just to be explicit" if you mean
  the default — let the spec speak.

## WHICH model — your judgment, grounded here (no enumerable catalog)

There is **no on-disk `models` catalog** to validate against (unlike specs/harnesses/
workloads). The model name is grounded two ways, both judgment + validation, never a Python
`if/elif`:

1. **You** choose the id from the conversation. Prefer the **smallest model that answers the
   user's question** — on a kind/CPU-sim cluster only tiny models (e.g. `facebook/opt-125m`,
   `facebook/opt-350m`) actually serve; an 8B+ model needs real GPU memory and will fail
   capacity. Use the exact canonical HuggingFace id (`org/name`), not a colloquial name.
2. **The capacity pre-flight validates it.** `check_capacity` does the real grounding: a HF
   model-config lookup (does this id exist / is it reachable?), the sizing math (will it fit
   the configured accelerator?), and the gated-access check (can the backend's token pull it?).

## The pre-flight MUST see the SAME model — the one rule that matters

A model override changes what `standup` deploys, so the "will this fit?" pre-flight is only
meaningful if it checks the **identical** model. Before a standup/run with `models=<id>`:

> Call `check_capacity(spec=<spec>, overrides={'model': '<id>'})` with the **same** `<id>`.

If you pass `-m` to the standup but let `check_capacity` size the spec's stock default, you've
validated the wrong thing — a 70B override sails past a pre-flight that sized a 125M default,
then OOMs minutes into the deploy. Keep them in lockstep:

1. User asks for model `X`.
2. `propose_session_plan` is approved.
3. `check_capacity(spec=…, overrides={'model': 'X'})` → read the verdict
   (`knowledge/capacity.md`): does `X` fit, and can the token pull it if gated?
4. Only if feasible (and authorized, for a gated model): `execute_llmdbenchmark(
   subcommand='standup', spec=…, models='X', …)`.

If the user later changes the model mid-session, re-run `check_capacity` with the new id
before the next standup. The override and the pre-flight override are always the same string.

## Notes

- The id is constrained by the allowlist (`model_id`: the safe `org/name` charset) and the
  metacharacter screen — a shell-dangerous value is refused before any approval prompt.
- `-m` does not change a command's mode: a `standup -m …` is still mutating (needs approval);
  a `plan -m …` / `--dry-run` is still a read-only preview you can use to confirm the override
  renders before committing to a real standup.
- One model id per standup. To compare models, do separate standups (or a DoE `setup_factor`
  on the model key) and compare the reports — don't try to pack several into `-m`.
