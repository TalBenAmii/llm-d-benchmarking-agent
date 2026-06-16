// roadmap-v4-refresh — reusable workflow.
// Re-derives ROADMAP_V4 from the post-V4 docs: mines docs/USEFUL_REPO_DOCS.md (the 137 llm-d
// deploy-side docs the benchmark catalog never reached) + the refreshed FEATURES/PROGRESS state,
// re-assesses every existing phase, adversarially verifies new gaps, and reprioritizes by coverage
// impact. Run with: Workflow({name:'roadmap-v4-refresh'}). It RETURNS structured pieces
// (priority_order, existing_updates, changelog_notes, new_blocks) — it does NOT write the file.
// Apply them to ROADMAP_V4.md as faithful, deterministic edits on a worktree branch off main
// (existing phase bodies stay byte-exact; only status lines + appended new phases change).
// Re-run when docs/USEFUL_REPO_DOCS.md or the coverage catalog changes.

export const meta = {
  name: 'roadmap-v4-refresh',
  description: 'Update ROADMAP_V4 using post-V4 docs (USEFUL_REPO_DOCS + refreshed state) to add coverage + reprioritize',
  phases: [
    { title: 'Ingest', detail: 'mine post-V4 docs: new capabilities + current state + existing phases' },
    { title: 'Classify', detail: 'new-gap finders + existing-phase re-assessors' },
    { title: 'Verify', detail: 'adversarial skeptics on new phases + done/defer claims' },
    { title: 'Compose', detail: 'reprioritize by coverage impact, number new phases' },
    { title: 'Assemble', detail: 'author each new phase skeleton' },
    { title: 'Finalize', detail: 'completeness critic' },
  ],
}

const P = '/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project'
const DOCS = {
  roadmapV4: `${P}/ROADMAP_V4.md`,
  catalog: `${P}/docs/BENCHMARK_FEATURE_COVERAGE.md`,
  usefulDocs: `${P}/docs/USEFUL_REPO_DOCS.md`,
  usefulPtr: `${P}/knowledge/useful_repo_docs.md`,
  features: `${P}/FEATURES.md`,
  progress: `${P}/PROGRESS.md`,
  claude: `${P}/CLAUDE.md`,
}
const LAW = `HARD CONSTRAINTS: (1) the sibling repos llm-d/ and llm-d-benchmark/ are READ-ONLY — never propose writing into them; the agent reads their docs/specs at runtime and shells out to the llmdbenchmark CLI, and authors any generated config INTO the session workspace only. (2) Thin code, thick agent: mechanism in Python (flags, allowlist DATA, validation), JUDGMENT lives in knowledge/ markdown/yaml — never if/elif decision logic in Python. (3) Allowlist widening is DATA only (security/allowlist.yaml). (4) Secrets stay backend-only + scrubbed. (5) Every phase must be hermetically testable with pytest — no GPU, no live cluster, no real benchmark run. (6) Each phase keeps the skeleton GOAL / BUILD / ACCEPTANCE / HERMETIC-TEST.`

