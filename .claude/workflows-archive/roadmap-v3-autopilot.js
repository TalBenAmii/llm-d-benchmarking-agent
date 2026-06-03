export const meta = {
  name: 'roadmap-v3-autopilot',
  description: 'Autonomously implement Roadmap v3 (Phases 19-26: the MISSING proposal features) for the llm-d-benchmarking-agent as fresh-context agents in isolated git worktrees, adversarially verify each (3 lenses), serially integrate into feature/roadmap-v3 (off main), and FINALLY merge feature/roadmap-v3 into main + commit it (gated on a green, ruff-clean, mypy-clean suite; aborts and leaves main untouched on any failure). Every phase is a separate agent context; the orchestrator holds only short summaries. Resumable via resumeFromRunId.',
  whenToUse: 'Complete the proposal-coverage gaps (DOE generation, well-lit-path advisor, log streaming, checkpoint/resume, resource mgmt, health-check, analyzer metrics, inference-sim integration tests) autonomously AND land them on main. Run after Roadmap v2 is merged into main.',
  phases: [
    { title: 'Prep', detail: 'create feature/roadmap-v3 integration worktree off main, copy .env, record baseline, detect already-done v3 phases' },
    { title: 'Wave 1', detail: 'P19 DOE generator + P20 well-lit advisor + P25 analyzer metrics — parallel implement+verify, serial integrate' },
    { title: 'Wave 2', detail: 'P21 log streaming + P23 resource management — parallel implement+verify, serial integrate' },
    { title: 'Wave 3', detail: 'P22 DOE checkpoint/resume + P24 endpoint health-check — parallel implement+verify, serial integrate' },
    { title: 'Wave 4', detail: 'P26 llm-d-inference-sim integration tests — solo, exercises the new features' },
    { title: 'Finalize', detail: 'a subagent merges feature/roadmap-v3 into main and commits it, gated on a green + ruff-clean + mypy-clean suite (aborts, leaving main untouched, on any failure); no push' },
  ],
}

// ----------------------------------------------------------------------------
// Constants (verified against on-disk state)
// ----------------------------------------------------------------------------
const MONO  = '/home/tal/kind-quickstart-guide'          // main checkout: shares .git, has POPULATED read-only sibling repos
const PROJ  = 'llm-d-benchmarking-agent-project'
const INTEG = 'feature/roadmap-v3'                        // integration branch (NEVER main) — branched off main
const BASE  = 'main'                                      // v2 is merged into main (+ the conftest SIMULATE=0 / pytest-timeout hardening); build v3 on main
const HOME  = '/home/tal/kqg-v3-home'                     // integration worktree — CREATED in Prep
const PDIR  = HOME + '/' + PROJ                           // integration project dir
const VENV  = MONO + '/' + PROJ + '/.venv/bin/python'     // reuse the existing venv for ALL runs

// phase id -> slug; WAVES are conflict/dependency ordered. Disjoint authoring/analyzer
// phases first; orchestrator run-loop phases sequenced (21 before 22 — both touch the run
// loop); integration tests last so they can exercise the new features.
const SLUG  = { 19:'doe-gen', 20:'welllit-advisor', 21:'log-stream', 22:'checkpoint', 23:'resource-mgmt', 24:'health-check', 25:'analyzer-metrics', 26:'sim-integration' }
const TITLE = {
  19:'DOE experiment-file generator + token-characteristics elicitation',
  20:'Well-lit-path advisor',
  21:'Real-time benchmark-pod log streaming',
  22:'DOE checkpoint/resume for long sweeps',
  23:'Resource management: node affinity / GPU selection / anti-starvation',
  24:'Endpoint health-check before submit (+ optional auto-standup)',
  25:'Analyzer metric completeness: KV-cache hit rate, schedule delay, GPU utilization',
  26:'llm-d-inference-sim integration tests (opt-in)',
}
const WAVES = [ [19,20,25], [21,23], [22,24], [26] ]

const wt = (id) => '/home/tal/kqg-v3-p' + id + '-' + SLUG[id]
const br = (id) => 'feature/roadmap-v3-p' + id + '-' + SLUG[id]

// ----------------------------------------------------------------------------
// Schemas
// ----------------------------------------------------------------------------
const STATUS_SCHEMA = { type:'object', additionalProperties:false,
  properties:{ done:{type:'array', items:{type:'integer'}}, base:{type:'string'}, notes:{type:'string'} }, required:['done'] }

const IMPL_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, branch:{type:'string'}, worktree:{type:'string'},
  summary:{type:'string'}, filesChanged:{type:'array', items:{type:'string'}},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  ok:{type:'boolean'}, blocker:{type:'string'} },
  required:['phase','branch','worktree','summary','passCount','failCount','ok'] }

const VERDICT_SCHEMA = { type:'object', additionalProperties:false, properties:{
  lens:{type:'string'}, acceptable:{type:'boolean'},
  blocking:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['lens','acceptable','blocking'] }

const INTEG_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, merged:{type:'boolean'}, fullSuitePassed:{type:'boolean'},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  timedOut:{type:'boolean'}, notes:{type:'string'} },
  required:['phase','merged','fullSuitePassed','passCount','failCount'] }

