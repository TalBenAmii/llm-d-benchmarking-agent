export const meta = {
  name: 'roadmap-autopilot',
  description: 'Autonomously implement the remaining roadmap phases (4-10) as fresh-context agents in isolated git worktrees, adversarially verify each, and serially integrate into feature/roadmap (NEVER main). Keeps the main agent context near-zero: every phase is a separate agent() context; the orchestrator holds only short summaries.',
  whenToUse: 'Continue the llm-d-benchmarking-agent phased roadmap autonomously without filling the main conversation context. Resumable via resumeFromRunId.',
  phases: [
    { title: 'Prep', detail: 'put integration worktree on feature/roadmap, clear stale empty phase branches, read DONE status from ROADMAP.md' },
    { title: 'Wave 1', detail: 'phases 4 (analyzer), 6 (capacity), 7 (observability) — parallel implement+verify in isolated worktrees, then serial integrate' },
    { title: 'Wave 2', detail: 'phases 5 (storage/trends), 8 (packaging), 10 (multi-harness) — parallel implement+verify, then serial integrate' },
    { title: 'Wave 3', detail: 'phase 9 (docs) — implement+verify+integrate' },
  ],
}

// ----------------------------------------------------------------------------
// Constants (verified against on-disk state)
// ----------------------------------------------------------------------------
const MONO   = '/home/tal/kind-quickstart-guide'                 // main checkout: shares .git, has POPULATED read-only sibling repos
const HOME   = '/home/tal/kind-quickstart-guide-roadmap'         // integration worktree: has .venv + .env
const PROJ   = 'llm-d-benchmarking-agent-project'
const PDIR   = HOME + '/' + PROJ                                 // integration project dir
const INTEG  = 'feature/roadmap'                                 // integration branch (NEVER main)
const VENV   = HOME + '/.venv/bin/python'

// phase id -> slug; WAVES encode dependency order (later waves branch off the
// integration branch AFTER earlier waves merge). Phases within a wave have no
// code-dependency on each other, so they implement in parallel safely.
const SLUG  = { 4:'analyzer', 5:'storage', 6:'capacity', 7:'observability', 8:'packaging', 9:'docs', 10:'multiharness' }
const WAVES = [ [4,6,7], [5,8,10], [9] ]

const wt = (id) => '/home/tal/kqg-p' + id + '-' + SLUG[id]
const br = (id) => 'feature/roadmap-p' + id + '-' + SLUG[id]

// ----------------------------------------------------------------------------
// Schemas (structured agent returns -> no parsing, validated at the tool layer)
// ----------------------------------------------------------------------------
const STATUS_SCHEMA = { type:'object', additionalProperties:false,
  properties:{ done:{type:'array', items:{type:'integer'}}, notes:{type:'string'} }, required:['done'] }

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
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'}, notes:{type:'string'} },
  required:['phase','merged','fullSuitePassed','passCount','failCount'] }

// ----------------------------------------------------------------------------
// The per-worktree test command. Run from a phase worktree's project dir, it
// uses the shared venv's third-party deps but YOUR worktree's `app` code
// (PYTHONPATH + `python -m` put cwd first), and the populated sibling repos in
// the main checkout (REPOS_DIR) so catalog/report tests don't fail empty.
// ----------------------------------------------------------------------------
// `timeout 420` so a hung test (e.g. one reaching a live cluster under concurrency)
// can NEVER wedge the pipeline — it returns 124 and is treated as a failure to fix.
const TESTCMD = (worktree) =>
  'cd ' + worktree + '/' + PROJ +
  ' && REPOS_DIR=' + MONO + ' PYTHONPATH="$PWD" timeout 420 ' + VENV + ' -m pytest tests/ -q'

const TRAILER = 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>'

