export const meta = {
  name: 'repo-docs-index',
  description: 'Read every .md in both upstream repos (llm-d + llm-d-benchmark), tier each for usefulness to this agent, and emit docs/USEFUL_REPO_DOCS.md (annotated catalog, no file dropped) + knowledge/useful_repo_docs.md (runtime pointer). Fan-out → read+tier → compose → assemble → verify. Docs-only on branch docs/repo-docs-index off main (no push/merge). Re-runnable.',
  whenToUse: 'Build/refresh the curated index of which llm-d + llm-d-benchmark documentation files are useful when working on the llm-d-benchmarking-agent (and why). Docs-only; produces a reviewable branch.',
  phases: [
    { title: 'Prep', detail: 'enumerate every .md in both upstream repos, cluster them by repo+topic (no file dropped), snapshot our project context, create the docs worktree off main' },
    { title: 'Read', detail: 'one agent per doc cluster: read each file, emit a per-file entry (title, summary, covers, relevance tier, why-useful-for-us, reference points, external links)' },
    { title: 'Compose', detail: 'parallel section-writers: header+start-here(+pointer) / llm-d-benchmark tables / llm-d tables / reference-points+external-links / skip-appendix -> fragment files (no single agent writes the whole doc)' },
    { title: 'Assemble', detail: 'one cheap agent: concatenate the fragments into docs/USEFUL_REPO_DOCS.md, confirm the pointer, and commit on the docs branch' },
    { title: 'Verify', detail: 'one agent: confirm every enumerated .md is accounted for, named examples present, all cited paths exist, tiers calibrated; one bounded fix loop' },
  ],
}

// ----------------------------------------------------------------------------
// Constants (verified against on-disk state: 57 .md in llm-d-benchmark, 132 in llm-d)
// ----------------------------------------------------------------------------
const MONO  = '/home/tal/llm-d-benchmarking-agent'      // main checkout: has the POPULATED read-only sibling repos
const BENCH = MONO + '/llm-d-benchmark'               // benchmark repo (READ-ONLY source) — read docs from HERE (worktree siblings are EMPTY)
const GUIDE = MONO + '/llm-d'                         // llm-d guide repo (READ-ONLY source) — read docs from HERE
const PROJ  = 'llm-d-benchmarking-agent-project'
const SRC   = MONO + '/' + PROJ                       // our project as it exists on main (POPULATED) — read project context from HERE
const DOCBR = 'docs/repo-docs-index'                  // docs integration branch (NEVER main) — branched off main
const HOME  = '/home/tal/kqg-docs-index'             // docs worktree — CREATED in Prep
const PDIR  = HOME + '/' + PROJ                       // worktree project dir — WRITE the docs HERE
const BUILD = PDIR + '/workspace/docs_index_build'    // scratch handoff dir (workspace/ is gitignored) — index_*.json

// Output artifact paths (repo-relative to PROJ)
const INDEX   = 'docs/USEFUL_REPO_DOCS.md'
const POINTER = 'knowledge/useful_repo_docs.md'

const TRAILER = 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>'

// ----------------------------------------------------------------------------
// What THIS project is — so every reader judges "useful for US" consistently,
// not "useful in the abstract".
// ----------------------------------------------------------------------------
const PROJECT_CONTEXT = [
  'THE PROJECT we are judging usefulness FOR: "llm-d-benchmarking-agent" — a local chat-based assistant that helps NON-EXPERTS run llm-d-benchmark.',
  'A user describes a use case ("benchmark a chat app with 500 concurrent users"); the agent interviews them, checks preconditions, deploys an llm-d stack if needed (kind or real cluster), drives the `llmdbenchmark` CLI (standup/run/teardown/smoketest/experiment) on their behalf, parses the Benchmark Report v0.2, and explains the results.',
  'It is FastAPI backend + chat UI; "thin code, thick agent" (judgment lives in knowledge/*.md|yaml, not Python if/elif); deny-by-default allowlist; reads upstream repo truth at runtime and shells out to the CLI.',
  'So a doc is HIGHLY useful to us if it describes: the benchmark CLI lifecycle & flags, the deploy GUIDES our agent runs (optimized-baseline, kind/quickstart, prereqs), workload/harness/scenario selection & config, the analysis/report schema, observability/metrics, capacity/resources, or the llm-d CRDs/APIs we deploy and target. It is LOW/SKIP if it is deep component-internal architecture, a future design proposal, or governance/meta (code-of-conduct, license, contributing).',
].join('\n')

