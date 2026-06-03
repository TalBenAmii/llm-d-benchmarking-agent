export const meta = {
  name: 'finish-roadmap-recovery',
  description: 'Recovery: Wave-2 phases 5/8/10 are already implemented+committed+green on their branches but never integrated (the parallel run wedged on a no-timeout pytest hang under a live kind cluster). Integrate them SERIALLY (no concurrency => no contention) with a TIMEOUT-wrapped full-suite gate, then implement+integrate Wave-3 phase 9. Never touches main.',
  whenToUse: 'Finish the roadmap after the parallel Wave-2 run stalled, salvaging the already-committed phase branches.',
  phases: [
    { title: 'Integrate W2', detail: 'serially merge existing branches feature/roadmap-p{5,8,10}-* into feature/roadmap with a timeout-bounded full-suite gate' },
    { title: 'Wave 3', detail: 'implement phase 9 (docs) off the updated integration branch, then integrate' },
    { title: 'Summary', detail: 'read git+ROADMAP and report the final state' },
  ],
}

const MONO  = '/home/tal/kind-quickstart-guide'
const HOME  = '/home/tal/kind-quickstart-guide-roadmap'
const PROJ  = 'llm-d-benchmarking-agent-project'
const PDIR  = HOME + '/' + PROJ
const INTEG = 'feature/roadmap'
const VENV  = HOME + '/.venv/bin/python'
const TRAILER = 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>'

// id -> slug + existing branch/worktree (created by the stalled run; already committed)
const SLUG = { 5:'storage', 8:'packaging', 10:'multiharness', 9:'docs' }
const br = (id) => 'feature/roadmap-p' + id + '-' + SLUG[id]
const wt = (id) => '/home/tal/kqg-p' + id + '-' + SLUG[id]

// CRITICAL FIX: every pytest run is wrapped in `timeout` so a hang can NEVER wedge the
// pipeline again — a timed-out run returns 124 and is treated as a failure to fix/retry,
// not an infinite idle. Integration runs SERIALLY so there is no concurrent-pytest
// contention (the original wedge cause).
const FULLTEST = 'cd ' + PDIR + ' && REPOS_DIR=' + MONO + ' timeout 420 ' + VENV + ' -m pytest tests/ -q'

const INTEG_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, merged:{type:'boolean'}, fullSuitePassed:{type:'boolean'},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  timedOut:{type:'boolean'}, notes:{type:'string'} },
  required:['phase','merged','fullSuitePassed','passCount','failCount'] }

async function safe(prompt, opts) {
  try { return await agent(prompt, opts) }
  catch (e) { log('agent ' + (opts && opts.label) + ' errored (non-fatal): ' + String(e).slice(0,180)); return null }
}

function integratePrompt(id, alreadyImplemented) {
  const slug = SLUG[id]
  const existsClause = alreadyImplemented
    ? `The branch ${br(id)} ALREADY EXISTS with a committed, green implementation (from a stalled run). Do NOT re-implement it — just integrate it.`
    : `The branch ${br(id)} was just implemented in worktree ${wt(id)}.`
  return `You are a SERIAL integrator (one at a time — no concurrency). Merge a completed phase branch into the integration branch, gate on a TIMEOUT-bounded full suite, update docs, clean up. Use Bash.

${existsClause}

== Context ==
- Integration worktree (on ${INTEG}; has the real venv + .env): ${HOME}
- Project dir: ${PDIR}
- Branch to merge: ${br(id)} ; its worktree: ${wt(id)}

== Steps ==
1. git -C ${HOME} rev-parse --abbrev-ref HEAD  (must be ${INTEG}; else  git -C ${HOME} checkout ${INTEG}). Ensure  git -C ${HOME} status --porcelain  is clean.
2. Confirm the branch has work:  git -C ${MONO} log ${INTEG}..${br(id)} --oneline  . If EMPTY, there is nothing to merge — return merged=false, notes="no commits ahead", and STOP.
3. Merge no-ff:  git -C ${HOME} merge --no-ff ${br(id)} -m "Merge Phase ${id} (${slug}) into ${INTEG}"
   Conflicts will be in shared registration files (app/tools/registry.py, app/tools/schemas.py, app/agent/prompt.py, security/allowlist.yaml). Resolve by KEEPING BOTH SIDES' additions — each phase adds DISTINCT tools/fields/entries; never drop an existing entry. Then  git -C ${HOME} add -A && git -C ${HOME} commit --no-edit.
4. AUTHORITATIVE full suite, TIMEOUT-BOUNDED (this is the fix for the earlier infinite hang):
   ${FULLTEST}
   Read the exit code. If it is 124 the suite HUNG (timed out) — capture which test was last running (re-run once with  ${FULLTEST.replace('-q','-x -q -o faulthandler_timeout=30')}  to dump the stuck stack), then make that test HERMETIC (a hang is almost always a test reaching the LIVE kind cluster / dockerd or binding a port — stub it with the repo's fake-kube / CaptureRunner patterns; do NOT skip/xfail to hide it, and do NOT weaken coverage). If there are ordinary failures, fix the real integration/wiring problem. Repeat until the suite is GREEN (0 failures, not timed out) and passCount >= the prior baseline in PROGRESS.md.
5. Update state docs on ${INTEG} (only writer — no conflicts):
   - ROADMAP.md: mark Phase ${id} DONE with a one-line result.
   - PROGRESS.md: append a short Phase ${id} entry (what shipped + new pass/skip counts).
   git -C ${HOME} add ${PROJ}/ROADMAP.md ${PROJ}/PROGRESS.md && git -C ${HOME} commit -m "docs: mark Phase ${id} (${slug}) done" -m "${TRAILER}"
6. Cleanup:  git -C ${MONO} worktree remove --force ${wt(id)} 2>/dev/null ; git -C ${MONO} branch -D ${br(id)} 2>/dev/null ; git -C ${MONO} worktree prune
Return INTEG_SCHEMA (set timedOut=true if you ever hit a 124, even if later fixed). merged=false if you could not land it.`
}

