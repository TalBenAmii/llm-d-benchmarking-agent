# llm-d Benchmarking Assistant Agent

This folder holds all of the project's code and docs. See the overview, installation steps, and security model in the
[**repository root**](../README.md).

Useful links:

- **Get started:** [Quick start](../README.md#quick-start)
- **Features and verification:** [`docs/reference/FEATURES.md`](docs/reference/FEATURES.md)
- **MCP server for Claude Code:** [`docs/reference/MCP.md`](docs/reference/MCP.md)
- **Documentation index:** [`docs/README.md`](docs/README.md)

## Reproducible upstream versions

The local installer's fresh clones use the `LLMD_REF`, `BENCH_REF` and `SKILLS_REF`
revisions in `Dockerfile`, matching the container baseline. Existing checkouts are
preserved. Set `LLMD_REVISION`, `BENCH_REVISION` or `SKILLS_REVISION` only when deliberately
testing another version. Moving upstream HEAD has changed report paths, scenario structure
and workload identifiers; pulling upstream alone does not establish compatibility.

The current baseline is benchmark `a75d91e`, llm-d `51f59431`, and skills `d23b14c`.
It uses the `guides/wide-ep` name, nested scenario settings, GuideLLM `spec` profiles,
and Benchmark Report 0.2.1. Capacity preflight and profile inspection also retain support
for older flat formats. Validate a different checkout with the full suite; `/readyz`
checks repository availability and authentication, not upstream format compatibility.

`SIMULATE=1` still needs a working Claude login: read-only probes execute normally,
and approved mutations return synthetic results. `/readyz` does not verify the login.
