# llm-d Benchmarking Assistant — Developer Guide

## Design and modules

Read [Architecture](ARCHITECTURE.md) for the final diagram, module/file map, request flow
and four validation gates. The diagram derives from the supplied architecture HTML and
is shared with the presentation. The browser and standalone MCP interfaces reuse a single
tool registry. Claude and Codex engines translate provider events; knowledge files guide
benchmark decisions, while Python validates data and runs approved work.

## Build from the submitted ZIP

Use Linux/WSL2, Bash, Python 3.11, uv, Git and Internet access. `make` is convenient for quality
targets. Keep the source sibling layout intact and use editable installs: UI, knowledge,
policy and upstream schemas are loaded from the checkout at runtime.

```bash
cd src/llm-d-benchmarking-agent-project
uv sync --locked --extra dev --python 3.11
cp .env.example .env
.venv/bin/python -m pytest tests/ -n 4 --cov=app --cov-fail-under=85 -ra
make lint typecheck
.venv/bin/python scripts/eval/validate_flows.py
```

The ordinary tests use fake model/command adapters. Live and cluster checks are opt-in;
passing the ordinary suite does not establish GPU performance or a live provider login.
For browser startup and real provider configuration, use the user manual. Production
runtime libraries and dev tools are declared in `pyproject.toml`; `uv.lock` pins app
resolution. The static UI needs no frontend package manager.

### Optional benchmark CLI environment (no deployment)

This installs the packaged Python CLI and report library, enabling report-bridge tests
without installing the system cluster toolchain. A pinned requirements file also installs
the planner and its optimizer dependency from Git (Git and Internet access are required):

```bash
uv venv --python 3.11 ../llm-d-benchmark/.venv
uv pip install --python ../llm-d-benchmark/.venv/bin/python \
  -r scripts/submission/benchmark-requirements.txt \
  -e ../llm-d-benchmark/benchmark-report -e ../llm-d-benchmark
../llm-d-benchmark/.venv/bin/llmdbenchmark --help
.venv/bin/python -m pytest tests/tools/test_aggregate_runs.py -q
```

Real deployment additionally requires the system client tools installed by the full local
installer in the user manual. This CLI check does not create a cluster or download models.

### Optional standalone MCP interface

```bash
uv pip install --python .venv/bin/python -e '../llm-d-bench-mcp[dev]'
(cd ../llm-d-bench-mcp && ../llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/ -q)
```

Configure an MCP client to launch the absolute path to `.venv/bin/llm-d-bench-mcp`.
Use the extracted project as cwd and `REPOS_DIR` as the extracted `src/` if launching
elsewhere. The server speaks stdio; client tool permissions govern execution. A later
`uv sync` can prune the optional adapter; reinstall it or use `uv sync --inexact`.

## Extend and verify

Add an input model under `app/tools/schemas/`, a handler in `setup/`, `run/`, `analyze/` or
`access/`, and a ToolSpec in `registry.py`. Route dedicated commands through ToolContext,
update policy data when needed, and test invalid input, rejection and execution behavior.
Keep selection/remediation guidance in `knowledge/`. Check both SDK engines and the MCP
adapter after shared-contract changes. Add deterministic flows for new workflows.

Do not edit the read-only upstream repositories as part of agent behavior. Compatibility
pins are defined in `Dockerfile`; synchronize installer/CI references before upgrading.
`NOTICE` and upstream licenses retain attribution. The ZIP omits Git history, so runtime
provenance may have unknown SHAs; the submission source manifest records revisions and
per-file SHA-256 hashes.

## Rebuild the submission

From a Git checkout with the four sibling repositories available, run:

```bash
python3 llm-d-benchmarking-agent-project/scripts/submission/build_submission.py
```

Before a first build, copy `scripts/submission/students.example.json` to
`scripts/submission/students.json` and fill in the real student details. The latter is
Git-ignored and is copied only to the submission ZIP root.

Requires Google Chrome for HTML-to-PDF printing, plus Python 3 and Git. The builder derives
its repository root from its location, uses current owned source and Dockerfile-pinned
upstreams, materializes safe symlinks, retains executable modes and excludes runtime data.
It copies the canonical user/developer guides, diagram, student metadata and presentation
into `final_submission/`. The PDF uses A4 pages and a title/name-only cover. Part 1 is the
PDF; Part 2 is `project_submission.zip`. The ZIP does not depend on the builder to run.

Use `scripts/submission/verify_submission.py <zip> --destination <new-directory>` to
validate layout/hashes, extract source and run the documented Python installation and
checks in new environments. Logs are kept outside the submission. Consult
`docs/submission/verification.md` for the precise checks completed for this release.
