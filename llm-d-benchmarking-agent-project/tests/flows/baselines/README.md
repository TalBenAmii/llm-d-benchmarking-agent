# WS event-stream baselines (both engines' wire behavior, pinned)

Each `<name>.events.json` is the NORMALIZED server→client WebSocket stream of one golden flow,
captured by driving the REAL FastAPI app over an in-process WS with that flow's ScriptedProvider
transcript + CaptureRunner (the hermetic `tests/flows/harness.py` sandbox, lifted to the WS
layer). `<name>.sdk-native.events.json` is the SAME conversation driven by the SDK-native engine
(`--engine sdk-native`: AGENT_ENGINE switch + a FakeTransport playing the same transcript). The
guard test diffs the two sets event-for-event; the streams are byte-identical modulo `usage`
cadence (old: one per LLM call, mid-turn; new: one per SDK response, post-result) — the one
adjudicated engine difference.

Normalized at capture: `seq`/timestamps/durations stripped; usage token numbers omitted;
request/tool-call ids → `<approval-N>`/`<tc-N>`; absolute paths → `<WS>`/`<REPOS>`/`<TMP>`/
`<PROJECT>`/`<HOME>`; session id → `<SID>`; long strings truncated; `resource_stats` +
`assistant_delta` dropped (timing/chunking-dependent). Captures run WITHOUT pytest's autouse
skill-grounding fixture, so the skill gate is live — exactly as in production.

Recapture (only after a deliberate wire-behavior change; from a worktree; add
`--engine sdk-native` for the second set):

    PYTHONPATH=<worktree>/llm-d-benchmarking-agent-project REPOS_DIR=<repo-root> \
      <repo-root>/llm-d-benchmarking-agent-project/.venv/bin/python \
      scripts/eval/capture_ws_baseline.py

Guard: `tests/flows/test_ws_baselines.py` (files exist, parse, end with `done`, and the
engine-parity diff itself). The capture script is NOT run in CI.