// ----------------------------------------------------------------------------
// Shared relevance rubric — every reader applies the SAME bar so tiers are
// comparable across clusters.
// ----------------------------------------------------------------------------
const RELEVANCE_RUBRIC = [
  'RELEVANCE TIERS (apply this exact bar):',
  '- "high"   ⭐⭐⭐ ESSENTIAL: directly drives what our agent does / something we must read, cover, or use. Examples: the benchmark README, docs/{lifecycle,standup,run,quickstart,doe,analysis,benchmark_report,metrics_collection,observability,reproducibility,resource_requirements}.md, workload/harness/scenario docs, skills/convert-guide references (how guides map to benchmark configs), llm-d guides/optimized-baseline + guides/prereq + getting-started/quickstart, helpers/benchmark.md.',
  '- "medium" ⭐⭐ USEFUL CONTEXT: needed when implementing a SPECIFIC feature. Examples: llm-d docs/api-reference/* (CRDs: InferencePool, InferenceObjective, EPP config), docs/resources/observability/* (Prometheus/PromQL/Grafana/tracing), well-lit-paths, infra-providers (cloud standup), the per-module llmdbenchmark/*/README CLI internals, kustomize/flexibility/faq/developer-guide.',
  '- "low"    ⭐ BACKGROUND: read only if working deep in that area. Examples: llm-d docs/architecture/* deep dives, docs/proposals/* (future designs), component-internal READMEs.',
  '- "skip"   — NOT useful for building features: CODE_OF_CONDUCT, LICENSE, CONTRIBUTING, SECURITY, PR_SIGNOFF, OWNERS/MAINTAINERS, issue/PR templates, .github/*, changelog-style files.',
  'When unsure between two tiers, pick the LOWER one and say why in why_useful. Be honest — do not inflate.',
].join('\n')

// ----------------------------------------------------------------------------
// Clustering rules — Prep assigns every enumerated file to exactly one cluster.
// Grouping is by repo + top-level area; large areas are split to keep clusters
// readable (<= ~16 files each). Re-runnable: derived from the live enumeration.
// ----------------------------------------------------------------------------
const CLUSTER_RULES = [
  'Build clusters by REPO then TOP-LEVEL AREA. Target <= ~16 files per cluster; split a large area into "<area>-a"/"<area>-b" if needed. EVERY enumerated file goes to EXACTLY ONE cluster; the sum of cluster file counts MUST equal the enumerated total (no file dropped, none duplicated).',
  'Suggested clusters (adapt to whatever actually exists on disk):',
  'llm-d-benchmark (repo "llm-d-benchmark", paths relative to ' + BENCH + '):',
  '  - bench-core-lifecycle:  README.md + docs/{lifecycle,standup,run,quickstart,teardown,smoketest}*.md',
  '  - bench-workload-harness: workload/README.md, config/README.md, docs/flexibility.md, skills/convert-guide/SKILL.md + skills/convert-guide/references/*.md (harnesses/mappings/patterns/templates), skills/add-metadata*/SKILL.md',
  '  - bench-analysis-doe-report: docs/{analysis,doe,benchmark_report}.md, docs/analysis/**/README.md, docs/tutorials/**/*.md',
  '  - bench-observability: docs/observability.md, docs/metrics_collection.md  (PRIORITY cluster)',
  '  - bench-repro-resources: docs/{reproducibility,resource_requirements,upstream-versions,workload-variant-autoscaler,faq,developer-guide,kustomize}.md',
  '  - bench-cli-internals: llmdbenchmark/**/README.md (the per-module CLI internals)',
  '  - bench-misc-meta: util/**, llm_d_stack_discovery/**, tests/README.md, experimental/**, and root governance/meta (CONTRIBUTING/SECURITY/CODE_OF_CONDUCT/PR_SIGNOFF) + .github/**',
  'llm-d (repo "llm-d", paths relative to ' + GUIDE + '):',
  '  - guide-deploy-guides: guides/README.md + guides/{optimized-baseline,prereq,recipes,rollouts,no-kubernetes-deployment}/**',
  '  - guide-helpers-bench: helpers/**/*.md (esp. helpers/benchmark.md, client-setup, hf-token, smoke-test, interactive-pod) + docs/getting-started/**',
  '  - guide-well-lit-paths: docs/well-lit-paths/** + the matching guides/{tiered-prefix-cache,pd-disaggregation,flow-control,wide-ep-lws,workload-autoscaling,batch-gateway,precise-prefix-cache-routing,predicted-latency-routing,asynchronous-processing}/**',
  '  - guide-api-and-obs: docs/api-reference/**, docs/resources/**, docs/{readiness-probes,infrastructure}.md, docs/accelerators/**',
  '  - guide-architecture-a: docs/architecture/core/** + docs/architecture/README.md',
  '  - guide-architecture-b: docs/architecture/advanced/**',
  '  - guide-proposals-meta: docs/proposals/**, docs/infra-providers/**, and root meta (README/PROJECT/ONBOARDING/ADOPTERS/MAINTAINERS/SIGS/etc.)',
  'If a file matches no suggested cluster, place it in the closest cluster by topic and note it. Keep .github/** but mark its files "skip".',
].join('\n')

