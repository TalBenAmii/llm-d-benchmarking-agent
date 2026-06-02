export const meta = {
  name: 'benchmark-catalog-gap',
  description: 'Read every API/feature .md in the llm-d-benchmark repo, cross-reference each documented feature against THIS agent project\'s coverage (22 tools + knowledge/ + FEATURES.md), and emit two grounded artifacts: docs/BENCHMARK_FEATURE_COVERAGE.md (a ✅/🟡/⬜ coverage catalog with options/defaults + a recommended-default-on + optional-to-surface section) and ROADMAP_V4.md (a gap roadmap whose phases each close a ⬜/🟡 row, starting with Phase 27 = default-enable benchmark --monitoring). Also adds knowledge/benchmark_feature_coverage.md (a runtime-readable pointer) + a key_docs.yaml observability entry. Fan-out doc clusters -> extract -> cross-ref -> 3-lens adversarial skeptic -> synthesize -> verify (1:1 phase<->gap). Docs-only: writes/commits on branch docs/benchmark-feature-catalog off main (NEVER main; no push, no merge). Re-runnable when benchmark docs change.',
  whenToUse: 'Build/refresh the benchmark feature-coverage catalog and the next gap-roadmap (v4) for the llm-d-benchmarking-agent. Docs-only; produces a reviewable branch.',
  phases: [
    { title: 'Prep', detail: 'enumerate all benchmark .md docs, cluster them, snapshot our coverage, create the docs worktree off main' },
    { title: 'Extract', detail: 'one agent per doc cluster: deep-read the docs, emit a structured feature inventory (feature, source, options, optional?, default)' },
    { title: 'CrossRef', detail: 'one agent: dedupe the full inventory, assign a grounded ✅/🟡/⬜ verdict per feature (every ✅ cites a tool+file; every ⬜ cites the benchmark doc)' },
    { title: 'Skeptic', detail: '3 parallel lenses (no-over-claim / no-under-claim / completeness) audit the verdicts; one bounded fix loop' },
    { title: 'Synthesize', detail: 'one agent: write the coverage catalog, the knowledge pointer, the key_docs entry, and ROADMAP_V4.md; commit on the docs branch' },
    { title: 'Verify', detail: 'one agent: sample evidence, confirm 1:1 phase<->gap map + the --monitoring regression guard; one bounded fix loop' },
  ],
}

// ----------------------------------------------------------------------------
// Constants (verified against on-disk state)
// ----------------------------------------------------------------------------
const MONO  = '/home/tal/kind-quickstart-guide'          // main checkout: has the POPULATED read-only sibling repos
const BENCH = MONO + '/llm-d-benchmark'                   // benchmark repo (READ-ONLY source) — read docs from HERE (worktree siblings are EMPTY)
const PROJ  = 'llm-d-benchmarking-agent-project'
const SRC   = MONO + '/' + PROJ                           // our project as it exists on main (POPULATED) — read coverage truth from HERE
const DOCBR = 'docs/benchmark-feature-catalog'            // docs integration branch (NEVER main) — branched off main
const HOME  = '/home/tal/kqg-catalog-home'                // docs worktree — CREATED in Prep
const PDIR  = HOME + '/' + PROJ                           // worktree project dir — WRITE the docs HERE
const BUILD = PDIR + '/workspace/catalog_build'           // scratch handoff dir (workspace/ is gitignored) — inventory_*.json + verdicts.json

// Output artifact paths (repo-relative to PROJ)
const CATALOG  = 'docs/BENCHMARK_FEATURE_COVERAGE.md'
const POINTER  = 'knowledge/benchmark_feature_coverage.md'
const KEYDOCS  = 'knowledge/key_docs.yaml'
const ROADMAP4 = 'ROADMAP_V4.md'

const TRAILER = 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'

// ----------------------------------------------------------------------------
// The 8 recommended feature areas (the catalog's section spine). Prep assigns
// every found .md to one of these (or adds an area), so no doc is dropped.
// ----------------------------------------------------------------------------
const AREAS = [
  'A. Lifecycle (standup / run / teardown / smoketest / CLI flags)',
  'B. Specifications & scenario catalog',
  'C. Harnesses & workload profiles',
  'D. Design of Experiments (DOE)',
  'E. Analysis & reporting (Benchmark Report v0.2, goodput, Pareto, plots)',
  'F. Observability & metrics collection (PRIORITY)',
  'G. Workload Variant Autoscaler (WVA) / autoscaling',
  'H. Reproducibility, resource requirements & capacity',
]

