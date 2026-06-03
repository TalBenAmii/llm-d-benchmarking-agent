export const meta = {
  name: 'debloat-docs',
  description: 'Audit every .md in the agent project; correct/compress/remove/relocate each on an isolated worktree branch (review then merge). Never touches main.',
  phases: [
    { title: 'Prep', detail: 'inventory + reference graph + hard-deps + worktree' },
    { title: 'Analyze', detail: 'one agent per .md: decide KEEP/UPDATE/COMPRESS/REMOVE/RELOCATE and apply it' },
    { title: 'Reconcile', detail: 'fix every inbound reference to a removed/relocated/renamed file' },
    { title: 'Skeptic', detail: 'adversarial per-file review: no data loss, runtime-safe, no dangling refs' },
    { title: 'Fix', detail: 'bounded loop to reconcile blocking objections' },
    { title: 'Commit', detail: 'git add -A + commit on the branch + change report' },
    { title: 'Verify', detail: 'dangling-ref sweep, knowledge auto-discovery, file accounting, md lint' },
  ],
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const MONO = '/home/tal/kind-quickstart-guide'
const PROJ = 'llm-d-benchmarking-agent-project'
const SRC = MONO + '/' + PROJ                       // populated main checkout (read current truth)
const HOME = '/home/tal/kqg-debloat-home'           // isolated worktree
const PDIR = HOME + '/' + PROJ                       // where .md files get edited
const SCRATCH = PDIR + '/workspace/debloat_build'    // gitignored handoff (inventory.json, report)
const INV_JSON = SCRATCH + '/inventory.json'
const REPORT = SCRATCH + '/CHANGE_REPORT.md'
const BR = 'chore/debloat-docs'
const BASE = 'main'
const TODAY = '2026-06-03'
const TRAILER = 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'

const base = p => String(p).split('/').pop()

// ---------------------------------------------------------------------------
// Schemas (all additionalProperties:false)
// ---------------------------------------------------------------------------
const FILE_ROW = {
  type: 'object', additionalProperties: false,
  required: ['path', 'category', 'hardDep', 'hardDepReason', 'inboundRefs', 'lines'],
  properties: {
    path: { type: 'string', description: 'repo-relative path under the project, e.g. ROADMAP.md or knowledge/capacity.md' },
    category: { type: 'string', enum: ['RUNTIME_KNOWLEDGE', 'INSTRUCTIONS', 'HUMAN_DOC', 'TRACKING', 'HISTORICAL', 'ENTRY'] },
    hardDep: { type: 'boolean', description: 'true if renaming/removing/relocating this file would break code, tests, build, or registry read_knowledge() / CORE_KNOWLEDGE' },
    hardDepReason: { type: 'string' },
    inboundRefs: { type: 'array', items: { type: 'string' }, description: 'files (with line) that reference/link this file' },
    lines: { type: 'integer' },
  },
}
const INVENTORY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['worktreeReady', 'totalMd', 'files', 'coreKnowledge', 'registryKnowledgeNames', 'groundTruth', 'notes'],
  properties: {
    worktreeReady: { type: 'boolean' },
    totalMd: { type: 'integer' },
    files: { type: 'array', items: FILE_ROW },
    coreKnowledge: { type: 'array', items: { type: 'string' }, description: 'CORE_KNOWLEDGE basenames inlined by app/agent/prompt.py' },
    registryKnowledgeNames: { type: 'array', items: { type: 'string' }, description: "knowledge names referenced via read_knowledge('X') in app/tools/registry.py" },
    groundTruth: {
      type: 'object', additionalProperties: false,
      required: ['toolCount', 'latestPhase', 'recentCommits'],
      properties: {
        toolCount: { type: 'integer', description: 'authoritative tool count from app/tools/registry.py' },
        latestPhase: { type: 'string', description: 'highest completed phase per ROADMAP/PROGRESS' },
        recentCommits: { type: 'array', items: { type: 'string' } },
      },
    },
    notes: { type: 'string' },
  },
}
const REF_FIX = {
  type: 'object', additionalProperties: false,
  required: ['inFile', 'oldRef', 'suggestedNewRef'],
  properties: { inFile: { type: 'string' }, oldRef: { type: 'string' }, suggestedNewRef: { type: 'string' } },
}
const ANALYSIS_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['file', 'category', 'actions', 'rationale', 'newPath', 'removed', 'linesBefore', 'linesAfter', 'refsToFix', 'anchorsPreserved', 'summary'],
  properties: {
    file: { type: 'string' },
    category: { type: 'string' },
    actions: { type: 'array', items: { type: 'string', enum: ['KEEP', 'UPDATE', 'COMPRESS', 'REMOVE', 'RELOCATE'] } },
    rationale: { type: 'string' },
    newPath: { type: ['string', 'null'], description: 'destination path if RELOCATE, else null' },
    removed: { type: 'boolean' },
    linesBefore: { type: 'integer' },
    linesAfter: { type: 'integer' },
    refsToFix: { type: 'array', items: REF_FIX, description: 'inbound refs that must change because this file was removed/relocated/renamed' },
    anchorsPreserved: { type: 'array', items: { type: 'string' }, description: 'heading anchors kept because other files deep-link them' },
    summary: { type: 'string' },
  },
}
const RECONCILE_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['editsApplied', 'danglingResolved', 'notes'],
  properties: {
    editsApplied: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['file', 'change'], properties: { file: { type: 'string' }, change: { type: 'string' } } } },
    danglingResolved: { type: 'integer' },
    notes: { type: 'string' },
  },
}
const SKEPTIC_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['file', 'ok', 'blocking', 'lensesChecked', 'notes'],
  properties: {
    file: { type: 'string' },
    ok: { type: 'boolean' },
    blocking: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['issue', 'severity', 'fix'], properties: { issue: { type: 'string' }, severity: { type: 'string', enum: ['blocking', 'minor'] }, fix: { type: 'string' } } } },
    lensesChecked: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}