// ----------------------------------------------------------------------------
// Prompts
// ----------------------------------------------------------------------------
const PREP_PROMPT = `Prepare the integration worktree so phase branches can be merged cleanly. Use Bash. Do EXACTLY:

1. Confirm the integration worktree is clean and put it on the integration branch:
   - git -C ${HOME} status --porcelain   (if there is uncommitted work, DO NOT destroy it — report it in notes and still continue)
   - git -C ${HOME} checkout ${INTEG}

2. Delete STALE phase branches (parity with ${INTEG}, i.e. NO unique commits) so phases can recreate them. For each branch matching 'feature/roadmap-p*':
   - if  git -C ${MONO} log ${INTEG}..<branch> --oneline  prints NOTHING, it is stale:
       * if a worktree uses it:  git -C ${MONO} worktree remove --force <that-worktree-path>
       * then:  git -C ${MONO} branch -D <branch>
   - if it HAS unique commits, LEAVE it untouched and mention it in notes.
   - also run  git -C ${MONO} worktree prune .

3. Read ${PDIR}/ROADMAP.md and report which phase ids among 4,5,6,7,8,9,10 are ALREADY marked DONE/complete.

Return {done:[...ids already done...], notes:"..."}.`

function implPrompt(id) {
  const slug = SLUG[id]
  return `You implement EXACTLY ONE roadmap phase (Phase ${id} "${slug}") in your OWN isolated git worktree, then commit on its branch. Other agents are implementing other phases in parallel in DIFFERENT worktrees — stay strictly inside yours so there is zero collision. Favor correctness over speed.

== Repo facts ==
- Main checkout (shares .git; has the POPULATED read-only sibling repos llm-d/ and llm-d-benchmark/): ${MONO}
- Integration branch (NEVER touch main; never merge anywhere yourself): ${INTEG}
- App project dir name (inside any worktree): ${PROJ}
- Authoritative spec + conventions: ${PDIR}/ROADMAP.md (Phase ${id} section), ${PDIR}/PROGRESS.md, ${PDIR}/CLAUDE.md

== Step 1: read the spec (absorb; do not paste back) ==
Read the Phase ${id} section of ${PDIR}/ROADMAP.md, plus PROGRESS.md (conventions, gotchas, current test baseline) and CLAUDE.md (the non-negotiable rules).

== Step 2: create YOUR worktree off the integration branch ==
If ${wt(id)} already exists from a prior run: git -C ${MONO} worktree remove --force ${wt(id)} ; and git -C ${MONO} branch -D ${br(id)} (ignore errors).
Then: git -C ${MONO} worktree add -b ${br(id)} ${wt(id)} ${INTEG}
Verify: git -C ${wt(id)} rev-parse --abbrev-ref HEAD  ==  ${br(id)}
Do ALL edits inside ${wt(id)}/${PROJ} ONLY.

== Step 3: implement Phase ${id} per the ROADMAP spec. Hard rules ==
- THIN CODE, THICK AGENT: no decision logic in Python if/elif. Mechanism in Python; judgment lives in knowledge/*.md|*.yaml.
- SECURITY: the allowlist is DATA (security/allowlist.yaml) — widen it via YAML, never via per-command Python. Commands are argv lists with shell=False. Read-only auto-runs; mutating needs approval.
- Read repo truth at runtime; never vendor copies; NEVER edit the sibling repos llm-d/ or llm-d-benchmark/.
- Tests: add/extend pytest under tests/ that MEANINGFULLY cover the feature (no vacuous asserts, no xfail-to-pass). HERMETIC ONLY — no live cluster, no GPU, no long real runs; use fakes / the existing CaptureRunner / fake-kube-client patterns already in the repo.
- DO NOT edit ROADMAP.md or PROGRESS.md (the integrator updates those centrally so parallel phases never conflict on them). You MAY add phase-specific knowledge/*.md|yaml and docs.

== Step 4: run the suite from YOUR worktree ==
${TESTCMD(wt(id))}
First confirm  python -c "import app, os; print(app.__file__)"  resolves under ${wt(id)} (not the integration worktree). There must be ZERO failures and your NEW tests must pass. Iterate until green.

== Step 5: commit (do NOT push, do NOT merge) ==
git -C ${wt(id)} add -A
git -C ${wt(id)} commit -m "<clear scoped Phase ${id} message>" -m "${TRAILER}"

Return the structured result. Set ok=true ONLY if the suite is green (failCount=0) AND the Phase ${id} acceptance is genuinely met. If blocked, ok=false with a one-paragraph blocker. summary <= 6 lines. branch=${br(id)}, worktree=${wt(id)}.`
}