const FINALIZE_SCHEMA = { type:'object', additionalProperties:false, properties:{
  merged:{type:'boolean'}, suitePassed:{type:'boolean'}, lintPassed:{type:'boolean'},
  typePassed:{type:'boolean'}, mergeCommit:{type:'string'},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  notes:{type:'string'} },
  required:['merged','suitePassed','passCount','failCount'] }

// ----------------------------------------------------------------------------
// Uniform test command: PYTHONPATH="$PWD" so the worktree's app wins over the shared
// venv's editable finder; REPOS_DIR=MONO so sibling-repo tests run; timeout 600 so a
// hung test returns 124 (treated as a failure to make hermetic) instead of wedging.
// ----------------------------------------------------------------------------
const TESTCMD = (projDir) =>
  'cd ' + projDir + ' && REPOS_DIR=' + MONO + ' PYTHONPATH="$PWD" timeout 600 ' + VENV + ' -m pytest tests/ -q'

const TRAILER = 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>'

// ----------------------------------------------------------------------------
// Per-phase specs (authoritative; carried inline). NO backticks / no ${ } inside.
// ----------------------------------------------------------------------------
const SPEC = {
19: `Phase 19 — DOE experiment-file generator + token-characteristics elicitation.
GOAL: let the agent AUTHOR a Design-of-Experiments matrix file (proposal stretch #1), not just run one.
BUILD:
- A new tool (register in app/tools/registry.py + app/tools/schemas.py), e.g. generate_doe_experiment, that takes agent-chosen FACTORS (each: name + list of LEVELS) and emits the cross-product of TREATMENTS as a valid experiment YAML written to the workspace. The cross-product expansion + YAML emission is pure MECHANISM; WHICH factors/levels to sweep is the agent's judgment supplied as tool args informed by knowledge (do NOT hardcode factor choices in Python if/elif).
- Validate the generated file structurally against the repo's experiment example format (read llm-d-benchmark experiment examples at runtime via the existing repo-reading helpers; do NOT vendor copies). Reuse app/tools/config_artifact.py validation patterns where possible.
- Update knowledge/sweep_playbook.md (add knowledge as needed) so the agent knows how to pick factors/levels for common questions (e.g. optimal prefill/decode ratio) and to ELICIT token characteristics (input/output length distributions, system-prompt reuse ratio -> prefix sharing) during the interview. Judgment in knowledge, not Python.
ACCEPTANCE: given factors+levels the tool writes a correct treatments matrix (full cross-product, deduped, named) as a structurally-valid experiment YAML; knowledge guides factor choice + token-characteristics elicitation; no factor/level decision logic in Python.
HERMETIC TEST: unit-test the generator: 2 factors x (3 levels, 2 levels) -> 6 treatments with correct fields; an empty/invalid factor set is rejected; the emitted YAML parses and matches the expected structure. No network/cluster.
THIN-CODE NOTE: cross-product = mechanism; factor/level/elicitation judgment = knowledge.`,

20: `Phase 20 — Well-lit-path advisor.
GOAL: recommend WHICH llm-d well-lit-path scenario to benchmark based on workload shape (proposal stretch).
BUILD:
- A knowledge file (e.g. knowledge/welllit_path_advisor.yaml) mapping workload characteristics -> recommended well-lit-path scenario/guide + rationale, e.g.: prefix-heavy chat -> precise-prefix-cache-aware scheduling; long-context RAG -> prefill/decode (P/D) disaggregation; high-throughput batch -> inference-scheduling; default/sanity -> cicd/kind. Include the SIGNAL(s) that select each (prefix-reuse ratio, context length, concurrency, SLO emphasis).
- Wire the knowledge into the agent (add to the knowledge index/loader so it is in the system prompt) so the agent can advise scenario selection. The JUDGMENT must live in the YAML, not Python; a thin read affordance is fine.
- Update knowledge/deploy_path_playbook.md to reference the advisor.
- Recommended scenario names must correspond to REAL specs/guides discoverable in the catalog; validate names are well-formed and, where applicable, present in the frozen catalog snapshot. For GPU-only guides absent from the kind catalog, clearly mark them as deploy-path guidance.
ACCEPTANCE: the advisor knowledge exists, covers the main archetypes (chat/prefix-heavy, long-context/RAG, throughput/batch, code, agentic), references real scenario/guide identifiers, and is loaded into the agent context; no scenario-selection logic in Python.
HERMETIC TEST: the YAML parses, has an entry per archetype with the required fields; a test asserts referenced identifiers are well-formed and (where applicable) present in the catalog snapshot; the loader includes the new file. No network.
THIN-CODE NOTE: pure knowledge addition; the advisor IS data.`,

21: `Phase 21 — Real-time benchmark-pod log streaming.
GOAL: surface benchmark-pod logs to the user in real time during a run (proposal §3.3/§4), not just at the end.
BUILD:
- Wire the existing app/orchestrator/kube.py stream_logs / logs(follow=True) capability INTO the orchestrator run loop (app/orchestrator/controller.py run_with_retries and run_sweep) so that, while a Job runs, pod log lines are emitted as live events through the SAME output/event mechanism the UI already renders (do NOT invent a new transport). Run the log tail as a background async task cancelled when the Job reaches a terminal state; log streaming must NEVER block or break the run/watch loop.
- Keep the streamed lines on the existing allowlisted kubectl logs path (security/allowlist.yaml already permits kubectl logs -f); argv-only, shell=False.
- Guard the stream (pod-not-ready, rotation, cancellation) so a failing tail does not fail the run.
ACCEPTANCE: during a run, benchmark-pod log lines are emitted as events as produced; the tail is cancelled on terminal state and its failure never fails the run; existing run/sweep behavior otherwise unchanged.
HERMETIC TEST: with FakeKubeClient, have logs(follow=True) yield a sequence of lines; assert run_with_retries (and a sweep treatment) surfaces those lines as events in order during the run, and that a raised error in the tail is isolated (run still succeeds). No real cluster.
THIN-CODE NOTE: mechanism only. INTEGRATOR NOTE: controller.py is structural — reconcile with P22's run-loop edits (P21 lands first).`,

22: `Phase 22 — DOE checkpoint/resume for long sweeps.
GOAL: a sweep interrupted at treatment k/N resumes from k+1, consistent with the stateless design (proposal §3.3/§4).
BUILD:
- Persist sweep progress to a K8s resource (e.g. a ConfigMap, or annotations/labels on the sweep's Jobs) as the SOURCE OF TRUTH: which treatments are completed (+ outcome) and which are in-flight. This must live in the cluster, NOT in local workspace files (stateless design).
- On reconstruct/resume (extend controller.py reconstruct + run_sweep), read the checkpoint and SKIP already-completed treatments, running only the remainder; merge prior outcomes into the final sweep result.
- Make resume idempotent: re-running a sweep with the same sweep id continues rather than restarting; completed treatments are not re-run.
ACCEPTANCE: a sweep that records k of N completed, then is re-invoked/reconstructed, runs only the remaining N-k treatments and returns a complete merged result; the checkpoint lives in cluster state; no duplicate runs.
HERMETIC TEST: with FakeKubeClient, drive a sweep, simulate interruption after k treatments (checkpoint persisted via the fake), then resume and assert only treatments k+1..N execute and the merged result covers all N. No real cluster.
THIN-CODE NOTE: checkpoint store + skip logic = mechanism. INTEGRATOR NOTE: controller.py run loop is structural — reconcile with P21's streaming edits.`,

23: `Phase 23 — Resource management: node affinity / GPU selection / anti-starvation.
GOAL: benchmark Jobs request the right hardware and do not starve the llm-d stack being measured (proposal §4).
BUILD:
- Extend the JobSpec model + app/orchestrator/job.py build_job_manifest with OPTIONAL scheduling fields: nodeSelector, affinity / anti-affinity, tolerations, and a GPU resource request (e.g. nvidia.com/gpu) / GPU-type label. When unset, behavior is EXACTLY as today (generic cpu/memory only) — do not break existing tests or change the baseline manifest.
- Provide anti-affinity / placement so a benchmark Job avoids co-scheduling onto the nodes serving the measured llm-d stack (e.g. a configurable avoid-label or pod anti-affinity). The agent supplies GPU type + placement at plan time informed by knowledge — manifest assembly is mechanism, the choice is judgment.
- Add knowledge (e.g. knowledge/resource_management.md) on picking GPU type / placement / quotas for common scenarios.
- Widen security/allowlist.yaml only if new kubectl surface is required (data, not Python).
ACCEPTANCE: when scheduling fields are supplied, the rendered Job manifest carries the correct nodeSelector/affinity/tolerations/GPU resource in the right spec paths; when omitted, the manifest is byte-for-byte the current baseline; knowledge explains the choices; no placement decision logic in Python if/elif.
HERMETIC TEST: build_job_manifest with GPU type + nodeSelector + anti-affinity -> assert the manifest dict carries them; build with none -> identical to the current baseline manifest. Pure unit test, no cluster.
THIN-CODE NOTE: manifest fields = mechanism; GPU/placement choice = knowledge/plan args.`,

24: `Phase 24 — Endpoint health-check before submit (+ optional auto-standup).
GOAL: don't submit a benchmark against an unready stack; optionally bring one up (proposal §3.3 dependency management).
BUILD:
- Before submitting a benchmark Job (in the orchestrate path / controller pre-submit), GATE on inference-endpoint READINESS: verify the inference service/endpoint is healthy (a readiness/health check of the serving pods/endpoints, or an endpoint probe) — go BEYOND today's mere pod-presence probe. Keep it argv-only / allowlisted; widen security/allowlist.yaml via YAML if a new check command is needed.
- If no healthy stack is present, surface a clear STRUCTURED not-ready result AND offer to trigger standup (approval-gated, via the existing execute/standup path). The DECISION to stand up is the agent's/user's (knowledge + approval); the mechanism is the readiness check + the (approval-gated) standup call. Do NOT auto-mutate without approval.
- Add knowledge (extend knowledge/orchestrator.md or preconditions.md) on the readiness gate and when to stand up.
ACCEPTANCE: submission is gated on a real endpoint-readiness check (not just pod presence); an unready stack yields a structured not-ready outcome with a standup suggestion; standup remains approval-gated; a ready stack proceeds as today.
HERMETIC TEST: with fakes, an unready endpoint -> submission blocked with a structured not-ready result and NO mutation; a ready endpoint -> proceeds; assert auto-standup is only PROPOSED, never run without approval. No real cluster.
THIN-CODE NOTE: readiness check = mechanism; the standup decision = agent judgment + approval gate.`,

25: `Phase 25 — Analyzer metric completeness: KV-cache hit rate, schedule delay, GPU utilization.
GOAL: extract + surface the §3.4 standard metrics currently ignored.
BUILD:
- Extend app/validation/report.py (summarize_report) and app/validation/analysis.py to PARSE and SURFACE, when present in the Benchmark Report v0.2 / harness-native output: KV-cache hit rate, schedule delay, and GPU utilization. Read field names from the live BR v0.2 schema / report structure at runtime; gracefully return None / omit when a harness does not provide them (NEVER fabricate).
- Include these in the human summary and, where sensible, as OPTIONAL Pareto/analysis dimensions (e.g. KV-cache hit rate as an informational objective). Keep goodput/SLO behavior unchanged.
- Update knowledge/results_interpretation.md + analysis.md to explain the new metrics in plain language.
ACCEPTANCE: a report carrying these fields has them extracted and surfaced in the summary; a report lacking them degrades gracefully (None/omitted, no crash); existing metric/goodput/Pareto behavior preserved.
HERMETIC TEST: a report fixture WITH kv-cache hit rate / schedule delay / GPU util -> all three surfaced; a fixture WITHOUT them -> None/omitted and no error; existing analyzer tests still pass. Pure fixtures, no cluster.
THIN-CODE NOTE: parsing = mechanism; interpretation guidance = knowledge.`,

26: `Phase 26 — llm-d-inference-sim integration tests (opt-in).
GOAL: the proposal's explicit "integration tests with llm-d-inference-sim" — exercise the analyze/compare path against a REAL mock report, without breaking the hermetic default suite.
BUILD:
- Add an OPT-IN integration test layer (e.g. tests/integration/) that, when enabled by an env flag (e.g. LLMD_SIM_INTEGRATION=1) AND when llm-d-inference-sim is available, stands up llm-d-inference-sim (the mock inference server) and runs a small benchmark/analyze/compare against it end to end; otherwise the test SKIPS cleanly (pytest skipif / importorskip). The DEFAULT suite must stay fully hermetic and green with NO new required dependency.
- Add a NON-GATING CI job (mirror the existing opt-in live-eval job in .github/workflows/agent-flow-validation.yml) that runs the sim integration on manual dispatch; never block the build.
- Document how to run it (reference from CONTRIBUTING/TROUBLESHOOTING or VALIDATION docs).
ACCEPTANCE: an opt-in integration test exists that genuinely exercises analyze/compare against an inference-sim-produced report; it is SKIPPED by default (suite stays hermetic/green); a non-gating CI job runs it on dispatch; the default test count/coverage is unaffected.
HERMETIC TEST: the default run SKIPS the integration test cleanly (assert it is collected + skipped without the flag); the harness/wiring (parsing a sim-shaped report fixture through analyze/compare) is unit-tested hermetically so the integration logic is covered even when the sim is absent.
THIN-CODE NOTE: tests + CI only; no app behavior change. NOTE: if llm-d-inference-sim cannot be located in this environment, STILL deliver the opt-in harness + a sim-shaped report fixture test + the CI job, and document the binary requirement — do NOT hang the suite trying to reach a real server.`,
}