const GLOBAL_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['ok', 'danglingRefs', 'orphanedFiles', 'notes'],
  properties: {
    ok: { type: 'boolean' },
    danglingRefs: { type: 'array', items: { type: 'string' } },
    orphanedFiles: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}
const COMMIT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['committed', 'commitHash', 'filesChanged', 'insertions', 'deletions', 'removedFiles', 'relocatedFiles', 'reportPath', 'diffStat'],
  properties: {
    committed: { type: 'boolean' },
    commitHash: { type: 'string' },
    filesChanged: { type: 'integer' },
    insertions: { type: 'integer' },
    deletions: { type: 'integer' },
    removedFiles: { type: 'array', items: { type: 'string' } },
    relocatedFiles: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['from', 'to'], properties: { from: { type: 'string' }, to: { type: 'string' } } } },
    reportPath: { type: 'string' },
    diffStat: { type: 'string' },
  },
}
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['ok', 'blocking', 'danglingRefs', 'knowledgeResolves', 'testsImpacted', 'stats'],
  properties: {
    ok: { type: 'boolean' },
    blocking: { type: 'array', items: { type: 'string' } },
    danglingRefs: { type: 'array', items: { type: 'string' } },
    knowledgeResolves: { type: 'boolean', description: 'every registry read_knowledge() name + every CORE_KNOWLEDGE file still resolves to an existing knowledge/ file' },
    testsImpacted: { type: 'array', items: { type: 'string' }, description: 'tests that reference a removed/renamed file (must be empty or explained)' },
    stats: {
      type: 'object', additionalProperties: false,
      required: ['mdBefore', 'mdAfter', 'removed', 'relocated', 'compressed', 'updated', 'kept'],
      properties: {
        mdBefore: { type: 'integer' }, mdAfter: { type: 'integer' }, removed: { type: 'integer' },
        relocated: { type: 'integer' }, compressed: { type: 'integer' }, updated: { type: 'integer' }, kept: { type: 'integer' },
      },
    },
  },
}

