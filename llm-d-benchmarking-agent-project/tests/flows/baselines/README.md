# WS event-stream baselines (the old agent loop's wire behavior, pinned)

Each `<name>.events.json` is the NORMALIZED server→client WebSocket stream of one golden flow,
captured by driving the REAL FastAPI app over an in-process WS with that flow's ScriptedProvider
transcript + CaptureRunner (the hermetic `tests/flows/harness.py` sandbox, lifted to the WS
layer). They exist so a replacement agent engine (the SDK-native engine work) can be diffed
against the current loop event-for-event.

Normalized at capture: `seq`/timestamps/durations stripped; usage token numbers omitted;
request/tool-call ids → `<approval-N>`/`<tc-N>`; absolute paths → `<WS>`/`<REPOS>`/`<TMP>`/
`<PROJECT>`/`<HOME>`; session id → `<SID>`; long strings truncated; `resource_stats` +
`assistant_delta` dropped (timing/chunking-dependent). Captures run WITHOUT pytest's autouse
skill-grounding fixture, so the skill gate is live — exactly as in production.

Recapture (only after a deliberate wire-behavior change; from a worktree):

    PYTHONPATH=<worktree>/llm-d-benchmarking-agent-project REPOS_DIR=<repo-root> \
      <repo-root>/llm-d-benchmarking-agent-project/.venv/bin/python \
      scripts/eval/capture_ws_baseline.py

Guard: `tests/flows/test_ws_baselines.py` (files exist, parse, end with `done`). The capture
script is NOT run in CI; content diffing is the engine-parity phase's job.