// ----------------------------------------------------------------------------
// Prompts
// ----------------------------------------------------------------------------
const PREP_PROMPT = `Prepare a FRESH integration worktree for Roadmap v3 (proposal-completion) so phase branches merge cleanly. Use Bash. main is NEVER touched. Do EXACTLY:

1. Sanity: note (do NOT switch) the main branch with  git -C ${MONO} rev-parse --abbrev-ref HEAD . Run  git -C ${MONO} status --porcelain  (if unrelated uncommitted work exists, leave it and mention it in notes).

2. Choose BASE and create (or reuse) the integration branch ${INTEG} + worktree ${HOME}:
   - If branch ${BASE} exists ( git -C ${MONO} rev-parse --verify ${BASE} ), BASE=${BASE}; else BASE=main. Report which in notes (field 'base').
   - If ${HOME} already exists as a worktree (git -C ${MONO} worktree list shows it): put it on ${INTEG} (git -C ${HOME} checkout ${INTEG}) and report reuse.
   - Else if branch ${INTEG} already exists:  git -C ${MONO} worktree add ${HOME} ${INTEG}
   - Else:  git -C ${MONO} worktree add -b ${INTEG} ${HOME} <BASE>
   Verify:  git -C ${HOME} rev-parse --abbrev-ref HEAD  ==  ${INTEG}

3. Make the integration worktree runnable + record the baseline:
   - Copy env if missing:  cp -n ${MONO}/${PROJ}/.env ${HOME}/${PROJ}/.env 2>/dev/null || true
   - Confirm the venv:  ${VENV} --version
   - Confirm the worktree's app wins on the path:  cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"  (MUST print a path under ${PDIR})
   - Run the suite to record the green baseline:  ${TESTCMD(PDIR)}   (record pass/skip counts)
   - Note whether ruff/mypy are configured (grep -q 'tool.ruff' ${PDIR}/pyproject.toml ; ${VENV} -m ruff --version) so integrators can run the lint gate when present.

4. Open the Roadmap v3 section in ${PDIR}/ROADMAP.md if absent: append a short header '## Roadmap v3 — proposal-completion features (Phases 19-26)' with one intro line (integration branch ${INTEG} off the chosen base, never main). Commit only if changed:
   git -C ${HOME} add ${PROJ}/ROADMAP.md && git -C ${HOME} commit -m "docs: open Roadmap v3 section" -m "${TRAILER}"

5. Detect resume state: read ${PDIR}/ROADMAP.md and report which of phases 19..26 are ALREADY marked DONE.

Return {done:[...ids already DONE...], base:"feature/roadmap-v2 or main", notes:"baseline pass/skip counts + ruff/mypy availability + anything notable"}.`