// ---------------------------------------------------------------------------
// Shared rules (injected into every editing prompt)
// ---------------------------------------------------------------------------
const RULES = `
GROUND RULES (non-negotiable):
- You may ONLY touch .md files under ${PDIR} (this project). NEVER edit/read-modify the sibling
  repos llm-d/ or llm-d-benchmark/ (they are READ-ONLY and are empty in this worktree anyway).
- Work in the worktree ${HOME} on branch ${BR}. NEVER touch ${MONO} (the main checkout) and NEVER
  switch branches, merge, push, or run git in ${MONO}.
- Do NOT run pytest / make / any live LLM eval. Verification here is static (grep/read) only.
- HARD DEPENDENCIES must never be renamed, removed, or relocated (you may still correct/compress
  their CONTENT in place):
    * any knowledge/<name>.md whose <name> is referenced by read_knowledge('<name>') in
      app/tools/registry.py, or listed in CORE_KNOWLEDGE in app/agent/prompt.py;
    * README.md (COPYed by Dockerfile), and any .md referenced by tests/ or Dockerfile/.dockerignore.
- HISTORICAL records (docs/CHANGELOG.md and llm-d-benchmarking-agent-proposal.md): PRESERVE the
  record. You may fix a forward-looking claim that is now wrong, but do NOT compress away history.
- When you REMOVE or RELOCATE a file, you do NOT edit other files — instead REPORT every inbound
  reference in refsToFix (the Reconcile phase fixes them centrally).
- Preserve any heading anchor that another file deep-links (e.g. README links plan.md#implementation-status);
  list those in anchorsPreserved.
- Today is ${TODAY}. Judge staleness against the ground truth in ${INV_JSON} (real tool count,
  latest phase, recent commits) — not against what a doc claims about itself.
`

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------
const PREP_PROMPT = `You are the PREP agent for a documentation-debloat workflow on the
llm-d-benchmarking-agent project. Your job: build the inventory + reference graph + hard-dep map and
create the isolated worktree. Do NOT edit any .md content.

STEPS (run from ${MONO} for inventory; the worktree is a separate dir):
1. List every tracked markdown file in the project:
     git -C ${SRC} ls-files '*.md'
   (43-ish files; paths are relative to the project, e.g. ROADMAP.md, docs/API.md, knowledge/capacity.md)
2. Detect HARD DEPENDENCIES (files that must keep their name + location):
     - read_knowledge names: grep -oE "read_knowledge\\('([a-z_]+)'\\)" ${SRC}/app/tools/registry.py
     - CORE_KNOWLEDGE list: read the CORE_KNOWLEDGE tuple in ${SRC}/app/agent/prompt.py
     - test refs:   grep -rIl --include='*.py' -E '\\.md|read_knowledge|knowledge/' ${SRC}/tests
     - build refs:  grep -nE '\\.md' ${SRC}/Dockerfile ${SRC}/.dockerignore
   A knowledge/<name>.md is a hard dep if <name> is in registry read_knowledge() OR in CORE_KNOWLEDGE.
   README.md is a hard dep (Dockerfile COPY).
3. Build the inbound-reference graph: for each .md file, find which files mention/link it:
     grep -rIn --exclude-dir=workspace --exclude-dir=.git "<basename>" ${SRC}
   Record the most relevant inbound refs (file:line). A file with zero inbound refs is a relocate/remove candidate.
4. Classify each file into exactly one category:
     RUNTIME_KNOWLEDGE = knowledge/*.md (agent's runtime brain)
     INSTRUCTIONS      = CLAUDE.md
     ENTRY             = README.md
     HUMAN_DOC         = docs/*.md  (EXCEPT docs/CHANGELOG.md -> HISTORICAL)
     HISTORICAL        = docs/CHANGELOG.md, llm-d-benchmarking-agent-proposal.md
     TRACKING          = ROADMAP.md, ROADMAP_V4.md, PROGRESS.md, plan.md, PROPOSAL_ROADMAP.md,
                         FEATURES.md, INTERACTIVE_TEST_GUIDE.md
5. Capture GROUND TRUTH for staleness judging:
     - toolCount: count tool definitions in ${SRC}/app/tools/registry.py (authoritative)
     - latestPhase: highest DONE phase across ROADMAP.md / PROGRESS.md
     - recentCommits: git -C ${SRC} log -15 --format='%h %s'
6. Create the worktree (idempotent):
     - if ${HOME} already exists as a worktree on ${BR}, reuse it (git -C ${HOME} status);
       otherwise: git -C ${MONO} worktree remove --force ${HOME} 2>/dev/null; then
       git -C ${MONO} worktree add ${HOME} -b ${BR} ${BASE}
       (if branch ${BR} already exists, use: git -C ${MONO} worktree add ${HOME} ${BR})
     - confirm ${PDIR}/ROADMAP.md exists in the worktree.
7. mkdir -p ${SCRATCH} and write the full inventory (files[], coreKnowledge, registryKnowledgeNames,
   groundTruth) as JSON to ${INV_JSON} so per-file agents can read it.

${RULES}

Return the INVENTORY object. Set worktreeReady=true only if ${PDIR} exists on branch ${BR} and
${INV_JSON} was written.`