const IMPL9_PROMPT = `Implement Phase 9 (docs) of the roadmap in your OWN isolated worktree, then commit. Use Bash. Favor correctness; keep it hermetic and fast.

== Repo facts ==
- Main checkout (shares .git; populated read-only sibling repos): ${MONO}
- Integration branch (branch off its CURRENT tip; never merge to main): ${INTEG}
- Spec: read the Phase 9 section of ${PDIR}/ROADMAP.md and PROGRESS.md + CLAUDE.md first.

== Steps ==
1. If ${wt(9)} exists: git -C ${MONO} worktree remove --force ${wt(9)} 2>/dev/null; git -C ${MONO} branch -D ${br(9)} 2>/dev/null. Then:
   git -C ${MONO} worktree add -b ${br(9)} ${wt(9)} ${INTEG}
   Work only inside ${wt(9)}/${PROJ}.
2. Implement Phase 9 per ROADMAP: documentation suite (architecture doc, API/tool reference, deployment guide, user guide) under docs/, and refresh README/CLAUDE/plan status to match the now-complete feature set (phases 4-8 + 10 landed). THIN CODE / THICK AGENT rules still apply; do NOT edit ROADMAP.md or PROGRESS.md (the integrator owns those). Do NOT edit the sibling repos.
3. If you add or touch any tests, keep them hermetic. Run the TIMEOUT-bounded suite from your worktree:
   cd ${wt(9)}/${PROJ} && REPOS_DIR=${MONO} PYTHONPATH="$PWD" timeout 420 ${VENV} -m pytest tests/ -q
   It must be green (0 failures, not a 124 timeout).
4. Commit on ${br(9)} (no push, no merge):
   git -C ${wt(9)} add -A && git -C ${wt(9)} commit -m "Phase 9: documentation suite + upstream-PR readiness" -m "${TRAILER}"
End with a 3-5 line plain-text summary of what you wrote. (No structured output needed.)`

const SUMMARY_PROMPT = `Read the final state and report it concisely (plain text). Use Bash:
- git -C ${HOME} log --oneline -16 ${INTEG}
- grep the Phase status lines in ${PDIR}/ROADMAP.md (which phases are DONE)
- the last pass/skip counts noted in ${PDIR}/PROGRESS.md
- git -C ${MONO} worktree list  and  git -C ${MONO} branch --list 'feature/roadmap-p*'  (confirm phase worktrees/branches were cleaned up; the audit worktree /home/tal/kqg-audit-03 is expected to remain)
Report: which phases (0-10) are now integrated into ${INTEG}, the final test counts, anything NOT done, and any leftover worktrees/branches.`

// ---- run (all SERIAL: no concurrent pytest => no contention hang) ----
phase('Integrate W2')
for (const id of [5, 8, 10]) {
  log('Integrating existing Phase ' + id + ' branch (' + br(id) + ') serially with a timeout-bounded gate')
  const r = await safe(integratePrompt(id, true), { label: 'integrate:p' + id, phase: 'Integrate W2', agentType: 'general-purpose', schema: INTEG_SCHEMA })
  if (r && r.merged && r.fullSuitePassed && r.failCount === 0)
    log('Phase ' + id + ' integrated — ' + r.passCount + ' passed / ' + r.skipCount + ' skipped' + (r.timedOut ? ' (had to fix a hang)' : ''))
  else
    log('Phase ' + id + ' NOT cleanly integrated — ' + (r ? (r.notes || JSON.stringify(r).slice(0,160)) : 'agent returned null') + ' — branch left for review')
}

phase('Wave 3')
log('Implementing Phase 9 (docs) off the updated integration branch')
await safe(IMPL9_PROMPT, { label: 'impl:p9', phase: 'Wave 3', agentType: 'general-purpose' })
const r9 = await safe(integratePrompt(9, false), { label: 'integrate:p9', phase: 'Wave 3', agentType: 'general-purpose', schema: INTEG_SCHEMA })
log('Phase 9 ' + (r9 && r9.merged ? 'integrated' : 'not integrated') + (r9 ? ' (' + (r9.passCount||'?') + ' passed)' : ''))

phase('Summary')
const summary = await safe(SUMMARY_PROMPT, { label: 'summary', phase: 'Summary', agentType: 'general-purpose' })
return { done: 'recovery finished', summary: summary || 'see git log of feature/roadmap' }