// ----------------------------------------------------------------------------
// The pre-verified headline finding. Carried into the F-cluster extract AND the
// cross-ref so it can never be lost. Agents RE-CONFIRM it by reading the files.
// ----------------------------------------------------------------------------
const MONITORING_FINDING = [
  'KNOWN HIGH-PRIORITY FINDING (pre-verified ground truth — RE-CONFIRM by reading the cited files, do not just trust this):',
  'The benchmark metrics collection (vLLM/EPP Prometheus + DCGM GPU/accelerator + cAdvisor system metrics, written into the Benchmark Report under results.observability) is OFF BY DEFAULT: env LLMDBENCH_VLLM_COMMON_METRICS_SCRAPE_ENABLED defaults to false (' + BENCH + '/docs/metrics_collection.md, line ~45). It is turned ON by "llmdbenchmark standup/run --monitoring" (or scenario metricsScrapeEnabled: true) — see ' + BENCH + '/docs/observability.md (lines 5-34).',
  'OUR AGENT NEVER PASSES --monitoring: ' + SRC + '/app/tools/execute.py (build_argv emits no such flag); ' + SRC + '/app/tools/schemas.py (ExecuteInput.flags has no "monitoring" key); ' + SRC + '/security/allowlist.yaml (does not permit --monitoring/--no-monitoring under standup/run/experiment).',
  'YET we already PARSE results.observability (' + SRC + '/knowledge/standard_metrics.yaml + ' + SRC + '/app/validation/report.py — shipped in Phase 25). So the CONSUMER exists but the PRODUCER-ACTIVATION was never built => results.observability is perpetually EMPTY in practice.',
  'THEREFORE this feature is PARTIAL (🟡), default_on_candidate=true. It is the headline row of catalog area F and becomes ROADMAP_V4 Phase 27 (default-enable --monitoring + surface results.observability).',
].join('\n')

// ----------------------------------------------------------------------------
// Schemas (additionalProperties:false everywhere)
// ----------------------------------------------------------------------------
const CLUSTER_SCHEMA = { type:'object', additionalProperties:false, properties:{
  name:{type:'string'}, area:{type:'string'}, files:{type:'array', items:{type:'string'}} },
  required:['name','area','files'] }

const PREP_SCHEMA = { type:'object', additionalProperties:false, properties:{
  branch:{type:'string'}, worktree:{type:'string'}, baseOk:{type:'boolean'},
  docCount:{type:'integer'},
  clusters:{type:'array', items:CLUSTER_SCHEMA},
  toolCount:{type:'integer'}, knowledgeFiles:{type:'array', items:{type:'string'}},
  notes:{type:'string'} },
  required:['branch','worktree','baseOk','clusters','docCount'] }