// ---------- schemas ----------
const CANDIDATES = {
  type: 'object', additionalProperties: false,
  properties: { candidates: { type: 'array', items: {
    type: 'object', additionalProperties: false,
    properties: {
      name: { type: 'string' },
      source_doc: { type: 'string', description: 'exact upstream doc path as written in USEFUL_REPO_DOCS.md' },
      tier: { type: 'string' },
      side: { type: 'string', enum: ['llm-d', 'llm-d-benchmark'] },
      what_it_is: { type: 'string' },
      why_valuable: { type: 'string', description: 'how supporting this broadens what the agent can do for a user' },
    }, required: ['name', 'source_doc', 'side', 'what_it_is', 'why_valuable'],
  } } }, required: ['candidates'],
}
const CURRENT_STATE = {
  type: 'object', additionalProperties: false,
  properties: {
    capabilities: { type: 'array', items: { type: 'object', additionalProperties: false,
      properties: { capability: { type: 'string' }, evidence: { type: 'string' } }, required: ['capability', 'evidence'] } },
    notes: { type: 'string' },
  }, required: ['capabilities'],
}
const EXISTING = {
  type: 'object', additionalProperties: false,
  properties: { phases: { type: 'array', items: { type: 'object', additionalProperties: false,
    properties: {
      num: { type: 'integer' }, title: { type: 'string' }, gap_ref: { type: 'string' },
      status: { type: 'string' }, summary: { type: 'string' }, deferred: { type: 'boolean' },
    }, required: ['num', 'title', 'status', 'summary', 'deferred'] } } }, required: ['phases'],
}
const CLASSIFY = {
  type: 'object', additionalProperties: false,
  properties: { results: { type: 'array', items: { type: 'object', additionalProperties: false,
    properties: {
      name: { type: 'string' }, source_doc: { type: 'string' }, side: { type: 'string' },
      verdict: { type: 'string', enum: ['new_gap', 'already_covered', 'already_in_v4', 'out_of_scope'] },
      rationale: { type: 'string' },
      maps_to_existing_phase: { type: ['integer', 'null'] },
      proposed_title: { type: ['string', 'null'] },
      goal: { type: ['string', 'null'] },
      build_sketch: { type: ['string', 'null'] },
      coverage_impact: { type: 'string', enum: ['high', 'medium', 'low'] },
      constraints_ok: { type: 'boolean' },
    }, required: ['name', 'source_doc', 'side', 'verdict', 'rationale', 'coverage_impact', 'constraints_ok'] } } },
  required: ['results'],
}
const REASSESS = {
  type: 'object', additionalProperties: false,
  properties: { assessments: { type: 'array', items: { type: 'object', additionalProperties: false,
    properties: {
      num: { type: 'integer' }, title: { type: 'string' },
      verdict: { type: 'string', enum: ['keep', 'done', 'reprioritize_up', 'reprioritize_down', 'defer', 'merge'] },
      coverage_impact: { type: 'string', enum: ['high', 'medium', 'low'] },
      rationale: { type: 'string' }, merge_into: { type: ['integer', 'null'] },
    }, required: ['num', 'title', 'verdict', 'coverage_impact', 'rationale'] } } }, required: ['assessments'],
}
const VERDICT = {
  type: 'object', additionalProperties: false,
  properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } }, required: ['refuted', 'reason'],
}
const COMPOSE = {
  type: 'object', additionalProperties: false,
  properties: {
    priority_order: { type: 'array', items: { type: 'object', additionalProperties: false,
      properties: {
        rank: { type: 'integer' }, phase_num: { type: 'integer' }, title: { type: 'string' },
        tier: { type: 'string', enum: ['P1', 'P2', 'P3'] }, origin: { type: 'string', enum: ['existing', 'new'] },
        one_line_why: { type: 'string' },
      }, required: ['rank', 'phase_num', 'title', 'tier', 'origin', 'one_line_why'] } },
    existing_updates: { type: 'array', items: { type: 'object', additionalProperties: false,
      properties: { num: { type: 'integer' }, new_status: { type: 'string', enum: ['TODO', 'DONE', 'DEFERRED'] }, reason: { type: 'string' } },
      required: ['num', 'new_status', 'reason'] } },
    changelog_notes: { type: 'array', items: { type: 'string' } },
  }, required: ['priority_order', 'existing_updates', 'changelog_notes'],
}
const BLOCK = {
  type: 'object', additionalProperties: false,
  properties: { markdown: { type: 'string' } }, required: ['markdown'],
}
const CRITIC = {
  type: 'object', additionalProperties: false,
  properties: {
    ok: { type: 'boolean' },
    missing_originals: { type: 'array', items: { type: 'integer' } },
    bad_sources: { type: 'array', items: { type: 'string' } },
    problems: { type: 'array', items: { type: 'object', additionalProperties: false,
      properties: { kind: { type: 'string' }, detail: { type: 'string' } }, required: ['kind', 'detail'] } },
  }, required: ['ok', 'missing_originals', 'bad_sources', 'problems'],
}