const analyzePrompt = (f) => `You are the ANALYZE agent for ONE markdown file in the
llm-d-benchmarking-agent debloat workflow.

TARGET FILE (project-relative): ${f.path}
ABSOLUTE PATH (edit here): ${PDIR}/${f.path}
CATEGORY: ${f.category}
HARD DEPENDENCY: ${f.hardDep} ${f.hardDep ? '(' + f.hardDepReason + ')' : ''}
KNOWN INBOUND REFS: ${(f.inboundRefs || []).join(' | ') || '(none found)'}

FIRST read the shared inventory/ground-truth at ${INV_JSON} (real tool count, latest phase, recent
commits, the full hard-dep lists). THEN read the target file fully.

Decide one or more ACTIONS and APPLY them directly to ${PDIR}/${f.path} (use Edit/Write for content;
'mv' for RELOCATE; 'rm' for REMOVE). Do all edits via the filesystem only — do NOT run any git command
(a later phase stages + commits everything at once).

ACTION VOCABULARY:
- KEEP     : current, helpful, right size/location. Make no edit.
- UPDATE   : fix obsolete/wrong facts and dead references (e.g. wrong tool count, a phase marked TODO
             that is actually DONE per ground truth, a link to a moved file). Keep all helpful context.
- COMPRESS : reduce bloat WITHOUT losing useful future context. For a tracking list that mixes done +
             pending items, collapse each COMPLETED item to a terse one-liner ending in "— done"
             (keep its phase number/title/date for tracking) and keep PENDING/relevant items in full.
             For verbose prose, tighten; never delete a fact someone will still need.
- REMOVE   : the file (or whole file) has no future value (superseded, duplicated elsewhere, one-off).
             Only if NOT a hard dep. 'rm' it and report every inbound ref in refsToFix.
- RELOCATE : the file is in the wrong place — most importantly a knowledge/*.md that is NOT actually
             runtime-brain content (the agent never needs it at runtime; it's really human docs) but is
             still useful elsewhere. Move it to the right dir (e.g. docs/) with 'mv', and report inbound
             refs in refsToFix. NEVER relocate a hard-dep knowledge file (registry read_knowledge / CORE_KNOWLEDGE).

CATEGORY-SPECIFIC GUIDANCE:
- RUNTIME_KNOWLEDGE: correct obsolete facts AND compress redundancy (these load into the runtime prompt,
  so trimming genuinely helps) — but preserve EVERY load-bearing instruction/fact the agent acts on.
  If a knowledge file is misplaced human-doc material the agent never loads, RELOCATE it out of knowledge/.
- INSTRUCTIONS (CLAUDE.md): correct + lightly compress; NEVER drop a non-negotiable rule.
- TRACKING: prime compression target — apply the done→"— done" rule above. ROADMAP_V4.md is all-pending;
  KEEP it. Preserve anchors other files deep-link.
- HUMAN_DOC: update for accuracy against ground truth; tighten if verbose; keep the audience-facing value.
- HISTORICAL: preserve the record; only correct now-wrong forward-looking claims.
- ENTRY (README.md): keep + update; it is a hard dep (do not move/rename).

${RULES}

Return the ANALYSIS object with accurate linesBefore/linesAfter (count the file before and after),
the actions you actually applied, and any refsToFix.`