function implPrompt(id) {
  return `You implement EXACTLY ONE roadmap phase — Phase ${id} "${TITLE[id]}" (${SLUG[id]}) — in your OWN isolated git worktree, then commit on its branch. Other agents may be implementing other phases in parallel in DIFFERENT worktrees — stay strictly inside yours. Favor correctness over speed. main is NEVER touched.

== Repo facts ==
- Main checkout (shares .git; has the POPULATED read-only sibling repos llm-d/ and llm-d-benchmark/): ${MONO}
- Integration branch (never merge anywhere yourself): ${INTEG}
- Project dir name inside any worktree: ${PROJ}
- Conventions/law: read ${PDIR}/CLAUDE.md and ${PDIR}/PROGRESS.md (thin code/thick agent; allowlist-as-data; hermetic tests; secrets; determinism; the current test baseline). Skim ${PDIR}/PROPOSAL_ROADMAP.md for where your phase fits.

== The phase spec (AUTHORITATIVE; it is NOT yet in ROADMAP.md — implement from here) ==
${SPEC[id]}

== Step 1: create YOUR worktree off the integration branch ==
If ${wt(id)} already exists from a prior run:  git -C ${MONO} worktree remove --force ${wt(id)} ; git -C ${MONO} branch -D ${br(id)}  (ignore errors). Then:
  git -C ${MONO} worktree add -b ${br(id)} ${wt(id)} ${INTEG}
Verify:  git -C ${wt(id)} rev-parse --abbrev-ref HEAD  ==  ${br(id)} . Do ALL edits inside ${wt(id)}/${PROJ} ONLY.

== Step 2: implement per the spec. Hard rules ==
- THIN CODE, THICK AGENT: no decision logic in Python if/elif; mechanism in Python, judgment in knowledge/*.md|yaml.
- SECURITY: the allowlist is DATA (security/allowlist.yaml) — widen via YAML, never per-command Python. Commands are argv lists, shell=False. Read-only auto-runs; mutating needs approval.
- Read repo truth at runtime; NEVER edit the sibling repos llm-d/ or llm-d-benchmark/.
- NO new REQUIRED runtime dependency. (Phase 26 may add an OPT-IN/skipped integration dep, never a required one.)
- Tests: add/extend pytest under tests/ that MEANINGFULLY cover the feature (no vacuous asserts, no skip-to-pass, no xfail). HERMETIC ONLY — no live cluster, no GPU, no network, no long real runs; use the existing fakes (FakeKubeClient, CaptureRunner, the tests/test_ws.py TestClient harness, fake clocks). (Phase 26's opt-in integration test is the sole, explicitly-skipped exception.)
- DO NOT edit ROADMAP.md or PROGRESS.md (the integrator owns those). You MAY add knowledge/*.md|yaml and docs.
- If ruff/mypy are configured in pyproject (from the v2 quality gates), keep your changes ruff- and mypy-clean.

== Step 3: run the suite from YOUR worktree ==
First confirm your app wins on the path:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${wt(id)})
Then:
  ${TESTCMD(wt(id) + '/' + PROJ)}
ZERO failures, and your NEW tests must pass. Iterate until green. (timeout 600: exit 124 = a hung test reached a real resource — make it hermetic, do NOT skip.)

== Step 4: commit (do NOT push, do NOT merge) ==
git -C ${wt(id)} add -A
git -C ${wt(id)} commit -m "<clear scoped Phase ${id} message>" -m "${TRAILER}"

Return the structured result. ok=true ONLY if the suite is green (failCount=0) AND the spec's ACCEPTANCE is genuinely met. summary <= 6 lines. branch=${br(id)}, worktree=${wt(id)}.`
}