function fixPrompt(id, feedback) {
  return `Phase ${id} ("${SLUG[id]}") FAILED review. Fix it IN PLACE in the existing worktree ${wt(id)} on branch ${br(id)} — do not create a new worktree.

Reviewer feedback (address every blocking item):
${feedback}

Re-read the Phase ${id} section of ${PDIR}/ROADMAP.md if needed. Keep the same hard rules (thin code/thick agent; allowlist-as-data; hermetic tests only; do NOT edit ROADMAP.md/PROGRESS.md).
Then re-run the suite:
${TESTCMD(wt(id))}
It must be green (failCount=0). Amend or add a commit on ${br(id)} ending with the trailer:
${TRAILER}
Return the structured result with ok=true only if green and the blocking items are resolved.`
}

function verifyPrompt(id, lens) {
  const head = `Adversarially review Phase ${id} ("${SLUG[id]}") implemented on branch ${br(id)} in worktree ${wt(id)}. Be skeptical; default to acceptable=false if unsure. You are ONE of three independent lenses. Read-only review (running pytest to confirm is allowed). The phase spec is the Phase ${id} section of ${PDIR}/ROADMAP.md. Inspect the diff with:  git -C ${wt(id)} diff ${INTEG}...HEAD\n\n`
  const lenses = {
    'acceptance':
      head + `LENS = ACCEPTANCE. Does the implementation ACTUALLY deliver Phase ${id}'s ROADMAP acceptance criteria (the real feature, not a stub)? Are the edits coherent and complete? List any missing/incorrect behavior as blocking.`,
    'tests-real':
      head + `LENS = TESTS-ARE-REAL. RE-RUN the suite yourself:\n${TESTCMD(wt(id))}\nConfirm it is GREEN (0 failures) and the NEW tests genuinely exercise the feature (meaningful assertions, not skipped, not vacuous, not tautological). If red, vacuous, or coverage is fake, list it as blocking with the failing/weak test names.`,
    'philosophy-security':
      head + `LENS = PHILOSOPHY+SECURITY. Enforce the project rules: (a) thin code / thick agent — NO decision logic in Python if/elif; judgment must live in knowledge/*.md|yaml; (b) the allowlist stays DATA in security/allowlist.yaml, widened via YAML not Python, commands argv-only shell=False; (c) no edits to sibling repos llm-d/ or llm-d-benchmark/; (d) tests are hermetic (no live cluster/GPU/long runs); (e) the phase did NOT edit ROADMAP.md or PROGRESS.md. List every violation as blocking.`,
  }
  return lenses[lens]
}

function integratePrompt(id) {
  const slug = SLUG[id]
  return `You are the SERIAL integrator (only one runs at a time). Merge the verified Phase ${id} ("${slug}") branch into the integration branch, gate on the FULL suite, update state docs, and clean up. Use Bash.

== Context ==
- Integration worktree (on ${INTEG}; has the real .venv + .env): ${HOME}
- Project dir: ${PDIR}
- Phase branch to merge: ${br(id)}  (implemented in worktree ${wt(id)})

== Steps ==
1. Ensure integration worktree is on the integration branch:
   git -C ${HOME} rev-parse --abbrev-ref HEAD   (if not ${INTEG}:  git -C ${HOME} checkout ${INTEG})
   git -C ${HOME} status --porcelain   (must be clean; if not, commit/stash leftover WIP sensibly first)

2. Merge (no fast-forward):
   git -C ${HOME} merge --no-ff ${br(id)} -m "Merge Phase ${id} (${slug}) into ${INTEG}"
   If CONFLICTS occur, they will be in shared registration files (app/tools/registry.py, app/tools/schemas.py, app/agent/prompt.py, security/allowlist.yaml). Resolve by KEEPING BOTH SIDES' additions — each phase adds distinct tools/fields/entries; never drop an existing entry. Then  git -C ${HOME} add <files>  and  git -C ${HOME} commit --no-edit.

3. AUTHORITATIVE full suite, TIMEOUT-BOUNDED (this worktree has the real venv + .env):
   cd ${PDIR} && timeout 420 ${VENV} -m pytest tests/ -q
   Check the exit code: 124 means the suite HUNG (a test reached the live cluster/dockerd or bound a port) — make that test hermetic (fake-kube/CaptureRunner), do NOT skip/xfail to hide it. Must be 0 failures (not a 124 timeout) and >= the prior baseline pass count in PROGRESS.md. If red, FIX the real integration problem (resolve merge mistakes / wiring) — do NOT delete or weaken tests. Do not proceed until green.

4. Update state docs ON ${INTEG} (you are the ONLY writer of these, so no conflicts):
   - ROADMAP.md: mark Phase ${id} as DONE with a one-line result.
   - PROGRESS.md: append a short Phase ${id} entry (what shipped + the new pass/skip counts).
   git -C ${HOME} add ${PROJ}/ROADMAP.md ${PROJ}/PROGRESS.md
   git -C ${HOME} commit -m "docs: mark Phase ${id} (${slug}) done" -m "${TRAILER}"

5. Cleanup:
   git -C ${MONO} worktree remove --force ${wt(id)}
   git -C ${MONO} branch -D ${br(id)}

Return {phase:${id}, merged, fullSuitePassed, passCount, skipCount, failCount, notes}. Set merged=false if you could not land it cleanly (and leave the branch+worktree intact for review).`
}