const INVENTORY_SCHEMA = { type:'object', additionalProperties:false, properties:{
  cluster:{type:'string'}, inventoryPath:{type:'string'}, featureCount:{type:'integer'},
  docsRead:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['cluster','inventoryPath','featureCount'] }

const CROSSREF_SCHEMA = { type:'object', additionalProperties:false, properties:{
  verdictsPath:{type:'string'}, featureCount:{type:'integer'},
  covered:{type:'integer'}, partial:{type:'integer'}, missing:{type:'integer'},
  defaultOnCandidates:{type:'array', items:{type:'string'}},
  gaps:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['verdictsPath','featureCount','covered','partial','missing'] }

const SKEPTIC_SCHEMA = { type:'object', additionalProperties:false, properties:{
  lens:{type:'string'}, acceptable:{type:'boolean'},
  blocking:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['lens','acceptable','blocking'] }

const SYNTH_SCHEMA = { type:'object', additionalProperties:false, properties:{
  catalogPath:{type:'string'}, pointerPath:{type:'string'}, roadmapPath:{type:'string'},
  keyDocsUpdated:{type:'boolean'},
  featureCount:{type:'integer'}, covered:{type:'integer'}, partial:{type:'integer'}, missing:{type:'integer'},
  v4Phases:{type:'array', items:{type:'integer'}},
  committed:{type:'boolean'}, commit:{type:'string'}, notes:{type:'string'} },
  required:['catalogPath','roadmapPath','featureCount','committed'] }

const VERIFY_SCHEMA = { type:'object', additionalProperties:false, properties:{
  ok:{type:'boolean'}, phaseGapMap:{type:'string'},
  sampledRowsOk:{type:'boolean'}, monitoringClaimValid:{type:'boolean'},
  allDocsCovered:{type:'boolean'},
  blocking:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['ok','phaseGapMap','blocking'] }

// ----------------------------------------------------------------------------
// Prompts
// ----------------------------------------------------------------------------
const PREP_PROMPT = `Prepare a FRESH docs worktree for the benchmark feature-coverage catalog + gap-roadmap effort, enumerate the benchmark docs to mine, cluster them, and snapshot our coverage. Use Bash. main is NEVER touched. Do EXACTLY:

1. Sanity: note (do NOT switch) the main branch:  git -C ${MONO} rev-parse --abbrev-ref HEAD . Run  git -C ${MONO} status --porcelain  (if unrelated uncommitted work exists, leave it and mention it in notes).

2. Create (or reuse) the docs branch ${DOCBR} + worktree ${HOME} off main:
   - If ${HOME} already exists as a worktree ( git -C ${MONO} worktree list ): put it on ${DOCBR} ( git -C ${HOME} checkout ${DOCBR} ) and report reuse.
   - Else if branch ${DOCBR} exists ( git -C ${MONO} rev-parse --verify ${DOCBR} ):  git -C ${MONO} worktree add ${HOME} ${DOCBR}
   - Else:  git -C ${MONO} worktree add -b ${DOCBR} ${HOME} main
   Verify:  git -C ${HOME} rev-parse --abbrev-ref HEAD  == ${DOCBR}  -> set baseOk accordingly. Make the scratch dir:  mkdir -p ${BUILD}

3. ENUMERATE every benchmark doc to mine (read from the POPULATED main checkout, NOT the worktree — worktree siblings are EMPTY):
   find ${BENCH} -name '*.md' -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/site-packages/*' | sort
   Record the count as docCount. (Include READMEs in subdirs: docs/, config/, workload/, llmdbenchmark/*/, skills/, e2e/, experimental/, tutorials/ — whatever exists. Skip clearly non-API docs like CODE_OF_CONDUCT/LICENSE/CHANGELOG only if present; mention any skip in notes.)

4. CLUSTER the docs into these areas (assign EVERY found doc to exactly ONE cluster so none is dropped; add an extra cluster only if a doc fits none). Use repo-relative paths like "docs/observability.md":
${AREAS.map((a) => '   - ' + a).join('\n')}
   The Observability cluster (area F) is the PRIORITY cluster and MUST include docs/observability.md and docs/metrics_collection.md if they exist.

5. SNAPSHOT our coverage truth (so downstream agents are grounded, not guessing):
   - Tool count + names:  grep -nE 'name="|"name":' ${SRC}/app/tools/registry.py | head -60   (report toolCount = number of registered tools; the authoritative list is registry.py).
   - Knowledge files:  ls -1 ${SRC}/knowledge/   (report as knowledgeFiles).
   - Skim ${SRC}/FEATURES.md and ${SRC}/PROPOSAL_ROADMAP.md headings to know what is already covered (no need to return their full text).

Return PREP_SCHEMA: {branch:"${DOCBR}", worktree:"${HOME}", baseOk, docCount, clusters:[{name, area, files:[...]}], toolCount, knowledgeFiles:[...], notes:"git base note + any skipped docs + anything notable"}.`

function extractPrompt(cluster) {
  const isF = /observ|metric/i.test(cluster.area) || /observ|metric/i.test(cluster.name)
  return `You are ONE of several parallel extractor agents. Deep-read a CLUSTER of llm-d-benchmark documentation files and emit a STRUCTURED feature inventory. Focus on DOC files; only open code if a doc is ambiguous and you must confirm a default/flag. Stay inside your assigned files.

== Your cluster ==
name: ${cluster.name}
area: ${cluster.area}
files (repo-relative to ${BENCH}): ${JSON.stringify(cluster.files)}

== How to read ==
- Read each file from ${BENCH}/<file> (the POPULATED main checkout). Also skim ${BENCH}/README.md for system context if helpful.
- For EACH distinct feature / API / capability / option the docs describe, capture: the user-facing knobs (CLI flags, env vars, scenario/config keys, profile/scenario names), whether it is an OPTIONAL add-on vs CORE lifecycle, and its DEFAULT state (on/off/n-a) AS STATED by the doc (quote the doc — do not infer).
- Be exhaustive but DEDUPE within your cluster. Prefer many small precise features over a few vague ones.
${isF ? '\n== PRIORITY: this is the observability/metrics cluster ==\n' + MONITORING_FINDING + '\nMake sure the "Benchmark metrics collection (--monitoring)" feature is a row, with its options (--monitoring/--no-monitoring, metricsScrapeEnabled, LLMDBENCH_VLLM_COMMON_METRICS_SCRAPE_ENABLED, METRICS_COLLECTION_INTERVAL=15, _METRICS_PORT=8200), default_state "off", and the rich metric families (vLLM cache/queue/memory/NIXL, EPP pool gauges/histograms/P-D, DCGM GPU util/power/fb, cAdvisor cpu/mem/net, replica status, pod startup times, EPP log-derived). Also capture: distributed tracing (OTel config block), the cluster-level Prometheus/Grafana pointers, and the "real-time streaming NOT yet implemented" + "custom queries NOT yet implemented" notes.\n' : ''}
== Output: WRITE a JSON file, do NOT return the array inline ==
mkdir -p ${BUILD}
Write ${BUILD}/inventory_${cluster.name}.json containing a JSON array of feature objects, each EXACTLY:
  {"area": "<one of the 8 area labels, matching your cluster's area>",
   "feature": "<short feature name>",
   "source_docs": ["docs/observability.md#anchor-or-section", ...],   // repo-relative + section/line anchor
   "options": ["--flag", "ENV_VAR=default", "scenario.key", ...],
   "optional": true|false,
   "default_state": "on"|"off"|"n/a",
   "summary": "<1-2 plain-language lines>"}
Use a heredoc or your file-writer; ensure it is VALID JSON (the next stage parses it). Then return INVENTORY_SCHEMA: {cluster:"${cluster.name}", inventoryPath:"${BUILD}/inventory_${cluster.name}.json", featureCount:<n>, docsRead:[...the files you actually read...], notes:"anything notable / empty files / overlaps with other clusters"}.`
}

function crossRefPrompt(inventoryPaths, fixFeedback) {
  return `You assign a grounded COVERAGE VERDICT to every benchmark feature, by cross-referencing the extracted inventory against THIS agent project's ACTUAL coverage. Be precise and evidence-based. Use Bash/Read/Grep.

== Inputs ==
- Feature inventories (JSON arrays) written by the extractors: ${JSON.stringify(inventoryPaths)}
  Read and concatenate them all. DEDUPE across clusters (e.g. monitoring may appear under both observability.md and metrics_collection.md -> ONE feature; merge their source_docs/options).
- Our coverage truth (READ these — do NOT guess):
  - Tool registry (authoritative): ${SRC}/app/tools/registry.py  (the registered tools + descriptions)
  - ${SRC}/FEATURES.md and ${SRC}/PROPOSAL_ROADMAP.md (evidence-backed inventory + proposal coverage)
  - ${SRC}/knowledge/ (the agent's knowledge files) and ${SRC}/app/validation/ , ${SRC}/app/orchestrator/ , ${SRC}/app/tools/ as needed to confirm.
  You MAY grep our repo, e.g.:  grep -rn "<keyword>" ${SRC}/app ${SRC}/knowledge ${SRC}/security

== Pre-verified headline (RE-CONFIRM, then encode) ==
${MONITORING_FINDING}
${fixFeedback ? '\n== SKEPTIC FEEDBACK FROM THE PRIOR ROUND — you MUST resolve every item ==\n' + fixFeedback + '\n' : ''}
== Verdict rules (NON-NEGOTIABLE) ==
- coverage = "covered" (✅) | "partial" (🟡) | "missing" (⬜).
- Every "covered" MUST cite >=1 concrete EVIDENCE token: an agent tool name AND a file path (and line if easy) in OUR repo, e.g. "analyze_results -> app/tools/analyze.py; knowledge/analysis.md". No real citation => downgrade to partial or missing.
- Every "missing" MUST cite the benchmark doc (gap_source) that describes the feature, e.g. "docs/workload-variant-autoscaler.md".
- "partial" cites BOTH what we have (evidence) AND what's missing (gap_source) — monitoring is the canonical partial.
- Do NOT invent phantom gaps: things already shipped are COVERED — DOE generation (generate_doe_experiment), capacity pre-flight (check_capacity), checkpoint/resume (orchestrator/checkpoint.py), KV-cache/schedule-delay/GPU-util PARSING (standard_metrics.yaml, Phase 25), well-lit-path advisor (welllit_path_advisor.yaml), log streaming, health-check (readiness.py), multi-harness compare, history. Verify each before calling it missing.
- default_on_candidate=true ONLY for features that are useful + safe to enable by default and currently off/unused (monitoring is the flagship).
- surface_to_user=true for optional features worth making users aware of (even if covered) — e.g. --no-monitoring for clusters without Prometheus CRDs, WVA scenarios, tracing, cloud output sinks.

== Output: WRITE the verdicts file ==
Write ${BUILD}/verdicts.json = a JSON array, one object per DEDUPED feature, EXACTLY:
  {"area","feature","source_docs":[...],"options":[...],"optional":bool,"default_state":"on|off|n/a",
   "coverage":"covered|partial|missing","evidence":[...],"gap_source":[...],
   "default_on_candidate":bool,"surface_to_user":bool,"rationale":"<1-2 lines>"}
Valid JSON. Then return CROSSREF_SCHEMA: {verdictsPath:"${BUILD}/verdicts.json", featureCount, covered, partial, missing, defaultOnCandidates:[feature names], gaps:[feature names that are partial|missing], notes}.`
}

function skepticPrompt(lens) {
  const head = `Adversarially audit the coverage verdicts in ${BUILD}/verdicts.json (and the inventories ${BUILD}/inventory_*.json) for the benchmark feature-coverage catalog. Be skeptical; default to acceptable=false if unsure. You are ONE of three independent lenses. Read-only (grep/read allowed). Our repo to check against: ${SRC} . Benchmark docs: ${BENCH} .

`
  const lenses = {
    'no-over-claim':
      head + `LENS = NO-OVER-CLAIM. For each verdict marked "covered" (✅), OPEN the cited evidence (tool in ${SRC}/app/tools/registry.py + the cited file) and confirm the feature is GENUINELY and fully covered — not an adjacent/partial capability dressed up as full coverage. Any ✅ whose cited file does not actually back the claim, or that is really partial, is BLOCKING (name the feature + why it should be 🟡/⬜). Pay special attention that "monitoring" is NOT marked covered (it must be partial).`,
    'no-under-claim':
      head + `LENS = NO-UNDER-CLAIM. For each verdict marked "missing" (⬜) or "partial" (🟡), GREP our repo to check we don't ALREADY cover it (avoid phantom gaps). Known-shipped (must NOT be ⬜): DOE generation, capacity pre-flight, checkpoint/resume, KV-cache/schedule-delay/GPU-util parsing, well-lit-path advisor, log streaming, endpoint health-check, multi-harness compare, result history. Any feature wrongly marked missing/partial that we actually cover is BLOCKING (name it + cite the tool/file that covers it).`,
    'completeness':
      head + `LENS = COMPLETENESS. (a) Confirm EVERY benchmark .md in the Prep cluster map produced >=1 inventory row (no doc silently dropped) — compare the inventories to the cluster files. (b) Confirm all 8 areas (A-H) are represented in verdicts.json. (c) Confirm the monitoring feature is present with default_state "off", default_on_candidate=true, coverage "partial". List any dropped doc, empty area, or missing monitoring detail as BLOCKING.`,
  }
  return lenses[lens]
}

function synthesizePrompt(skepticNotes, fixFeedback) {
  return `You write the final documentation artifacts from the verified data and COMMIT them on the docs branch. Use Bash/Read/Write. Write ONLY inside the worktree ${PDIR}. main is NEVER touched.

== Inputs ==
- Verdicts: ${BUILD}/verdicts.json  (the deduped per-feature coverage verdicts — your source of truth for rows + gaps).
- Inventories: ${BUILD}/inventory_*.json (for any extra option detail).
- Conventions to MATCH: read ${SRC}/ROADMAP.md (use the v3 phase format: "## Phase N — Title — STATUS", prose bullets, a proposal/catalog ref line, suite-count line when integrated; status legend "TODO · IN-PROGRESS · DONE · DEFERRED"). Phases go up to 26 today => v4 starts at 27. Skim ${SRC}/docs/README.md and an existing docs/*.md for house style.
- Skeptic notes to honor: ${skepticNotes}
${fixFeedback ? '\n== VERIFY FEEDBACK FROM THE PRIOR ROUND — resolve every blocking item ==\n' + fixFeedback + '\n' : ''}
== Write artifact 1: ${PDIR}/${CATALOG} ==
A coverage catalog with: a title + generation note; a LEGEND (✅ covered / 🟡 partial / ⬜ missing; Optional? Y/N; Default on/off/n-a); a coverage SUMMARY TABLE (one row per area A-H with counts ✅/🟡/⬜); then ONE table per area with columns: Feature | Source doc | Options/knobs | Optional? | Default | Coverage | Evidence/notes. Render coverage as the emoji. Every ✅ note cites the tool+file; every ⬜ note cites the benchmark doc. Then two sections: "## Recommended default-on features" (monitoring FIRST, with the exact knobs + why) and "## Optional features to surface to users". Area F (observability) is the priority section — render the monitoring row fully (the --monitoring/metricsScrapeEnabled knobs, default off, 🟡 partial: consumer exists via knowledge/standard_metrics.yaml + app/validation/report.py, activation missing in execute.py/schemas.py/allowlist.yaml; "Fix -> ROADMAP_V4 Phase 27").

== Write artifact 2: ${PDIR}/${POINTER} (a knowledge file — auto-discovered by read_knowledge) ==
A concise pointer/summary the runtime agent reads: 1-line purpose, the legend, the coverage SUMMARY TABLE, the recommended-default-on list, the optional-to-surface list, and a final line: "Full catalog: docs/BENCHMARK_FEATURE_COVERAGE.md". Keep it compact (it is read on demand). Do NOT duplicate the full per-feature tables (avoid drift) — point to the canonical catalog.

== Write artifact 3: append to ${PDIR}/${KEYDOCS} (DATA only) ==
Add pointer entries (under the existing docs: list) so the agent grounds in the real monitoring procedure, e.g. task: observability -> llm-d-benchmark/docs/observability.md and llm-d-benchmark/docs/metrics_collection.md with a short why. Match the existing YAML shape (path/task/why). Preserve all existing entries.

== Write artifact 4: ${PDIR}/${ROADMAP4} ==
A v4 gap-roadmap matching the v3 ROADMAP convention. Header: living-doc note (derived from docs/BENCHMARK_FEATURE_COVERAGE.md; integration branch feature/roadmap-v4 off main, never main during the effort; continues numbering from 26). Status legend. Then one "## Phase N — Title — TODO" per ⬜/🟡 gap row (numbering CONTIGUOUS from 27), each with a proposal/catalog-ref line and a GOAL / BUILD / ACCEPTANCE / HERMETIC-TEST skeleton (so a future roadmap-v4-autopilot.js can consume it).
- Phase 27 is FIXED: "Default-enable benchmark --monitoring + surface results.observability" — BUILD: add a monitoring flag to ExecuteInput.flags (app/tools/schemas.py) + emit --monitoring/--no-monitoring in build_argv (app/tools/execute.py) for standup/run/experiment; widen security/allowlist.yaml (DATA only) to permit it; default ON with a knowledge-driven opt-out for clusters lacking Prometheus CRDs; surface the parsed results.observability metrics in the report/analysis summary + knowledge/observability.md + results_interpretation.md. ACCEPTANCE: a run/standup emits --monitoring (allowlist-approved); when scraping ran, results.observability KV-cache/GPU/queue metrics appear in the summary; --no-monitoring opt-out works; decision logic stays in knowledge, not Python.
- The remaining phases come 1:1 from the OTHER ⬜/🟡 rows in verdicts.json (drop nothing, invent nothing). Each phase maps to exactly one gap row.

== Commit (do NOT push, do NOT merge) ==
cd ${HOME}
git -C ${HOME} add ${PROJ}/${CATALOG} ${PROJ}/${POINTER} ${PROJ}/${KEYDOCS} ${PROJ}/${ROADMAP4}
git -C ${HOME} commit -m "docs: benchmark feature-coverage catalog + ROADMAP_V4 gap roadmap" -m "${TRAILER}"
Capture the short hash:  git -C ${HOME} rev-parse --short HEAD

Return SYNTH_SCHEMA: {catalogPath:"${CATALOG}", pointerPath:"${POINTER}", roadmapPath:"${ROADMAP4}", keyDocsUpdated:true, featureCount, covered, partial, missing, v4Phases:[27,...], committed:true, commit:"<hash>", notes:"the v4 phase list + anything notable"}.`
}

const VERIFY_PROMPT = `Adversarially VERIFY the committed documentation artifacts on branch ${DOCBR} (worktree ${HOME}). Read-only (grep/read allowed). Our repo: ${SRC} . Benchmark docs: ${BENCH} . Files: ${PDIR}/${CATALOG} , ${PDIR}/${POINTER} , ${PDIR}/${ROADMAP4} , ${PDIR}/${KEYDOCS} . Verdicts data: ${BUILD}/verdicts.json .

Checks (ALL must hold for ok=true):
1. NO OVER-CLAIM: sample ~8 random "✅ covered" rows in the catalog; for each, confirm the cited file exists ( ls / read ) and the cited tool appears in ${SRC}/app/tools/registry.py ( grep -n ). Any miss => blocking.
2. NO PHANTOM GAPS: for each "⬜ missing" row, confirm the cited benchmark doc exists under ${BENCH} AND grep ${SRC}/app + ${SRC}/knowledge to confirm we genuinely don't cover it. Any false gap => blocking.
3. PHASE<->GAP 1:1: every ROADMAP_V4.md phase maps to exactly one ⬜/🟡 catalog row and vice-versa (no orphan phase, no uncovered gap). Phase 27 == the --monitoring default-on row. Numbering CONTIGUOUS from 27. Set phaseGapMap = "1:1" or "mismatch".
4. DOC COMPLETENESS: every benchmark .md the extractors read produced >=1 catalog row; all 8 areas (A-H) appear in the summary table with counts. Set allDocsCovered.
5. MARKDOWN SANITY: catalog tables have consistent column counts; legend symbols (✅/🟡/⬜) used consistently; no obviously broken intra-repo links.
6. --monitoring REGRESSION GUARD: re-grep ${SRC}/app/tools/execute.py , ${SRC}/app/tools/schemas.py , ${SRC}/security/allowlist.yaml to confirm the catalog's claim "we do NOT currently pass --monitoring" is STILL TRUE at generation time (so the doc ships no false claim). Set monitoringClaimValid. If --monitoring is in fact already wired, that is blocking (the catalog/roadmap must be corrected).

Return VERIFY_SCHEMA: {ok, phaseGapMap, sampledRowsOk, monitoringClaimValid, allDocsCovered, blocking:[...specific items...], notes}. ok=true ONLY if every check passes and phaseGapMap=="1:1".`

// ----------------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------------
phase('Prep')
const prep = await agent(PREP_PROMPT, { label: 'prep', phase: 'Prep', agentType: 'general-purpose', schema: PREP_SCHEMA })
if (!prep || !prep.baseOk || !prep.clusters || !prep.clusters.length) {
  log('Prep failed (no worktree / no clusters). Aborting. Notes: ' + (prep && prep.notes))
  return { ok: false, stage: 'prep', prep }
}
log('Prep complete. Branch ' + prep.branch + ' @ ' + prep.worktree + '. ' + prep.docCount + ' benchmark docs in ' + prep.clusters.length + ' clusters; ' + (prep.toolCount || '?') + ' tools. ' + (prep.notes || ''))

// --- Extract: one agent per cluster (parallel barrier) -----------------------
phase('Extract')
log('Extracting features from ' + prep.clusters.length + ' doc clusters in parallel')
const inventories = (await parallel(prep.clusters.map(c => () =>
  agent(extractPrompt(c), { label: 'extract:' + c.name, phase: 'Extract', agentType: 'general-purpose', schema: INVENTORY_SCHEMA })
))).filter(Boolean)
const inventoryPaths = inventories.map(i => i.inventoryPath)
const totalFeatures = inventories.reduce((n, i) => n + (i.featureCount || 0), 0)
log('Extracted ~' + totalFeatures + ' features across ' + inventories.length + ' clusters')
if (!inventoryPaths.length) { log('No inventories produced. Aborting.'); return { ok: false, stage: 'extract', prep } }

// --- CrossRef + 3-lens Skeptic (parallel barrier) + one bounded fix loop -----
phase('CrossRef')
let crossref = await agent(crossRefPrompt(inventoryPaths, null), { label: 'crossref', phase: 'CrossRef', agentType: 'general-purpose', schema: CROSSREF_SCHEMA })
if (!crossref) { log('CrossRef failed. Aborting.'); return { ok: false, stage: 'crossref', prep } }
log('CrossRef: ' + crossref.featureCount + ' features — ✅ ' + crossref.covered + ' / 🟡 ' + crossref.partial + ' / ⬜ ' + crossref.missing)

phase('Skeptic')
const lenses = ['no-over-claim', 'no-under-claim', 'completeness']
let skeptics = (await parallel(lenses.map(L => () =>
  agent(skepticPrompt(L), { label: 'skeptic:' + L, phase: 'Skeptic', agentType: 'general-purpose', schema: SKEPTIC_SCHEMA })
))).filter(Boolean)
let blocking = skeptics.filter(s => !s.acceptable || (s.blocking && s.blocking.length))
if (blocking.length) {
  const feedback = JSON.stringify(blocking.map(s => ({ lens: s.lens, blocking: s.blocking }))).slice(0, 6000)
  log('Skeptic raised ' + blocking.length + ' blocking lens(es) — one bounded CrossRef fix pass')
  const fixed = await agent(crossRefPrompt(inventoryPaths, feedback), { label: 'crossref:fix', phase: 'Skeptic', agentType: 'general-purpose', schema: CROSSREF_SCHEMA })
  if (fixed) crossref = fixed
  skeptics = (await parallel(lenses.map(L => () =>
    agent(skepticPrompt(L), { label: 'reskeptic:' + L, phase: 'Skeptic', agentType: 'general-purpose', schema: SKEPTIC_SCHEMA })
  ))).filter(Boolean)
  blocking = skeptics.filter(s => !s.acceptable || (s.blocking && s.blocking.length))
}
const skepticNotes = (JSON.stringify(skeptics.map(s => ({ lens: s.lens, acceptable: s.acceptable, blocking: s.blocking, notes: s.notes }))) || '').slice(0, 4000)
log('Skeptic settled — ' + (blocking.length ? blocking.length + ' residual concern(s) (passed to Synthesize)' : 'all lenses clean'))

// --- Synthesize the docs + commit, then Verify (+ one bounded fix loop) -------
phase('Synthesize')
let synth = await agent(synthesizePrompt(skepticNotes, null), { label: 'synthesize', phase: 'Synthesize', agentType: 'general-purpose', schema: SYNTH_SCHEMA })
if (!synth || !synth.committed) {
  log('Synthesize did not commit. Notes: ' + (synth && synth.notes))
  return { ok: false, stage: 'synthesize', prep, crossref, synth }
}
log('Synthesized + committed ' + (synth.commit || '') + ': catalog + pointer + ROADMAP_V4 (phases ' + JSON.stringify(synth.v4Phases) + ')')

phase('Verify')
let verify = await agent(VERIFY_PROMPT, { label: 'verify', phase: 'Verify', agentType: 'general-purpose', schema: VERIFY_SCHEMA })
if (verify && !verify.ok && verify.blocking && verify.blocking.length) {
  const vfeedback = JSON.stringify({ blocking: verify.blocking, phaseGapMap: verify.phaseGapMap, notes: verify.notes }).slice(0, 6000)
  log('Verify found ' + verify.blocking.length + ' issue(s) — one bounded Synthesize fix pass')
  const synth2 = await agent(synthesizePrompt(skepticNotes, vfeedback), { label: 'synthesize:fix', phase: 'Verify', agentType: 'general-purpose', schema: SYNTH_SCHEMA })
  if (synth2 && synth2.committed) synth = synth2
  verify = await agent(VERIFY_PROMPT, { label: 'reverify', phase: 'Verify', agentType: 'general-purpose', schema: VERIFY_SCHEMA })
}

const summary = {
  ok: !!(synth && synth.committed && verify && verify.ok && verify.phaseGapMap === '1:1'),
  branch: prep.branch,
  worktree: prep.worktree,
  commit: synth && synth.commit,
  artifacts: { catalog: PROJ + '/' + CATALOG, pointer: PROJ + '/' + POINTER, keyDocs: PROJ + '/' + KEYDOCS, roadmap: PROJ + '/' + ROADMAP4 },
  featureCounts: { total: synth && synth.featureCount, covered: synth && synth.covered, partial: synth && synth.partial, missing: synth && synth.missing },
  v4Phases: (synth && synth.v4Phases) || [],
  defaultOnCandidates: (crossref && crossref.defaultOnCandidates) || [],
  verify: verify && { ok: verify.ok, phaseGapMap: verify.phaseGapMap, monitoringClaimValid: verify.monitoringClaimValid, allDocsCovered: verify.allDocsCovered, blocking: verify.blocking },
  note: 'Docs committed on ' + prep.branch + ' (off main; NOT pushed, NOT merged). Review then merge when ready. Phase 27 (default-enable --monitoring) and the rest of ROADMAP_V4 are authored only — execute later.',
}
log('benchmark-catalog-gap finished. ' + (summary.ok ? 'OK' : 'NEEDS REVIEW') + ' — features: total ' + (summary.featureCounts.total) + ' / ✅ ' + summary.featureCounts.covered + ' / 🟡 ' + summary.featureCounts.partial + ' / ⬜ ' + summary.featureCounts.missing + '; v4 phases ' + JSON.stringify(summary.v4Phases) + '; verify ' + (verify && verify.phaseGapMap))
return summary