// ============================================================ INGEST
phase('Ingest')
const [benchMined, llmdMined, refMined, current, existing] = await parallel([
  () => agent(
    `You are mining the post-ROADMAP_V4 doc docs/USEFUL_REPO_DOCS.md (a curated index of 195 upstream docs) for capabilities the llm-d-benchmarking-agent could newly cover to INCREASE its feature coverage.\n` +
    `Read ${DOCS.usefulDocs} lines 1-132 (the llm-d-benchmark sections: Start here, Core lifecycle & CLI, Workloads/harnesses/scenarios, Analysis/DOE/report, Observability & metrics, Reproducibility/resources/versions, CLI module internals, Config & convert-guide, Stack discovery). Read ${DOCS.usefulPtr} for orientation.\n` +
    `For EACH concrete capability / feature / flag / knob / flow a listed doc surfaces that the agent could plausibly support, emit a candidate {name, source_doc (exact path as written in the index), tier, side:'llm-d-benchmark', what_it_is, why_valuable}. Prefer things that look like genuinely NEW coverage opportunities over generic background. ${LAW}`,
    { label: 'mine:bench-side', phase: 'Ingest', schema: CANDIDATES }),
  () => agent(
    `You are mining docs/USEFUL_REPO_DOCS.md for NEW coverage opportunities on the llm-d DEPLOY-STACK side — the vein the benchmark-CLI coverage catalog (which ROADMAP_V4 was derived from) NEVER mined, so it is the richest source of new phases.\n` +
    `Read ${DOCS.usefulDocs} lines 133-235 (the llm-d sections: Deploy guides / well-lit paths, Helpers & benchmarking, Well-lit paths, API & CRD reference, Observability resources, Architecture, Infra providers & preconditions). Read ${DOCS.usefulPtr} for orientation.\n` +
    `For EACH well-lit path (optimized-baseline, pd-disaggregation, precise-prefix-cache-routing, predicted-latency-routing, tiered-prefix-cache, wide-ep-lws, etc.), EPP/routing/CRD config, readiness/precondition check, observability resource (PromQL/dashboards/metric names), or infra-provider capability the agent could help a user DEPLOY, BENCHMARK, or INTERPRET, emit a candidate {name, source_doc (exact path), tier, side:'llm-d', what_it_is, why_valuable}. ${LAW}`,
    { label: 'mine:llmd-side', phase: 'Ingest', schema: CANDIDATES }),
  () => agent(
    `You are mining the reference-point + external + skipped tail of docs/USEFUL_REPO_DOCS.md for cross-cutting NEW coverage opportunities.\n` +
    `Read ${DOCS.usefulDocs} lines 236-496 (API & feature reference points [benchmark CLI/lifecycle flags, workload & scenario keys, LLMDBENCH_* env vars, config/spec keys, Benchmark Report v0.2 fields, llm-d CRDs/EPP/routing config, observability knobs, stack-discovery helpers], External references, and the Lower-relevance & skipped appendix).\n` +
    `Emit candidates for concrete keys/knobs/fields/tools that imply a coverage opportunity (set side appropriately). ALSO scan the skipped/low appendix for anything wrongly dropped that is actually a usable feature. ${LAW}`,
    { label: 'mine:ref+external', phase: 'Ingest', schema: CANDIDATES }),
  () => agent(
    `Build the CURRENT implemented-capability inventory of the llm-d-benchmarking-agent, so later steps can tell which roadmap items are already done.\n` +
    `Read ${DOCS.features} (FEATURES.md — the post-refresh truth: the 22 tools, orchestrator, analyzer, observability, security, packaging) and ${DOCS.progress} (PROGRESS.md — completed phases). Optionally skim ${DOCS.claude} for the rules.\n` +
    `Return capabilities[] = {capability, evidence (file/tool/phase ref)}. Be precise and exhaustive about what is ALREADY implemented today.`,
    { label: 'inventory:current-state', phase: 'Ingest', schema: CURRENT_STATE }),
  () => agent(
    `Inventory the CURRENT ROADMAP_V4 baseline we will reprioritize and extend.\n` +
    `Read ${DOCS.roadmapV4} (Phases 27-58) and the coverage summary at the top of ${DOCS.catalog}.\n` +
    `For every phase return {num, title (verbatim), gap_ref, status, summary (one line), deferred}.`,
    { label: 'inventory:existing-phases', phase: 'Ingest', schema: EXISTING }),
])

const allCandidates = [benchMined, llmdMined, refMined].filter(Boolean).flatMap(r => r.candidates || [])
const currentJSON = JSON.stringify(current?.capabilities || [])
const existingJSON = JSON.stringify((existing?.phases || []).map(p => ({ num: p.num, title: p.title, summary: p.summary, status: p.status })))
log(`Ingest: ${allCandidates.length} candidate capabilities mined; ${(existing?.phases || []).length} existing phases; ${(current?.capabilities || []).length} current capabilities.`)

// ============================================================ CLASSIFY
phase('Classify')
const benchCands = allCandidates.filter(c => c.side === 'llm-d-benchmark')
const llmdCands = allCandidates.filter(c => c.side !== 'llm-d-benchmark')
const half = Math.ceil((existing?.phases || []).length / 2)
const exA = (existing?.phases || []).slice(0, half)
const exB = (existing?.phases || []).slice(half)