const reconcilePrompt = (refs, movedOrRemoved) => `You are the RECONCILE agent. Several files were
REMOVED or RELOCATED by the Analyze phase. Update every inbound reference across ${PDIR} so nothing
dangles. Edit referencing files in place (filesystem only, no git).

Removed/relocated files: ${JSON.stringify(movedOrRemoved.map(a => ({ file: a.file, removed: a.removed, newPath: a.newPath })))}
Reference fixes proposed by analyzers: ${JSON.stringify(refs)}

ALSO independently grep ${PDIR} for ANY remaining mention/link of each removed/relocated basename
(markdown links, prose mentions, doc index tables like docs/README.md) and fix them:
- relocated file -> update the link/path to the new location;
- removed file   -> remove the link or repoint it to the surviving canonical doc.
Do NOT introduce new dangling links. Do not touch hard-dep code/tests (those were guaranteed not moved).

${RULES}

Return RECONCILE with the edits you applied and danglingResolved count.`

const skepticPrompt = (a) => `You are an adversarial SKEPTIC reviewing ONE changed markdown file in the
debloat worktree. Default to skepticism: assume the change is too aggressive until proven safe.

FILE: ${a.file}
CLAIMED ACTIONS: ${(a.actions || ['(re-review)']).join(', ')}

Compare the original vs the new version:
  original:  git -C ${HOME} show HEAD:${PROJ}/${a.file}    (if removed/relocated, it won't exist in working tree)
  current :  read ${PDIR}/${a.file}  (or its new path if relocated)

Check these lenses and only pass each if truly satisfied:
1. NO DATA LOSS — did COMPRESS/REMOVE drop any unique, still-useful future context? A completed item
   collapsed to "— done" is fine; losing a still-pending item, a unique caveat, or a needed how-to is NOT.
2. RUNTIME SAFETY — for knowledge/*.md or CLAUDE.md: is any load-bearing fact/instruction the agent
   acts on now missing? Was a hard-dep file (registry read_knowledge / CORE_KNOWLEDGE / README) renamed,
   removed, or relocated? That is always blocking. Re-check against ${INV_JSON}.
3. REFERENCE INTEGRITY — grep ${PDIR} for this file's basename: any inbound link/mention now dangling
   or pointing at the wrong path? Any preserved anchor actually removed?
4. CORRECTNESS — are the UPDATE edits actually true per ground truth (not a new error)?

${RULES}

Return SKEPTIC. ok=false with severity 'blocking' for any real problem (include a concrete fix).`

const globalPrompt = () => `You are the GLOBAL CONSISTENCY checker for the debloat worktree.
Independently of per-file review, verify the whole project is internally consistent after the edits:
- grep ${PDIR} (exclude workspace/, .git/) for links/mentions of every file that was REMOVED or
  RELOCATED; list any that still dangle (danglingRefs).
- list any .md file that is now orphaned in a wrong place (orphanedFiles).
- confirm docs/README.md's doc index and the root README/CLAUDE cross-links still resolve.
Read ${INV_JSON} for the original file set. Do not edit anything.
${RULES}
Return GLOBAL.`

const fixPrompt = (b) => `You are a FIX agent. A skeptic flagged blocking problem(s) on ${b.file} in the
debloat worktree. Re-open the original (git -C ${HOME} show HEAD:${PROJ}/${b.file}) and the current
working-tree version, and resolve EVERY blocking objection below by editing ${PDIR}/${b.file}
(or restoring/relocating it). Filesystem edits only, no git.

BLOCKING OBJECTIONS: ${JSON.stringify(b.blocking || b)}

Restore any wrongly-dropped load-bearing content; undo any hard-dep rename/remove/relocate; repair
any dangling reference. Prefer the least-aggressive change that removes the objection.
${RULES}
Return ANALYSIS describing the corrected final state of the file.`