// ----------------------------------------------------------------------------
// Schemas (additionalProperties:false everywhere)
// ----------------------------------------------------------------------------
const CLUSTER_SCHEMA = { type:'object', additionalProperties:false, properties:{
  name:{type:'string'}, repo:{type:'string', enum:['llm-d','llm-d-benchmark']}, files:{type:'array', items:{type:'string'}} },
  required:['name','repo','files'] }

const PREP_SCHEMA = { type:'object', additionalProperties:false, properties:{
  branch:{type:'string'}, worktree:{type:'string'}, baseOk:{type:'boolean'},
  benchCount:{type:'integer'}, guideCount:{type:'integer'}, totalCount:{type:'integer'},
  assignedCount:{type:'integer'},
  clusters:{type:'array', items:CLUSTER_SCHEMA},
  projectNote:{type:'string'}, notes:{type:'string'} },
  required:['branch','worktree','baseOk','clusters','benchCount','guideCount','totalCount','assignedCount'] }

const READ_SCHEMA = { type:'object', additionalProperties:false, properties:{
  cluster:{type:'string'}, inventoryPath:{type:'string'}, fileCount:{type:'integer'},
  high:{type:'integer'}, medium:{type:'integer'}, low:{type:'integer'}, skip:{type:'integer'},
  topPicks:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['cluster','inventoryPath','fileCount'] }

// The doc is written as 5 fragments (in this concatenation order) so NO single agent
// has to write the whole ~460-line doc AND commit — that exhausts one turn's budget.
const SECTIONS = [
  { key:'header', file:'frag_00_header.md', writesPointer:true },
  { key:'bench',  file:'frag_01_bench.md',  writesPointer:false },
  { key:'guide',  file:'frag_02_guide.md',  writesPointer:false },
  { key:'refs',   file:'frag_03_refs.md',   writesPointer:false },
  { key:'skip',   file:'frag_04_skip.md',   writesPointer:false },
]
const FRAG_ORDER = SECTIONS.map(s => BUILD + '/' + s.file).join(' ')

const FRAGMENT_SCHEMA = { type:'object', additionalProperties:false, properties:{
  section:{type:'string'}, fragmentPath:{type:'string'}, ok:{type:'boolean'},
  lines:{type:'integer'}, pointerWritten:{type:'boolean'}, notes:{type:'string'} },
  required:['section','fragmentPath','ok'] }

const SYNTH_SCHEMA = { type:'object', additionalProperties:false, properties:{
  indexPath:{type:'string'}, pointerPath:{type:'string'},
  fileCount:{type:'integer'}, high:{type:'integer'}, medium:{type:'integer'}, low:{type:'integer'}, skip:{type:'integer'},
  startHereCount:{type:'integer'},
  committed:{type:'boolean'}, commit:{type:'string'}, notes:{type:'string'} },
  required:['indexPath','pointerPath','committed'] }

const VERIFY_SCHEMA = { type:'object', additionalProperties:false, properties:{
  ok:{type:'boolean'},
  allFilesCovered:{type:'boolean'}, indexedCount:{type:'integer'}, enumeratedCount:{type:'integer'},
  namedExamplesPresent:{type:'boolean'}, pathsValid:{type:'boolean'}, tiersCalibrated:{type:'boolean'},
  missingFiles:{type:'array', items:{type:'string'}},
  blocking:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['ok','allFilesCovered','blocking'] }

// ----------------------------------------------------------------------------
// Prompts
// ----------------------------------------------------------------------------
const PREP_PROMPT = `Prepare a FRESH docs worktree for the "useful upstream docs index" effort, enumerate EVERY .md in BOTH upstream repos, cluster them, and snapshot our project context. Use Bash. main is NEVER touched. Do EXACTLY:

1. Sanity: note (do NOT switch) the main branch:  git -C ${MONO} rev-parse --abbrev-ref HEAD . Run  git -C ${MONO} status --porcelain  (if unrelated uncommitted work exists, leave it and mention it in notes).

2. Create (or reuse) the docs branch ${DOCBR} + worktree ${HOME} off main:
   - If ${HOME} already exists as a worktree ( git -C ${MONO} worktree list ): put it on ${DOCBR} ( git -C ${HOME} checkout ${DOCBR} ) and report reuse.
   - Else if branch ${DOCBR} exists ( git -C ${MONO} rev-parse --verify ${DOCBR} ):  git -C ${MONO} worktree add ${HOME} ${DOCBR}
   - Else:  git -C ${MONO} worktree add -b ${DOCBR} ${HOME} main
   Verify:  git -C ${HOME} rev-parse --abbrev-ref HEAD  == ${DOCBR}  -> set baseOk accordingly. Make the scratch dir:  mkdir -p ${BUILD}

3. ENUMERATE every doc to mine (read from the POPULATED main checkout, NOT the worktree — worktree siblings are EMPTY). Run BOTH:
   find ${BENCH} -name '*.md' -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/site-packages/*' | sed 's#${BENCH}/##' | sort     # => benchCount
   find ${GUIDE} -name '*.md' -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/site-packages/*' | sed 's#${GUIDE}/##' | sort      # => guideCount
   totalCount = benchCount + guideCount. (Expect roughly 57 + 132 = ~189; report the real numbers.)

4. CLUSTER the docs. ${CLUSTER_RULES}
   After clustering, COMPUTE assignedCount = the total number of file entries across all clusters and CONFIRM assignedCount == totalCount. If they differ, fix the clusters (a file was dropped or duplicated) BEFORE returning. Use repo-relative paths in each cluster's files[] (relative to that repo's root).

5. SNAPSHOT our project context (so readers judge "useful for US", not in the abstract — do NOT return full file text):
   - Read ${SRC}/CLAUDE.md (what the project is + the thin-code/thick-agent rules).
   - Tool list:  grep -nE 'name="|"name":' ${SRC}/app/tools/registry.py | head -60  (how many tools, what they do).
   - Knowledge files:  ls -1 ${SRC}/knowledge/  . Skim ${SRC}/FEATURES.md headings.
   Write a 3-5 line projectNote capturing what the agent does and what kinds of docs matter to it.

Return PREP_SCHEMA: {branch:"${DOCBR}", worktree:"${HOME}", baseOk, benchCount, guideCount, totalCount, assignedCount, clusters:[{name, repo:"llm-d"|"llm-d-benchmark", files:[...repo-relative...]}], projectNote, notes:"git base note + any anomalies"}.`

function readPrompt(cluster) {
  const repoRoot = cluster.repo === 'llm-d-benchmark' ? BENCH : GUIDE
  const isObs = /observ|metric/i.test(cluster.name)
  return `You are ONE of several parallel reader agents producing a USEFULNESS INDEX of upstream documentation for our agent project. Read a CLUSTER of "${cluster.repo}" docs and emit ONE structured entry PER FILE. Stay strictly inside your assigned files. Use Read/Bash/Grep.

${PROJECT_CONTEXT}

${RELEVANCE_RUBRIC}

== Your cluster ==
name: ${cluster.name}
repo: ${cluster.repo}   (files are relative to ${repoRoot})
files: ${JSON.stringify(cluster.files)}

== How to read ==
- For EACH file, read ${repoRoot}/<file> (the POPULATED main checkout). Skim long files for their structure + key reference points; you do not need to memorize every line, but you DO need an accurate sense of what it documents and whether/why it helps US.
- "reference_points" = the concrete things in the doc we would actually consult or use: CLI commands/flags (e.g. 'llmdbenchmark run --monitoring'), config/scenario/profile keys, env vars, CRD/API kinds (e.g. InferencePool), named guides/recipes, report fields, or important in-doc section anchors. Pull the real tokens, not vague topics.
- "external_links" = notable EXTERNAL URLs the doc points to (GitHub blobs, upstream docs, dashboards) that are worth following — especially API/feature references linked from a README. Capture the URL. (Do NOT fetch them; just record.)
- "why_useful" = ONE concrete line on how THIS doc helps build/operate our agent (or, for low/skip, one line on why it is background/noise).
${isObs ? '\n== PRIORITY: observability cluster ==\nThese docs (observability.md, metrics_collection.md) are the source of truth for the benchmark metrics we want to surface and default-enable (--monitoring, results.observability). Capture the --monitoring/--no-monitoring flags, metricsScrapeEnabled, the metric families (vLLM/EPP/DCGM/cAdvisor), and the Prometheus/Grafana pointers as reference_points. Tier these "high".\n' : ''}
== Output: WRITE a JSON file, do NOT return the array inline ==
mkdir -p ${BUILD}
Write ${BUILD}/index_${cluster.name}.json containing a JSON array with EXACTLY ONE object PER FILE in your cluster (fileCount MUST equal the number of files assigned), each object EXACTLY:
  {"repo": "${cluster.repo}",
   "path": "<repo-relative path, e.g. docs/lifecycle.md>",
   "title": "<the doc's first H1/title, or a sensible name>",
   "summary": "<1-2 plain-language lines: what this document is>",
   "covers": ["<topic>", "<topic>"],
   "relevance": "high"|"medium"|"low"|"skip",
   "why_useful": "<1 concrete line, project-specific>",
   "reference_points": ["<flag/cmd/key/CRD/anchor>", ...],
   "external_links": ["https://...", ...]}
Ensure VALID JSON (the next stage parses it). Then return READ_SCHEMA: {cluster:"${cluster.name}", inventoryPath:"${BUILD}/index_${cluster.name}.json", fileCount:<n == #files>, high, medium, low, skip, topPicks:[<=5 highest-value paths], notes:"empty files / surprises / files that didn't fit the cluster"}.`
}

const COMPOSE_PREAMBLE = `You write ONE SECTION of the curated upstream-docs index into a fragment file (a later cheap agent concatenates the fragments). This split exists because writing the whole ~460-line doc in one turn fails — so write ONLY your section, then return. Use Bash/Read/Write. Write ONLY inside ${BUILD} (and, if told, the pointer under ${PDIR}). main is NEVER touched.

== Shared inputs ==
- Per-file entries (JSON arrays) the readers wrote: ${BUILD}/index_*.json. Each object: {repo, path, title, summary, covers[], relevance:"high|medium|low|skip", why_useful, reference_points[], external_links[]}. Read the files relevant to YOUR section and parse them (jq or a tiny node/python one-liner is fine).
- The index is curated for the "llm-d-benchmarking-agent" (a chat agent that drives the llm-d-benchmark CLI to deploy llm-d stacks and run/parse benchmarks for non-experts).
- Tier glyphs: ⭐⭐⭐ = high, ⭐⭐ = medium, ⭐ = low, — = skip.
`

function composePrompt(section, fixFeedback) {
  const fb = fixFeedback ? '\n== VERIFY FEEDBACK FROM A PRIOR ROUND — fix every item that concerns YOUR section ==\n' + fixFeedback + '\n' : ''
  const out = BUILD + '/' + section.file
  const bodies = {
    header: `== Your section: HEADER + START HERE (fragment ${out}) ==
Read ALL ${BUILD}/index_*.json (you need global counts + the best must-reads). Write ${out} containing, in order:
1. "# Useful upstream docs — llm-d & llm-d-benchmark"
2. A short blockquote: what this is (a curated relevance map of every *.md in both upstream repos, for THIS agent — not a mirror), a generation note ("Generated from per-file reader entries (<N> *.md: <Nbench> in llm-d-benchmark, <Nguide> in llm-d)"), and a final line pointing to the runtime pointer \`knowledge/useful_repo_docs.md\`. Fill <N>/<Nbench>/<Nguide> from the actual entry counts.
3. "## Legend" — a 2-col table mapping ⭐⭐⭐/⭐⭐/⭐/— to high/medium/low/skip meanings, then a line "Counts: <h> ⭐⭐⭐ · <m> ⭐⭐ · <l> ⭐ · <s> — across <N> files." computed from the entries.
4. "## Start here" — a NUMBERED list of the ~14-16 single most essential docs across BOTH repos (high-tier must-reads only), each formatted \`repo/path\` — one concrete line on why it's a must-read. Prioritise: benchmark README + interface/README + developer-guide + config/README + run.md + standup.md + benchmark_report README + quickstart.md + doe.md + metrics_collection.md + convert-guide mappings; llm-d well-lit-paths/README + guides/optimized-baseline + helpers/benchmark.md + resources/observability/metrics.md + readiness-probes.md.
End the fragment with a line containing exactly: ---

THEN also write the runtime pointer ${PDIR}/${POINTER} (mkdir -p ${PDIR}/knowledge first): a COMPACT knowledge file (auto-discovered by read_knowledge) with: a 1-line purpose, the legend + counts, the "Start here" must-reads (repo/path + short why), then the OTHER high-tier docs grouped by repo+topic (path — short why), and a final line "Full annotated index: docs/USEFUL_REPO_DOCS.md". Do NOT reproduce the medium/low tables (avoid drift). Set pointerWritten=true.`,

    bench: `== Your section: llm-d-benchmark TABLES (fragment ${out}) ==
Read only ${BUILD}/index_bench-*.json. Write ${out}: a "## llm-d-benchmark" heading + a 1-2 line intro, then ONE "###" subsection per topic, each a markdown TABLE of the HIGH and MEDIUM entries in that topic (do NOT include low/skip — those go in the skip appendix). Columns EXACTLY: \`Doc\` | \`Covers\` | \`Why useful for us\` | \`Reference points\`. The Doc cell = the repo-relative path in backticks + the tier glyph (e.g. \`docs/run.md\` ⭐⭐⭐). Rows ordered high then medium. Use these subsections (drop one only if it has no high/medium entries): Core lifecycle & CLI / Workloads, harnesses & scenarios / Analysis, DOE & report / Observability & metrics / Reproducibility, resources & versions / CLI module internals / Config & convert-guide / Stack discovery. Keep cells single-line (escape any | inside text). End the fragment with a line containing exactly: ---`,

    guide: `== Your section: llm-d TABLES (fragment ${out}) ==
Read only ${BUILD}/index_guide-*.json. Write ${out}: a "## llm-d" heading + a 1-2 line intro, then ONE "###" subsection per topic, each a markdown TABLE of the HIGH and MEDIUM entries in that topic (NOT low/skip). Columns EXACTLY: \`Doc\` | \`Covers\` | \`Why useful for us\` | \`Reference points\`. Doc cell = repo-relative path in backticks + tier glyph. Rows high then medium. Subsections (drop any with no high/medium entries): Deploy guides (well-lit paths) / Helpers & benchmarking / Well-lit paths / API & CRD reference / Observability resources / Architecture / Infra providers & preconditions / Proposals & meta. Keep cells single-line. End the fragment with a line containing exactly: ---`,

    refs: `== Your section: API & FEATURE REFERENCE POINTS + EXTERNAL REFERENCES (fragment ${out}) ==
Read ALL ${BUILD}/index_*.json and harvest the reference_points[] and external_links[] arrays. Write ${out}:
1. "## API & feature reference points" + a 1-line intro, then "###" grouped bullet lists distilling the reference_points into the specific things we want to COVER or USE: Benchmark CLI & lifecycle flags (\`llmdbenchmark\`) / Workload & scenario keys / Report & metrics fields (Benchmark Report v0.2) / llm-d CRDs & EPP config / Observability knobs & metrics. Deduplicate; keep the concrete tokens (flags, env vars, CRD kinds, report field paths, metric names). This is the answer to "all the reference points of api-related stuff we want to cover and use".
2. "## External references" — the external_links deduped into a bullet list, each "[label](url) — 2-4 word note". Drop obvious noise.
End the fragment with a line containing exactly: ---`,

    skip: `== Your section: LOWER-RELEVANCE & SKIPPED APPENDIX (fragment ${out}) ==
Read ALL ${BUILD}/index_*.json and select EVERY entry with relevance "low" or "skip". Write ${out}: a "## Lower-relevance & skipped" heading + a 1-line note ("so no enumerated doc is silently dropped; ⭐ = low/background, — = skip/governance-CI-stub"), then "### llm-d-benchmark" and "### llm-d", each with a "**Low (⭐):**" bulleted list and a "**Skip (—):**" bulleted list. Each bullet: \`path\` — one-clause reason. EVERY low/skip file MUST appear (this appendix is what makes the index complete). Do NOT end with a --- separator (this is the last section); just end with a newline.`,
  }
  return COMPOSE_PREAMBLE + fb + '\n' + bodies[section.key] + `\n\nReturn FRAGMENT_SCHEMA: {section:"${section.key}", fragmentPath:"${out}", ok:true, lines:<line count of the fragment>, pointerWritten:${section.writesPointer ? 'true' : 'false'}, notes:"anything notable"}.`
}

function assemblePrompt(fixFeedback) {
  const fb = fixFeedback ? '\n== VERIFY FEEDBACK FROM THE PRIOR ROUND — resolve every blocking item before re-committing ==\n' + fixFeedback + '\n  (To add a file that Verify reports missing: append it to the appropriate fragment — usually ' + BUILD + '/frag_04_skip.md — then re-concatenate. To fix a section, edit that fragment then re-concatenate.)\n' : ''
  return `You ASSEMBLE the final docs index from the section fragments and COMMIT it on the docs branch. This is a CHEAP step: concatenate, sanity-check, commit. Use Bash/Read/Write. Write ONLY inside the worktree ${PDIR}. main is NEVER touched.
${fb}
== Steps ==
1. Confirm the fragments exist: ${FRAG_ORDER}. If any is missing, note it (you can still assemble from those present, but record the gap).
2. Concatenate IN ORDER into the final index (mkdir -p ${PDIR}/docs first):
   cat ${FRAG_ORDER} > ${PDIR}/${INDEX}
3. Sanity-check ${PDIR}/${INDEX}: it should contain the headings "# Useful upstream docs", "## Start here", "## llm-d-benchmark", "## llm-d", "## API & feature reference points", "## External references", and "## Lower-relevance & skipped", and its markdown tables should have consistent column counts. Lightly fix obvious concatenation glitches (e.g. a missing blank line before a heading) by editing the file.
4. Confirm the runtime pointer ${PDIR}/${POINTER} exists (the header agent wrote it). If it is MISSING, create a compact one: purpose + legend + the Start here list + "Full annotated index: docs/USEFUL_REPO_DOCS.md".
5. Commit (do NOT push, do NOT merge):
   git -C ${HOME} add ${PROJ}/${INDEX} ${PROJ}/${POINTER}
   git -C ${HOME} commit -m "docs: curated index of useful llm-d + llm-d-benchmark docs" -m "${TRAILER}"
   git -C ${HOME} rev-parse --short HEAD   # capture the hash
   (If a prior commit already exists on this branch for these files, you may amend instead: git -C ${HOME} commit --amend --no-edit after re-adding.)

Return SYNTH_SCHEMA: {indexPath:"${INDEX}", pointerPath:"${POINTER}", fileCount:<distinct .md paths referenced in the index>, high, medium, low, skip, startHereCount, committed:true, commit:"<hash>", notes:"any missing fragment or fix applied"}.`
}

const VERIFY_PROMPT = `Adversarially VERIFY the committed docs index on branch ${DOCBR} (worktree ${HOME}). Read-only (grep/read/find allowed). Files: ${PDIR}/${INDEX} and ${PDIR}/${POINTER}. The per-file source data: ${BUILD}/index_*.json. Upstream repos (populated): ${BENCH} and ${GUIDE}.

Checks (ALL must hold for ok=true):
1. COMPLETENESS — no doc dropped. Re-enumerate BOTH repos:
   find ${BENCH} -name '*.md' -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/site-packages/*' | wc -l
   find ${GUIDE} -name '*.md' -not -path '*/.venv/*' -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/site-packages/*' | wc -l
   enumeratedCount = sum. Then count how many DISTINCT upstream .md paths appear in ${PDIR}/${INDEX} (in the tables + the "Lower-relevance & skipped" appendix). EVERY enumerated file must appear somewhere in the index (high/medium/low/skip). Set allFilesCovered, indexedCount, enumeratedCount, and list any missingFiles (paths present on disk but absent from the doc). A few formatting-only mismatches are fine ONLY if every real file is still represented.
2. NAMED EXAMPLES present — the user explicitly wanted these; grep the index for each and confirm present:
   - llm-d-benchmark/README.md, llm-d-benchmark/docs/quickstart.md (the kind quick guide)
   - llm-d/guides/optimized-baseline (the optimized-baseline guide), llm-d/helpers/benchmark.md
   Set namedExamplesPresent=false (blocking) if any is missing.
3. PATHS VALID — sample ~10 doc paths cited in the index (mix of repos/tiers); confirm each exists on disk under ${BENCH}/ or ${GUIDE}/ (ls/test). Any cited path that does not exist => blocking. Set pathsValid.
4. TIER CALIBRATION — sanity-check the tiers: confirm the obvious must-reads are "high" (benchmark README + docs/{lifecycle,run,standup,quickstart,analysis,observability}.md, llm-d guides/optimized-baseline, helpers/benchmark.md) and that governance/meta (CODE_OF_CONDUCT/LICENSE/CONTRIBUTING/PR_SIGNOFF/templates) are "skip". Flag any badly miscategorized entry. Set tiersCalibrated.
5. STRUCTURE — the index has the "Start here" quicklist, per-repo grouped tables, an "API & feature reference points" section, and the "Lower-relevance & skipped" appendix; tables are well-formed (consistent columns). The knowledge pointer ${POINTER} exists and points to the full index.

Return VERIFY_SCHEMA: {ok, allFilesCovered, indexedCount, enumeratedCount, namedExamplesPresent, pathsValid, tiersCalibrated, missingFiles:[...], blocking:[...specific items...], notes}. ok=true ONLY if every check passes (allFilesCovered + namedExamplesPresent + pathsValid + tiersCalibrated + structure OK).`

// ----------------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------------
phase('Prep')
const prep = await agent(PREP_PROMPT, { label: 'prep', phase: 'Prep', agentType: 'general-purpose', schema: PREP_SCHEMA })
if (!prep || !prep.baseOk || !prep.clusters || !prep.clusters.length) {
  log('Prep failed (no worktree / no clusters). Aborting. Notes: ' + (prep && prep.notes))
  return { ok: false, stage: 'prep', prep }
}
if (prep.assignedCount !== prep.totalCount) {
  log('WARNING: Prep assignedCount ' + prep.assignedCount + ' != totalCount ' + prep.totalCount + ' (a file may be dropped/duplicated). Proceeding; Verify will catch gaps.')
}
log('Prep complete. Branch ' + prep.branch + ' @ ' + prep.worktree + '. ' + prep.totalCount + ' docs (' + prep.benchCount + ' benchmark + ' + prep.guideCount + ' llm-d) in ' + prep.clusters.length + ' clusters. ' + (prep.notes || ''))

// --- Read: one agent per cluster (parallel barrier) --------------------------
phase('Read')
log('Reading + tiering ' + prep.totalCount + ' docs across ' + prep.clusters.length + ' clusters in parallel')
const reads = (await parallel(prep.clusters.map(c => () =>
  agent(readPrompt(c), { label: 'read:' + c.name, phase: 'Read', agentType: 'general-purpose', schema: READ_SCHEMA })
))).filter(Boolean)
const totalIndexed = reads.reduce((n, r) => n + (r.fileCount || 0), 0)
const tier = (k) => reads.reduce((n, r) => n + (r[k] || 0), 0)
log('Read ' + totalIndexed + ' files: ⭐⭐⭐ ' + tier('high') + ' / ⭐⭐ ' + tier('medium') + ' / ⭐ ' + tier('low') + ' / — ' + tier('skip') + ' (across ' + reads.length + ' clusters)')
if (!reads.length) { log('No inventories produced. Aborting.'); return { ok: false, stage: 'read', prep } }
if (totalIndexed < prep.totalCount) {
  log('NOTE: indexed ' + totalIndexed + ' < enumerated ' + prep.totalCount + ' — some files may have been missed; Synthesize/Verify should surface them.')
}

// --- Compose: parallel section-writers -> fragment files (no giant single write) --
phase('Compose')
log('Composing ' + SECTIONS.length + ' doc fragments in parallel (header+pointer / bench / guide / refs / skip)')
const frags = (await parallel(SECTIONS.map(s => () =>
  agent(composePrompt(s, null), { label: 'compose:' + s.key, phase: 'Compose', agentType: 'general-purpose', schema: FRAGMENT_SCHEMA })
))).filter(Boolean)
const okFrags = frags.filter(f => f.ok)
log('Composed ' + okFrags.length + '/' + SECTIONS.length + ' fragments' + (okFrags.length < SECTIONS.length ? ' (some sections failed — Assemble will note gaps)' : '') + (frags.some(f => f.pointerWritten) ? '; pointer written' : ''))
if (!okFrags.length) { log('No fragments produced. Aborting.'); return { ok: false, stage: 'compose', prep } }

// --- Assemble: cat fragments + commit (cheap), then Verify (+ one bounded fix) ---
phase('Assemble')
let synth = await agent(assemblePrompt(null), { label: 'assemble', phase: 'Assemble', agentType: 'general-purpose', schema: SYNTH_SCHEMA })
if (!synth || !synth.committed) {
  log('Assemble did not commit. Notes: ' + (synth && synth.notes))
  return { ok: false, stage: 'assemble', prep, frags, synth }
}
log('Assembled + committed ' + (synth.commit || '') + ': index (' + (synth.fileCount || '?') + ' files; ⭐⭐⭐ ' + synth.high + ' / ⭐⭐ ' + synth.medium + ' / ⭐ ' + synth.low + ' / — ' + synth.skip + ') + knowledge pointer')

phase('Verify')
let verify = await agent(VERIFY_PROMPT, { label: 'verify', phase: 'Verify', agentType: 'general-purpose', schema: VERIFY_SCHEMA })
if (verify && !verify.ok && verify.blocking && verify.blocking.length) {
  const vfeedback = JSON.stringify({ blocking: verify.blocking, missingFiles: (verify.missingFiles || []).slice(0, 60), namedExamplesPresent: verify.namedExamplesPresent, pathsValid: verify.pathsValid, tiersCalibrated: verify.tiersCalibrated, notes: verify.notes }).slice(0, 6000)
  log('Verify found ' + verify.blocking.length + ' issue(s)' + (verify.missingFiles && verify.missingFiles.length ? ' (' + verify.missingFiles.length + ' missing files)' : '') + ' — one bounded Assemble fix pass')
  const synth2 = await agent(assemblePrompt(vfeedback), { label: 'assemble:fix', phase: 'Verify', agentType: 'general-purpose', schema: SYNTH_SCHEMA })
  if (synth2 && synth2.committed) synth = synth2
  verify = await agent(VERIFY_PROMPT, { label: 'reverify', phase: 'Verify', agentType: 'general-purpose', schema: VERIFY_SCHEMA })
}

const summary = {
  ok: !!(synth && synth.committed && verify && verify.ok),
  branch: prep.branch,
  worktree: prep.worktree,
  commit: synth && synth.commit,
  artifacts: { index: PROJ + '/' + INDEX, pointer: PROJ + '/' + POINTER },
  counts: { enumerated: prep.totalCount, bench: prep.benchCount, guide: prep.guideCount, indexed: synth && synth.fileCount,
            high: synth && synth.high, medium: synth && synth.medium, low: synth && synth.low, skip: synth && synth.skip },
  verify: verify && { ok: verify.ok, allFilesCovered: verify.allFilesCovered, indexedCount: verify.indexedCount, enumeratedCount: verify.enumeratedCount, namedExamplesPresent: verify.namedExamplesPresent, pathsValid: verify.pathsValid, tiersCalibrated: verify.tiersCalibrated, missingFiles: (verify.missingFiles || []).slice(0, 30), blocking: verify.blocking },
  note: 'Docs committed on ' + prep.branch + ' (off main; NOT pushed, NOT merged). Review then merge when ready.',
}
log('repo-docs-index finished. ' + (summary.ok ? 'OK' : 'NEEDS REVIEW') + ' — indexed ' + (synth && synth.fileCount) + '/' + prep.totalCount + ' docs; ⭐⭐⭐ ' + (synth && synth.high) + ' / ⭐⭐ ' + (synth && synth.medium) + ' / ⭐ ' + (synth && synth.low) + ' / — ' + (synth && synth.skip) + '; verify ' + (verify && verify.ok ? 'OK' : 'review'))
return summary