const finderPrompt = (cands, sideLabel) =>
  `Decide whether each candidate capability is a NEW coverage gap worth a roadmap phase, or already handled.\n` +
  `CANDIDATES (${sideLabel}): ${JSON.stringify(cands)}\n\n` +
  `What the agent ALREADY implements: ${currentJSON}\n\n` +
  `EXISTING ROADMAP_V4 phases (27-58): ${existingJSON}\n\n` +
  `For each candidate classify verdict: 'already_covered' (the agent already does it — cite the capability evidence), 'already_in_v4' (an existing phase already covers it — set maps_to_existing_phase), 'out_of_scope' (violates a HARD constraint — e.g. requires writing into the read-only repos with no workspace-local variant, or is pure governance/CI), or 'new_gap'.\n` +
  `For 'new_gap' ALSO fill proposed_title, goal, build_sketch (thin-code/thick-agent compliant; name the knowledge/ file judgment goes in + the allowlist DATA / schema mechanism), coverage_impact (high = unlocks a deploy path / metric family / workflow many users need; low = niche/experimental), constraints_ok. Dedupe AGGRESSIVELY against existing phases and current capabilities — when in doubt it is already covered. ${LAW}`

const reassessPrompt = (phases) =>
  `Re-assess these existing ROADMAP_V4 phases against the agent's CURRENT implemented state, to make the roadmap more relevant.\n` +
  `PHASES: ${JSON.stringify(phases.map(p => ({ num: p.num, title: p.title, summary: p.summary })))}\n\n` +
  `Current implemented capabilities: ${currentJSON}\n\n` +
  `For each phase return verdict: 'done' (already implemented now — cite evidence), 'keep' (still a valid open TODO), 'reprioritize_up' (high coverage impact, belongs near the top), 'reprioritize_down' (low impact), 'defer' (environment-gated out of the kind/CPU MVP, e.g. OpenShift-only WVA, or experimental upstream), 'merge' (duplicate — set merge_into). Give coverage_impact + a one-line rationale grounded in the current-state evidence. Do NOT invent; if unsure whether something is implemented, say 'keep'.`

const [clsBench, clsLlmd, reA, reB] = await parallel([
  () => agent(finderPrompt(benchCands, 'llm-d-benchmark side'), { label: 'classify:bench', phase: 'Classify', schema: CLASSIFY }),
  () => agent(finderPrompt(llmdCands, 'llm-d deploy-stack side'), { label: 'classify:llm-d', phase: 'Classify', schema: CLASSIFY }),
  () => agent(reassessPrompt(exA), { label: 'reassess:27-42', phase: 'Classify', schema: REASSESS }),
  () => agent(reassessPrompt(exB), { label: 'reassess:43-58', phase: 'Classify', schema: REASSESS }),
])

const proposedNew = [clsBench, clsLlmd].filter(Boolean)
  .flatMap(r => r.results || [])
  .filter(r => r.verdict === 'new_gap' && r.constraints_ok !== false)
const assessments = [reA, reB].filter(Boolean).flatMap(r => r.assessments || [])
log(`Classify: ${proposedNew.length} candidate NEW phases proposed; ${assessments.filter(a => a.verdict === 'done').length} existing flagged done, ${assessments.filter(a => a.verdict === 'defer').length} defer.`)

// ============================================================ VERIFY
phase('Verify')
// New phases: 2 adversarial lenses (already-covered? + constraint/source-valid?). Survive only if BOTH pass.
const verifiedNew = (await pipeline(
  proposedNew,
  cand => parallel([
    () => agent(
      `Try to REFUTE this proposed NEW ROADMAP_V4 phase by showing the agent ALREADY covers it (or an existing phase already plans it).\n` +
      `PROPOSED: ${JSON.stringify({ title: cand.proposed_title, goal: cand.goal, name: cand.name })}\n` +
      `Current capabilities: ${currentJSON}\nExisting phases: ${existingJSON}\n` +
      `If needed, read ${DOCS.features} or ${DOCS.roadmapV4} to check. refuted=true if it is already covered or already a planned phase. Default refuted=true if you cannot convince yourself it adds genuinely new coverage.`,
      { label: `verify:covered:${cand.proposed_title?.slice(0, 24)}`, phase: 'Verify', schema: VERDICT }),
    () => agent(
      `Try to REFUTE this proposed NEW ROADMAP_V4 phase on (a) CONSTRAINTS or (b) SOURCE validity.\n` +
      `PROPOSED: ${JSON.stringify({ title: cand.proposed_title, goal: cand.goal, build: cand.build_sketch, source_doc: cand.source_doc })}\n` +
      `${LAW}\n` +
      `(a) refuted=true if it needs writing into the read-only repos with no workspace-local path, puts judgment in Python if/elif, can't be hermetically pytested, or is pure governance/CI. (b) Open ${DOCS.usefulDocs} and confirm the cited source_doc actually exists in the index and plausibly supports this feature — refuted=true if the source is fabricated or unsupported.`,
      { label: `verify:constraint:${cand.proposed_title?.slice(0, 24)}`, phase: 'Verify', schema: VERDICT }),
  ]).then(vs => ({ cand, verdicts: vs.filter(Boolean) }))
)).filter(Boolean)
  .filter(x => x.verdicts.length === 2 && x.verdicts.every(v => !v.refuted))
  .map(x => x.cand)