// ----------------------------------------------------------------------------
// Per-phase pipeline: implement -> 3-lens parallel verify -> (bounded retry) ->
// return readiness. Integration is done separately and SERIALLY by the caller.
// ----------------------------------------------------------------------------
async function preparePhase(id, waveTitle) {
  const A = (prompt, label) => agent(prompt, { label, phase: waveTitle, agentType: 'general-purpose' })
  const Aj = (prompt, label, schema) => agent(prompt, { label, phase: waveTitle, agentType: 'general-purpose', schema })

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
}

// ----------------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------------
phase('Prep')
const status = await agent(PREP_PROMPT, { label: 'prep', phase: 'Prep', agentType: 'general-purpose', schema: STATUS_SCHEMA })
const done = new Set((status && status.done) || [])
log('Prep complete. Already DONE: [' + [...done].join(',') + ']' + (status && status.notes ? ' — ' + status.notes : ''))

const integrated = []
const skipped = []

for (let w = 0; w < WAVES.length; w++) {
  const waveTitle = 'Wave ' + (w + 1)
  phase(waveTitle)
  const ids = WAVES[w].filter(id => !done.has(id))
  if (!ids.length) { log(waveTitle + ': all phases already done — skipping'); continue }
  log(waveTitle + ': implementing phases [' + ids.join(',') + '] in parallel isolated worktrees')

  // PARALLEL implement + verify (each phase in its own worktree -> no collision)
  const prepared = (await parallel(ids.map(id => () => preparePhase(id, waveTitle)))).filter(Boolean)

  // SERIAL integrate (in ascending phase order) -> no merge-time collision on hot files
  for (const p of prepared.sort((a, b) => a.phase - b.phase)) {
    if (!p.ready) {
      skipped.push({ phase: p.phase, reason: p.reason || 'failed-verification', blocking: p.blocking })
      log('SKIP Phase ' + p.phase + ' — failed verification; branch+worktree left intact for human review')
      continue
    }
    const r = await agent(integratePrompt(p.phase), { label: 'integrate:p' + p.phase, phase: waveTitle, agentType: 'general-purpose', schema: INTEG_SCHEMA })
    if (r && r.merged && r.fullSuitePassed && r.failCount === 0) {
      integrated.push({ phase: p.phase, pass: r.passCount, skip: r.skipCount })
      log('INTEGRATED Phase ' + p.phase + ' — suite ' + r.passCount + ' passed / ' + r.skipCount + ' skipped')
    } else {
      skipped.push({ phase: p.phase, reason: 'integration-failed', notes: r && r.notes })
      log('SKIP Phase ' + p.phase + ' — integration did not land cleanly; left for review')
    }
  }
}

const summary = {
  integrated: integrated.map(i => i.phase),
  skipped,
  finalSuite: integrated.length ? integrated[integrated.length - 1] : null,
  note: 'All work is on ' + INTEG + ' only — main was never touched. Skipped phases retain their branch+worktree for review.',
}
log('Autopilot finished. Integrated: [' + summary.integrated.join(',') + ']  Skipped: [' + skipped.map(s => s.phase).join(',') + ']')
return summary