const commitPrompt = (extra) => `You are the COMMIT agent for the debloat worktree.
${extra || ''}
From ${HOME}:
1. git -C ${HOME} add -A
2. git -C ${HOME} status --short   (capture)
3. git -C ${HOME} diff --cached --stat   (capture as diffStat)
4. Write a human-readable change report to ${REPORT} (gitignored): one row per .md file with its
   action(s), lines before→after, and (for removed/relocated) where it went and which refs were fixed;
   plus totals. This report is for the user's review.
5. Commit with a heredoc message:
     git -C ${HOME} commit -F - <<'MSG'
     chore(docs): debloat + refresh project markdown (worktree, review before merge)

     <2-4 line summary: how many files compressed/updated/removed/relocated/kept; headline reductions
     (e.g. ROADMAP.md / PROGRESS.md completed-item compression); note no hard-deps moved.>

     ${TRAILER}
     MSG
Do NOT push and do NOT merge to ${BASE}. Stay on ${BR}.

Return COMMIT with the real commitHash, counts, removedFiles, relocatedFiles[{from,to}], reportPath, diffStat.`

const VERIFY_PROMPT = `You are the final VERIFY agent (read-mostly; you may grep/read/cat and run
read-only git, but make NO edits and run NO tests). Verify the debloat commit on branch ${BR} in ${HOME}.

Checks (all must pass for ok=true):
1. DANGLING REFS: grep ${PDIR} (exclude workspace/, .git/) for any link/mention of a removed or
   relocated file's old path/basename → must be empty (report any in danglingRefs).
2. KNOWLEDGE AUTO-DISCOVERY: read ${INV_JSON}; for every name in registryKnowledgeNames and every
   file in coreKnowledge, confirm ${PDIR}/knowledge/<name>.md still exists (or a .yaml for non-md
   names) → knowledgeResolves. Any miss is blocking.
3. FILE ACCOUNTING: compare the original .md set (from ${INV_JSON}) to the current set
   (git -C ${HOME} ls-files '${PROJ}/*.md'); every original file is KEEP, changed, removed, or
   relocated — none silently vanished without being in removedFiles/relocatedFiles. Fill stats
   (mdBefore, mdAfter, removed, relocated, compressed, updated, kept).
4. TESTS: grep ${PDIR}/tests for any reference to a removed/renamed .md or knowledge name → testsImpacted
   (should be empty; list any).
5. MD LINT: spot-check that edited tables are well-formed (consistent columns) and intra-repo links
   resolve to existing files.

Return VERIFY. Put any failure in blocking[].`

// ---------------------------------------------------------------------------
// Flow
// ---------------------------------------------------------------------------
phase('Prep')
const inv = await agent(PREP_PROMPT, { schema: INVENTORY_SCHEMA, label: 'prep', agentType: 'general-purpose' })
if (!inv || !inv.worktreeReady) {
  log('Prep failed: worktree not ready — aborting.')
  return { error: 'prep-failed', inv }
}
log(`Inventory: ${inv.totalMd} .md files; toolCount=${inv.groundTruth.toolCount}; latestPhase=${inv.groundTruth.latestPhase}`)

phase('Analyze')
let analyses = (await parallel(inv.files.map(f => () =>
  agent(analyzePrompt(f), { schema: ANALYSIS_SCHEMA, phase: 'Analyze', label: 'an:' + base(f.path), agentType: 'general-purpose' })
))).filter(Boolean)
const changedNames = a => (a.actions || []).some(x => x !== 'KEEP')
let changed = analyses.filter(changedNames)
log(`Analyze done: ${analyses.length} files; ${changed.length} changed, ${analyses.length - changed.length} kept.`)