// Existing 'done'/'defer' claims: 1 skeptic each. If skeptic refutes (gap still open), downgrade to 'keep'.
const claims = assessments.filter(a => a.verdict === 'done' || a.verdict === 'defer')
const claimVerdicts = await parallel(claims.map(a => () =>
  agent(
    `A re-assessment claims existing ROADMAP_V4 Phase ${a.num} ("${a.title}") should be '${a.verdict}'. Try to REFUTE that by showing the gap is in fact STILL OPEN (the feature is NOT actually implemented, or — for 'defer' — that it is actually high-value on the kind/CPU MVP and should stay active).\n` +
    `Current capabilities: ${currentJSON}\n` +
    `Read ${DOCS.features} / ${DOCS.progress} to check. refuted=true if the '${a.verdict}' claim is WRONG (the phase is a real, still-relevant open gap).`,
    { label: `verify:claim:P${a.num}`, phase: 'Verify', schema: VERDICT },
  ).then(v => ({ num: a.num, refuted: v?.refuted }))
))
const overturned = new Set(claimVerdicts.filter(Boolean).filter(v => v.refuted).map(v => v.num))
const finalAssessments = assessments.map(a =>
  ((a.verdict === 'done' || a.verdict === 'defer') && overturned.has(a.num)) ? { ...a, verdict: 'keep', rationale: a.rationale + ' [skeptic overturned: gap still open → kept]' } : a)
log(`Verify: ${verifiedNew.length}/${proposedNew.length} new phases survived adversarial check; ${overturned.size} done/defer claims overturned.`)

// Assign new phase numbers (>=59), ordered by coverage impact then side.
const impactRank = { high: 0, medium: 1, low: 2 }
const orderedNew = [...verifiedNew].sort((a, b) =>
  (impactRank[a.coverage_impact] - impactRank[b.coverage_impact]) || a.side.localeCompare(b.side))
const newPhases = orderedNew.map((c, i) => ({ ...c, num: 59 + i }))

// ============================================================ COMPOSE
phase('Compose')
const compose = await agent(
  `Produce the reprioritized ROADMAP_V4 execution plan. Goal: make the roadmap MORE RELEVANT and INCREASE coverage.\n\n` +
  `EXISTING phases (27-58) with re-assessments: ${JSON.stringify(finalAssessments)}\n\n` +
  `VERIFIED NEW phases (already numbered 59+): ${JSON.stringify(newPhases.map(p => ({ num: p.num, title: p.proposed_title, side: p.side, source_doc: p.source_doc, coverage_impact: p.coverage_impact, goal: p.goal })))}\n\n` +
  `Produce:\n` +
  `1) priority_order — ONE ranked execution list across ALL ACTIVE phases (every existing phase NOT marked done/deferred, plus every new phase). Highest coverage-impact first. Each {rank, phase_num, title, tier(P1/P2/P3), origin('existing'|'new'), one_line_why}. RESPECT dependencies: the monitoring producer (Phase 27) must rank before its consumer (Phase 49); pair-phases (e.g. 39↔47, 48↔52) should be adjacent. Put high-impact deploy-path / observability / metric-family work in P1; environment-gated or experimental work in P3.\n` +
  `2) existing_updates — for every existing phase whose STATUS should change: {num, new_status('DONE'|'DEFERRED'|'TODO'), reason}. Mark 'done' assessments DONE and 'defer' assessments DEFERRED. Phases staying active do NOT need an entry.\n` +
  `3) changelog_notes — concise bullets describing what changed and why: new phases added (and the doc vein they came from), reprioritizations, things marked DONE/DEFERRED.\n` +
  `COMPLETENESS: every existing phase 27-58 MUST appear in EITHER priority_order OR existing_updates (as DONE/DEFERRED). Nothing silently dropped.`,
  { label: 'compose:plan', phase: 'Compose', schema: COMPOSE })