function fixPrompt(id, feedback) {
  return `Phase ${id} ("${SLUG[id]}" — ${TITLE[id]}) FAILED review. Fix it IN PLACE in the existing worktree ${wt(id)} on branch ${br(id)} — do not create a new worktree.

Reviewer feedback (address EVERY blocking item):
${feedback}

Re-read the spec if needed:
${SPEC[id]}

Keep the hard rules (thin code/thick agent; allowlist-as-data; hermetic tests only; no new required runtime dep; do NOT edit ROADMAP.md/PROGRESS.md; keep ruff/mypy clean if configured). Then re-run, asserting your app wins on the path:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (under ${wt(id)})
  ${TESTCMD(wt(id) + '/' + PROJ)}
It must be green (failCount=0). Amend or add a commit on ${br(id)} ending with:
${TRAILER}
Return the structured result with ok=true only if green and the blocking items are resolved.`
}

function verifyPrompt(id, lens) {
  const head = `Adversarially review Phase ${id} ("${SLUG[id]}" — ${TITLE[id]}) implemented on branch ${br(id)} in worktree ${wt(id)}. Be skeptical; default to acceptable=false if unsure. You are ONE of three independent lenses. Read-only review (re-running pytest is allowed). Inspect the diff:  git -C ${wt(id)} diff ${INTEG}...HEAD

The AUTHORITATIVE phase spec (NOT yet in ROADMAP.md):
${SPEC[id]}

`
  const lenses = {
    'acceptance':
      head + `LENS = ACCEPTANCE. Does the implementation ACTUALLY deliver the spec's BUILD + ACCEPTANCE (the real feature, not a stub)? Coherent and complete? List anything missing/incorrect as blocking.`,
    'tests-real':
      head + `LENS = TESTS-ARE-REAL. RE-RUN the suite yourself. First assert app origin:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (must be under ${wt(id)})
  ${TESTCMD(wt(id) + '/' + PROJ)}
Confirm GREEN (0 failures, NOT a 124 timeout) and the NEW tests genuinely exercise the feature (meaningful assertions, not skipped/vacuous/tautological, matching the spec's HERMETIC TEST). For Phase 26 specifically: confirm the integration test is SKIPPED by default AND the sim-shaped fixture/harness is unit-tested hermetically. If red, vacuous, or fake coverage, list it as blocking with the weak/failing test names.`,
    'philosophy-security':
      head + `LENS = PHILOSOPHY+SECURITY. Enforce: (a) thin code/thick agent — NO decision logic in Python if/elif; judgment in knowledge/*.md|yaml; (b) the allowlist stays DATA in security/allowlist.yaml, widened via YAML not Python, commands argv-only shell=False; (c) NO new REQUIRED runtime dependency (Phase 26 opt-in/skipped dep only); (d) NO edits to sibling repos llm-d/ or llm-d-benchmark/; (e) tests hermetic by default (no live cluster/GPU/network/long runs; Phase 26's opt-in test must skip cleanly); (f) did NOT edit ROADMAP.md or PROGRESS.md. List every violation as blocking.`,
  }
  return lenses[lens]
}

