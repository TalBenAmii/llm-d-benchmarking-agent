# Sweep validity — do my overrides actually apply, and did the sweep really vary anything?

Read this whenever you author a sweep OR before you trust a comparison. A sweep can complete,
produce clean reports, and show a "delta" that is **pure noise** because the treatments never
actually differed — the override keys silently didn't apply. This is the guard against that.
Companion: `read_knowledge('sweep_playbook')` / `read_knowledge('sweep_authoring')`.

## Author-time: keys that silently DON'T apply

### List-indexed dotted keys (`load.stages.0.*`) NEVER apply — in any path
The workload-profile override applier (`profile_renderer.apply_overrides`) walks **dicts only**:
for a dotted key it descends only while the current node is a dict, otherwise the whole key is
appended to `unmatched` and **dropped**. A segment like `stages.0` needs to index into a LIST, so
it can never match — the key is silently discarded. This holds for the **DoE experiment
run-treatment path too** (it calls the same applier), not just standalone profile rendering, and
it is test-pinned upstream. Concretely: `load.stages.0.session_rate` (and any `load.stages.N.…`)
does NOTHING. Upstream `experiments/otel-session-rate-sweep.yaml` ships exactly this key, so **as
shipped it does not vary the rate at all**.

**Workable alternatives** when the knob you want lives inside a list:
- Pick a knob that resolves to a **dict path** instead (top-level or nested-dict keys apply fine —
  e.g. `data.shared_prefix.num_groups`, `rate` at a level the profile exposes as a scalar dict key).
- If the value only exists as a list element, **vary the whole profile**: author one workload
  profile per level (each with its own `session_rate`) via `write_and_validate_config`, and sweep
  the `experiment.profile` override (a profile swap is a dict-level change) rather than a list index.
- Confirm the target key's shape with `inspect_workload_profile` first — if the field sits under a
  `stages:` list, a dotted list-index override will be dropped.

### Kustomize-mode deploys ignore setup/DoE treatments
When a scenario deploys via **kustomize** (guide-style deploys), `step_06_kustomize_deploy`
warns: *"DoE setup sweeps do NOT apply (use kustomize.patches); run/workload treatments do."* So:
- **Setup-phase factors do NOT apply** to a kustomize deploy. Express deployment changes via
  `kustomize.patches` (or `extraHelmValues` / `extraHelmSets` / `guideVariableOverrides`) instead.
- **Run/workload treatments DO still apply** — a run-parameter sweep works normally.
- `overlayPath` is a **patch/overlay directory** (resolved to absolute), **silently skipped unless
  it is a real directory** — it is NOT a backend/variant selector. Select backend/guide variants
  via the guide's **README commands** or **`guideVariableOverrides`**, not overlayPath.

### Don't bypass the DoE tool
Author sweeps with `generate_doe_experiment` (it validates keys and writes into the session
workspace). Never hand-write an experiment YAML to `/tmp` via `run_shell` and run it directly:
you lose the validation, and a key the tool would have flagged as unmatched just **silently won't
apply at runtime** — you'll compare treatments that never differed and never know.

## Post-run: the sweep-validity gate (run this BEFORE reporting any delta)
A completed sweep is not a valid comparison until you confirm the treatments actually differed.
Three cheap checks — if any fails, say the comparison is void rather than reporting a delta:

1. **Scan the run stderr / logs for dropped-override warnings.** `apply_overrides` reports
   `unmatched` keys ("… override key … did not match / was dropped"). Any unmatched key on a
   swept factor means that treatment ran with the DEFAULT — its "delta" is noise.
2. **Compare achieved load vs each treatment's intent.** Read `summary.load.rate_qps` / request
   totals per report. If two treatments meant to differ show the *same* achieved load, the knob
   didn't take.
3. **Diff the rendered per-treatment profiles.** If two treatments rendered **identical** workload
   profiles, the comparison between them is VOID — say so plainly; do not attribute the numeric
   difference to the (non-existent) config change.

A reported "+22.6% improvement" between two treatments whose configs were identical is measurement
noise, not a result. When the gate fails, tell the user the sweep didn't actually vary the factor,
show why (the dropped key / identical profiles), and offer a corrected grid — don't launder noise
into a recommendation. (Interpreting a *valid* delta: `read_knowledge('sweep_results')`.)