// ============================================================ ASSEMBLE
phase('Assemble')
const newBlocks = await parallel(newPhases.map(p => () =>
  agent(
    `Write the ROADMAP_V4 markdown block for this NEW phase in the EXACT house style of the existing phases.\n` +
    `Phase number: ${p.num}. Title: ${p.proposed_title}. Source doc: ${p.source_doc} (surfaced via docs/USEFUL_REPO_DOCS.md). Side: ${p.side}.\n` +
    `Intent: ${JSON.stringify({ goal: p.goal, build_sketch: p.build_sketch, why_valuable: p.why_valuable, coverage_impact: p.coverage_impact })}\n\n` +
    `Output EXACTLY this shape (markdown, no fences):\n` +
    `## Phase ${p.num} — <concise title> — TODO\n` +
    `*Source: ${p.source_doc} (via docs/USEFUL_REPO_DOCS.md). <one line: how this increases coverage>.*\n\n` +
    `- **GOAL:** <what user-facing capability this unlocks>\n` +
    `- **BUILD:** <mechanism in Python (which app/ file, which schema field, which security/allowlist.yaml DATA widening), judgment in which knowledge/ file; repos stay read-only — author any generated artifact into the session workspace>\n` +
    `- **ACCEPTANCE:** <observable outcome; decision logic lives in knowledge/, not Python>\n` +
    `- **HERMETIC-TEST:** <pytest assertion(s); no GPU / live cluster / real benchmark>\n\n` +
    `${LAW}\nReturn ONLY the markdown block.`,
    { label: `assemble:P${p.num}`, phase: 'Assemble', schema: BLOCK },
  ).then(b => ({ num: p.num, title: p.proposed_title, source_doc: p.source_doc, markdown: b?.markdown || '' }))
))

// ============================================================ FINALIZE
phase('Finalize')
const originalNums = (existing?.phases || []).map(p => p.num)
const critic = await agent(
  `Audit this ROADMAP_V4 revision for completeness and faithfulness.\n` +
  `Original phases: ${JSON.stringify(originalNums)}\n` +
  `priority_order: ${JSON.stringify(compose?.priority_order || [])}\n` +
  `existing_updates: ${JSON.stringify(compose?.existing_updates || [])}\n` +
  `NEW phases: ${JSON.stringify(newBlocks.filter(Boolean).map(b => ({ num: b.num, title: b.title, source_doc: b.source_doc })))}\n\n` +
  `Check: (a) missing_originals = any original phase NOT in priority_order AND NOT in existing_updates as DONE/DEFERRED. (b) bad_sources = any NEW phase whose source_doc looks fabricated/implausible (open ${DOCS.usefulDocs} to spot-check). (c) problems = any phase that violates ${LAW}, or a broken producer→consumer ordering (Phase 27 before 49). Return ok=true only if there are zero missing_originals and zero bad_sources.`,
  { label: 'finalize:critic', phase: 'Finalize', schema: CRITIC })

// Deterministic safety net: append any silently-missing original to priority_order as P3.
const priority = [...(compose?.priority_order || [])]
const seen = new Set(priority.map(r => r.phase_num).concat((compose?.existing_updates || []).filter(u => u.new_status !== 'TODO').map(u => u.num)))
for (const n of originalNums) {
  if (!seen.has(n)) priority.push({ rank: priority.length + 1, phase_num: n, title: `Phase ${n}`, tier: 'P3', origin: 'existing', one_line_why: 'retained (auto-added by completeness safety net — review)' })
}

return {
  priority_order: priority,
  existing_updates: compose?.existing_updates || [],
  changelog_notes: compose?.changelog_notes || [],
  new_blocks: newBlocks.filter(Boolean).sort((a, b) => a.num - b.num),
  new_phase_meta: newPhases.map(p => ({ num: p.num, title: p.proposed_title, side: p.side, source_doc: p.source_doc, coverage_impact: p.coverage_impact })),
  critic,
  stats: {
    candidates_mined: allCandidates.length,
    proposed_new: proposedNew.length,
    verified_new: verifiedNew.length,
    overturned_claims: [...overturned],
  },
}