function integratePrompt(id) {
  const slug = SLUG[id]
  return `You are the SERIAL integrator (only one runs at a time). Merge the verified Phase ${id} ("${slug}") branch into ${INTEG}, gate on the FULL suite, write the state docs, and clean up. Use Bash. main is NEVER touched.

== Context ==
- Integration worktree (on ${INTEG}; runnable): ${HOME}   (project dir ${PDIR})
- Phase branch to merge: ${br(id)}   (implemented in worktree ${wt(id)})
- Shared venv python: ${VENV}

== Steps ==
1. Ensure the integration worktree is on ${INTEG} and clean:
   git -C ${HOME} rev-parse --abbrev-ref HEAD   (if not ${INTEG}:  git -C ${HOME} checkout ${INTEG})
   git -C ${HOME} status --porcelain   (must be clean; commit/stash stray WIP sensibly first)

2. Merge (no fast-forward):
   git -C ${HOME} merge --no-ff ${br(id)} -m "Merge Phase ${id} (${slug}) into ${INTEG}"
   CONFLICT POLICY:
   - ADDITIVE-REGISTRATION files (app/tools/registry.py, app/tools/schemas.py, app/agent/prompt.py, security/allowlist.yaml, knowledge/* index/loader): KEEP BOTH SIDES' entries — never drop an existing tool/field/policy/knowledge line.
   - STRUCTURAL-WIRING files (app/orchestrator/controller.py, app/orchestrator/job.py, app/orchestrator/kube.py, app/tools/orchestrate.py, app/validation/report.py, app/validation/analysis.py, app/main.py, app/config.py): COMPOSITION-RECONCILE — merge both sides' logic into ONE coherent function/manifest/run-loop that runs BOTH behaviors. Read both versions and write the deliberate union; do NOT blind-concatenate duplicate blocks. (Notably P21 + P22 both edit the run loop; reconcile streaming + checkpoint together.)
   Then:  git -C ${HOME} add <files> && git -C ${HOME} commit --no-edit

3. AUTHORITATIVE full suite — from the integration worktree, TIMEOUT-bounded, worktree app on the path:
   cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${PDIR})
   ${TESTCMD(PDIR)}
   Exit 124 = the suite HUNG — make the offending test hermetic; do NOT skip/xfail to hide it (set timedOut=true, merged=false if you cannot). Require 0 failures and a pass count >= the prior baseline in PROGRESS.md. If red, FIX the real integration problem (resolve the merge composition) — do NOT delete/weaken tests.
3b. LINT/TYPE GATE (only if configured from the v2 quality gates): if pyproject.toml contains a [tool.ruff]/[tool.mypy] section AND ${VENV} -m ruff --version works, run  cd ${PDIR} && ${VENV} -m ruff check .  and  ${VENV} -m mypy app  — both must pass. If ruff/mypy are not installed/configured, SKIP this step (do not add them here).

4. Update state docs ON ${INTEG} (you are the ONLY writer — no conflicts). Append to ${PDIR}/ROADMAP.md:
   "## Phase ${id} — ${TITLE[id]} — DONE" with a 2-4 line result (what shipped) and the new pass/skip counts. Also tick the matching row in ${PDIR}/PROPOSAL_ROADMAP.md if straightforward.
   Append a short Phase ${id} entry to ${PDIR}/PROGRESS.md (what shipped + counts).
   git -C ${HOME} add ${PROJ}/ROADMAP.md ${PROJ}/PROGRESS.md ${PROJ}/PROPOSAL_ROADMAP.md && git -C ${HOME} commit -m "docs: mark Phase ${id} (${slug}) done" -m "${TRAILER}"

5. Cleanup:
   git -C ${MONO} worktree remove --force ${wt(id)}
   git -C ${MONO} branch -D ${br(id)}
   git -C ${MONO} worktree prune

Return {phase:${id}, merged, fullSuitePassed, passCount, skipCount, failCount, timedOut, notes}. Set merged=false (and leave the branch+worktree intact for review) if you could not land it cleanly.`
}