phase('Reconcile')
const refsToFix = analyses.flatMap(a => a.refsToFix || [])
const movedOrRemoved = analyses.filter(a => a.removed || a.newPath)
if (refsToFix.length || movedOrRemoved.length) {
  await agent(reconcilePrompt(refsToFix, movedOrRemoved), { schema: RECONCILE_SCHEMA, label: 'reconcile', agentType: 'general-purpose' })
} else {
  log('No removals/relocations — skipping reference reconciliation.')
}

phase('Skeptic')
let skeptics = (await parallel(changed.map(a => () =>
  agent(skepticPrompt(a), { schema: SKEPTIC_SCHEMA, phase: 'Skeptic', label: 'sk:' + base(a.file), agentType: 'general-purpose' })
))).filter(Boolean)
let globalCheck = await agent(globalPrompt(), { schema: GLOBAL_SCHEMA, label: 'global', phase: 'Skeptic', agentType: 'general-purpose' })

// bounded fix loop
let round = 0
let blockers = skeptics.filter(s => !s.ok)
let globalBad = globalCheck && !globalCheck.ok ? [{ file: '<global>', blocking: globalCheck.danglingRefs }] : []
while ((blockers.length || globalBad.length) && round < 2) {
  round++
  phase('Fix')
  const targets = blockers.concat(globalBad)
  log(`Fix round ${round}: ${targets.length} blocking item(s).`)
  await parallel(targets.map(b => () =>
    agent(fixPrompt(b), { schema: ANALYSIS_SCHEMA, phase: 'Fix', label: 'fix:' + base(b.file), agentType: 'general-purpose' })
  ))
  skeptics = (await parallel(blockers.map(b => () =>
    agent(skepticPrompt({ file: b.file, actions: ['(re-review)'] }), { schema: SKEPTIC_SCHEMA, phase: 'Fix', label: 'reverify:' + base(b.file), agentType: 'general-purpose' })
  ))).filter(Boolean)
  globalCheck = await agent(globalPrompt(), { schema: GLOBAL_SCHEMA, label: 'global-recheck', phase: 'Fix', agentType: 'general-purpose' })
  blockers = skeptics.filter(s => !s.ok)
  globalBad = globalCheck && !globalCheck.ok ? [{ file: '<global>', blocking: globalCheck.danglingRefs }] : []
}
if (blockers.length || globalBad.length) log(`WARNING: ${blockers.length + globalBad.length} blocking item(s) remain after ${round} fix round(s) — surfaced for review.`)

phase('Commit')
const commit = await agent(commitPrompt(), { schema: COMMIT_SCHEMA, label: 'commit', agentType: 'general-purpose' })

phase('Verify')
let verify = await agent(VERIFY_PROMPT, { schema: VERIFY_SCHEMA, label: 'verify', agentType: 'general-purpose' })
if (verify && !verify.ok) {
  phase('Fix')
  log(`Verify found ${verify.blocking.length} blocking issue(s) — one bounded fix+recommit.`)
  await agent(fixPrompt({ file: '(verify findings)', blocking: verify.blocking.concat(verify.danglingRefs) }), { schema: ANALYSIS_SCHEMA, label: 'verify-fix', agentType: 'general-purpose' })
  await agent(commitPrompt('(amend the debloat commit to fold in the verify fixes — use git add -A then a follow-up commit, do not rewrite published history)'), { schema: COMMIT_SCHEMA, label: 'recommit', agentType: 'general-purpose' })
  verify = await agent(VERIFY_PROMPT, { schema: VERIFY_SCHEMA, label: 'verify2', agentType: 'general-purpose' })
}

const counts = {
  total: analyses.length,
  kept: analyses.filter(a => (a.actions || []).length === 1 && a.actions[0] === 'KEEP').length,
  compressed: analyses.filter(a => (a.actions || []).includes('COMPRESS')).length,
  updated: analyses.filter(a => (a.actions || []).includes('UPDATE')).length,
  removed: analyses.filter(a => a.removed).length,
  relocated: analyses.filter(a => a.newPath).length,
}
log(`Debloat complete on ${BR}: ${JSON.stringify(counts)} | verify.ok=${verify && verify.ok}`)
return { branch: BR, worktree: HOME, counts, commit, verify, reportPath: REPORT }
