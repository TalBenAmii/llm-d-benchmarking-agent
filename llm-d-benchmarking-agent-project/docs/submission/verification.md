# Submission verification — 6 October 2026

## Method

The old `final_submission/` was removed and rebuilt from current owned source and
Dockerfile-pinned upstream repositories. The Windows Downloads architecture HTML supplied
by the author was adapted into the repository architecture guide and the presentation.
Student metadata and the author's existing project reflections were retained.

The submission ZIP was extracted into a new directory under `/tmp`, outside the development
checkout. The documented `uv sync --locked --extra dev --python 3.11` created a new application
virtual environment, using CPython 3.11.15. Benchmark CLI dependencies were installed into a
second new environment from the submitted sources and pinned requirements; MCP was installed
from its submitted sibling source. No development `.env`, virtualenv or Git checkout was
copied into the extraction. Internet access was used for Python/package dependencies.

The reproducible driver is `scripts/submission/verify_submission.py`:

```bash
python3 llm-d-benchmarking-agent-project/scripts/submission/verify_submission.py \
  final_submission/project_submission.zip --destination /tmp/llmd-submission-final-check
```

The destination must not already exist. It stores the extraction, per-command logs and
`results.json` recording the tested ZIP's SHA-256. The final archive's checksum also lives
beside the ZIP. Source hashes and exact repository revisions are inside `docs/source_manifest.json`.

## Results

| Check | Result |
|---|---|
| Fresh application install | Passed; committed lockfile, new CPython 3.11.15 environment. |
| Full application suite before optional CLI install | **2,391 passed, 53 skipped**, four dependency deprecation warnings. |
| Statement/branch coverage | **93.19%**, above the 85% gate. |
| Ruff / mypy | Passed; mypy checked 99 application source files. |
| Deterministic workflow replay | **44/44 passed**; no model calls or deployments. |
| Benchmark CLI install | Passed in its own new environment; `llmdbenchmark --help` exits 0. |
| Aggregation tests after CLI install | **23 passed**, including the real subprocess bridge. |
| Standalone MCP suite | **17 passed** from the extracted adapter source. |
| HTTP through the actual `scripts/run.sh` launcher | UI, health, readiness, metrics, provider metadata, history and fixture preview returned 200. |
| WebSocket | Connected and received the application's `ready` event. |
| Actual MCP stdio | Initialize/list succeeded: **35 tools, 61 resources, 5 prompts**; `list_catalog` and a knowledge-resource read succeeded. |
| Live Codex conversation | The documented setup script reused the tester's existing ChatGPT login. The extracted app called `list_catalog`, returned actual harness names and emitted `done` without an error. No mutation was requested or approved. |
| Browser | Chrome loaded the actual extracted app and fixture preview. Theme/debug controls and guided-builder open/close worked. Desktop/mobile views inspected; no JavaScript errors or failed local requests. |
| Helm / shell | Chart lint and Bash syntax checks passed. Helm's optional icon recommendation remains. |
| Presentation | **13 A4 pages** (portrait, with a landscape architecture page); exact title/name-only cover, no browser headers/footers, no clipped content or footer overlap. Diagram checked in the rendered PDF. |
| Archive | CRC, exact required roots, student schema, source inventory, per-file SHA-256, executable modes and safe paths verified. |
| Exclusions | No Git history, environments, `.env` secrets, runtime chats or caches. Credential-pattern scan found no matches. Student ID is in the ZIP metadata, not committed to public source. |

## Issues found and corrected

- The previous submission described only Claude, used stale upstream pins/statistics and
  understated shell/Auto-approve/MCP exceptions. The final guides reflect both engines and
  the implementation's actual execution boundaries.
- An unconstrained benchmark install selected Transformers 4.12.2/tokenizers 0.10.3 and
  failed to build the old tokenizer on Python 3.11. The CLI also requires the planner at
  import time. The submitted `benchmark-requirements.txt` pins the complete successfully
  tested dependency set, including planner/optimizer Git revisions. The clean recheck
  installs these together and successfully invokes the CLI.
- New packaging helpers initially had three Ruff issues; these were fixed and the full
  extracted lint/type checks then passed.
- The automated smoke server can linger during shutdown after background environment
  probes. The verifier bounds cleanup and kills its own process group if needed; a passing
  HTTP/WebSocket check is not a claim of graceful shutdown in every environment.

## Scope and prerequisites

The base suite's skips cover opt-in live LLM/cluster/soak tests, absent promtool, upstream
experiment examples and (before optional installation) the benchmark environment. The two
aggregation dependency skips were subsequently exercised by the 23-test bridge run.
The four warnings are upstream dependency deprecations, not application test failures.

The live smoke used a pre-existing ChatGPT subscription login outside the extraction.
No credentials are included in the ZIP: a tutor needs their own login for live chat.
The fixture UI and deterministic replay work without any paid account. Claude login was
not available, so a live Claude turn was not verified in this refresh.

No fresh GPU/model benchmark, kind-stack deployment, full system-package installer run,
container build/publish or GR upload is claimed. These optional paths require infrastructure
and downloads described in the guides. Existing recorded/demo and historical evaluation
artifacts remain historical evidence. This is a tested source distribution, not an offline
bundle of system tools, account credentials, models or container images.

Licenses/notices are retained and upstream origins recorded. See `third_party.md` for the
asset inventory and the course's public-repository condition; no trademark clearance is
asserted and repository visibility was not changed.