function finalizePrompt(integratedList, skippedList) {
  return `You are the FINAL integrator. The verified Roadmap v3 phases have been merged into ${INTEG}. Your job: merge ${INTEG} into MAIN and commit it — but ONLY behind a hard green + ruff-clean + mypy-clean gate. If ANY gate fails, ABORT the merge and leave main EXACTLY as it was. Use Bash. Do NOT push to any remote.

== Context ==
- Main checkout (currently on main): ${MONO}   (project dir ${MONO}/${PROJ})
- Integration branch to land: ${INTEG}   (integration worktree ${HOME}, project dir ${PDIR})
- Shared venv python: ${VENV}
- Integrated phases: [${integratedList.join(',')}]   Skipped/left-for-review: [${skippedList.join(',')}]

== Steps ==
1. Confirm ${INTEG} is GREEN before merging. From the integration worktree:
   cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${PDIR})
   ${TESTCMD(PDIR)}
   The per-test timeout backstop is baked into pyproject (a hang fails fast, exit 124-ish). Require 0 failures. If red, STOP: return merged=false and do NOT touch main.

2. Put the main checkout on main and ensure it is clean:
   git -C ${MONO} checkout main
   git -C ${MONO} status --porcelain   — IGNORE untracked files (PROPOSAL_ROADMAP.md, .env.bak.*, .claude/* artifacts). There must be no uncommitted TRACKED changes; if there are, STOP and return merged=false with a note.

3. Merge WITHOUT committing yet (so the gate runs before the commit):
   git -C ${MONO} merge --no-ff --no-commit ${INTEG}
   If CONFLICTS:
   - ADDITIVE-REGISTRATION files (app/tools/registry.py, app/tools/schemas.py, app/agent/prompt.py, security/allowlist.yaml, knowledge index/loader): KEEP BOTH SIDES' entries — never drop an existing tool/field/policy/knowledge line.
   - STRUCTURAL-WIRING files (app/main.py, app/config.py, app/agent/loop.py|channel.py|session.py, app/security/runner.py, app/tools/context.py, app/orchestrator/*.py, app/validation/report.py|analysis.py): COMPOSITION-RECONCILE — read both versions and write the deliberate union into ONE coherent function that runs both behaviors; never blind-concatenate duplicate blocks. (main already carries Roadmap v2 + simulate-mode; preserve all of it.)
   Then  git -C ${MONO} add <resolved files> .

4. HARD GATE on the merged main tree (from the main checkout's project dir):
   cd ${MONO}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${MONO}/${PROJ})
   ${TESTCMD(MONO + '/' + PROJ)}     -> require 0 failures (per-test timeout auto-active; do NOT weaken/skip tests to pass)
   cd ${MONO}/${PROJ} && ${VENV} -m ruff check .   -> must be clean (run ${VENV} -m ruff check --fix . only for trivial import-sort/format, then re-check)
   cd ${MONO}/${PROJ} && ${VENV} -m mypy app       -> must be clean
   If ANY gate fails and you cannot fix it cleanly:  git -C ${MONO} merge --abort  (restores main exactly), return merged=false with the failure in notes. NEVER commit a red/dirty merge to main.

5. Commit the merge to main (only when all gates pass):
   git -C ${MONO} commit -m "Merge ${INTEG} (Roadmap v3: proposal-completion features) into main" -m "<2-4 line summary: which phases [${integratedList.join(',')}] shipped + the suite pass/skip counts>" -m "${TRAILER}"
   Capture the commit hash:  git -C ${MONO} rev-parse --short HEAD . Do NOT push.

6. Cleanup (only after the green commit): remove the integration worktree (keep the ${INTEG} branch for reference):
   git -C ${MONO} worktree remove --force ${HOME}
   git -C ${MONO} worktree prune

Return {merged, suitePassed, lintPassed, typePassed, mergeCommit, passCount, skipCount, failCount, notes}. merged=true ONLY if the merge is COMMITTED to main with a green + ruff-clean + mypy-clean suite. On any gate failure, merged=false and main is left untouched (merge aborted).`
}

// ----------------------------------------------------------------------------
// Per-phase pipeline: implement -> 3-lens parallel verify -> (bounded fix+reverify)
// -> readiness. Integration is done separately and SERIALLY by the caller.
// ----------------------------------------------------------------------------
async function preparePhase(id, waveTitle) {
  const Aj = (prompt, label, schema) => agent(prompt, { label, phase: waveTitle, agentType: 'general-purpose', schema })
  try {
    let impl = await Aj(implPrompt(id), 'impl:p' + id, IMPL_SCHEMA)
    if (!impl) return { phase: id, ready: false, reason: 'impl-null' }

    const lenses = ['acceptance', 'tests-real', 'philosophy-security']
    let verdicts = (await parallel(lenses.map(L => () =>
      Aj(verifyPrompt(id, L), 'verify:' + L + ':p' + id, VERDICT_SCHEMA)))).filter(Boolean)
    let blocking = verdicts.filter(v => !v.acceptable || (v.blocking && v.blocking.length))

    if (!impl.ok || blocking.length) {
      const feedback = JSON.stringify({ implOk: impl.ok, blocker: impl.blocker, verdicts }).slice(0, 6000)
      const fixed = await Aj(fixPrompt(id, feedback), 'fix:p' + id, IMPL_SCHEMA)
      if (fixed) impl = fixed
      verdicts = (await parallel(lenses.map(L => () =>
        Aj(verifyPrompt(id, L), 'reverify:' + L + ':p' + id, VERDICT_SCHEMA)))).filter(Boolean)
      blocking = verdicts.filter(v => !v.acceptable || (v.blocking && v.blocking.length))
    }

    const ready = !!(impl && impl.ok && !blocking.length)
    return { phase: id, slug: SLUG[id], ready, impl, blocking: blocking.map(v => ({ lens: v.lens, blocking: v.blocking })) }
  } catch (e) {
    return { phase: id, ready: false, reason: 'exception: ' + (e && e.message ? e.message : String(e)) }
  }
}

// ----------------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------------
phase('Prep')
const status = await agent(PREP_PROMPT, { label: 'prep', phase: 'Prep', agentType: 'general-purpose', schema: STATUS_SCHEMA })
const done = new Set((status && status.done) || [])
log('Prep complete. Base: ' + ((status && status.base) || '?') + '. Already DONE: [' + [...done].join(',') + ']' + (status && status.notes ? ' — ' + status.notes : ''))

const integrated = []
const skipped = []

for (let w = 0; w < WAVES.length; w++) {
  const waveTitle = 'Wave ' + (w + 1)
  phase(waveTitle)
  const ids = WAVES[w].filter(id => !done.has(id))
  if (!ids.length) { log(waveTitle + ': all phases already done — skipping'); continue }
  log(waveTitle + ': implementing phases [' + ids.join(',') + '] in parallel isolated worktrees')

  const prepared = (await parallel(ids.map(id => () => preparePhase(id, waveTitle)))).filter(Boolean)

  for (const p of prepared.sort((a, b) => a.phase - b.phase)) {
    if (!p.ready) {
      skipped.push({ phase: p.phase, reason: p.reason || 'failed-verification', blocking: p.blocking })
      log('SKIP Phase ' + p.phase + ' — not ready (' + (p.reason || 'verification') + '); branch+worktree left for review')
      continue
    }
    let r = null
    try {
      r = await agent(integratePrompt(p.phase), { label: 'integrate:p' + p.phase, phase: waveTitle, agentType: 'general-purpose', schema: INTEG_SCHEMA })
    } catch (e) {
      r = null
    }
    if (r && r.merged && r.fullSuitePassed && r.failCount === 0) {
      integrated.push({ phase: p.phase, pass: r.passCount, skip: r.skipCount })
      log('INTEGRATED Phase ' + p.phase + ' — suite ' + r.passCount + ' passed / ' + r.skipCount + ' skipped')
    } else {
      skipped.push({ phase: p.phase, reason: 'integration-failed', notes: r && r.notes })
      log('SKIP Phase ' + p.phase + ' — integration did not land cleanly; left for review')
    }
  }
}

// ----------------------------------------------------------------------------
// Finalize: a subagent merges feature/roadmap-v3 into main and commits it,
// gated on a green + ruff-clean + mypy-clean suite (aborts -> main untouched).
// ----------------------------------------------------------------------------
let finalize = null
if (integrated.length) {
  phase('Finalize')
  log('Finalize: merging ' + INTEG + ' into main (gated on green + ruff + mypy)')
  try {
    finalize = await agent(
      finalizePrompt(integrated.map(i => i.phase), skipped.map(s => s.phase)),
      { label: 'finalize:merge-main', phase: 'Finalize', agentType: 'general-purpose', schema: FINALIZE_SCHEMA },
    )
  } catch (e) {
    finalize = null
  }
  if (finalize && finalize.merged) {
    log('MERGED ' + INTEG + ' into main @ ' + (finalize.mergeCommit || '?') + ' — suite ' + finalize.passCount + ' passed / ' + finalize.skipCount + ' skipped')
  } else {
    log('FINALIZE did NOT land on main (gate failed or aborted) — main left untouched; ' + INTEG + ' retained. Notes: ' + (finalize && finalize.notes))
  }
} else {
  log('Finalize skipped: nothing integrated into ' + INTEG + '.')
}

const summary = {
  integrated: integrated.map(i => i.phase),
  skipped,
  finalSuite: integrated.length ? integrated[integrated.length - 1] : null,
  mergedToMain: !!(finalize && finalize.merged),
  mergeCommit: finalize && finalize.mergeCommit,
  finalize,
  note: (finalize && finalize.merged)
    ? INTEG + ' was merged into main @ ' + (finalize.mergeCommit || '?') + ' (not pushed). Skipped phases retain their branch+worktree for review.'
    : 'main was left untouched (' + (integrated.length ? 'finalize gate failed/aborted' : 'nothing integrated') + '); work is on ' + INTEG + '. Skipped phases retain their branch+worktree.',
}
log('Roadmap v3 autopilot finished. Integrated: [' + summary.integrated.join(',') + ']  Skipped: [' + skipped.map(s => s.phase).join(',') + ']  Merged to main: ' + summary.mergedToMain)
return summary
