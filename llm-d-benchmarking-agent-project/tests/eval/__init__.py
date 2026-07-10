"""Agent self-eval harness — two OPT-IN, quota-spending LLM layers + their always-on
deterministic shadow/oracle guards.

See ``docs/reference/VALIDATION.md`` for the full picture. In brief:

  * **(A) LLM-judge quality eval** — a judge LLM scores each agent session transcript against
    the versioned ``rubric.md`` and emits an aggregate AGENT-QUALITY SCORE
    (``judge.py`` + ``scorecard.py``). The always-on guard is ``test_scorecard_shadow.py``
    (deterministic shadow scoring of the golden transcripts — NO quota); the opt-in LLM layer
    is ``test_judge_live.py`` (gated by ``LLM_EVAL_LIVE=1`` + a provider-auth check).

  * **(B) autonomous exploratory bug-hunter** — an LLM drives the REAL app over the same
    HTTP+WS surface the deterministic fuzzer drives (``app_driver.py``), with the existing
    invariant battery as the deterministic bug ORACLE (only it can fail a build) and an
    advisory-only LLM triage (``explorer.py`` + ``bug_report.py``). The always-on guard is
    ``test_oracle_unit.py`` (deterministic oracle + report assembly — NO quota); the opt-in
    LLM layer is ``test_bughunt_live.py`` (same gating).

CARDINAL COST RULE: plain ``pytest`` spends ZERO quota — only the deterministic shadow/oracle
layer is always-on. The LLM layers share the ``LLM_EVAL_LIVE`` switch (the SAME one
``tests/eval/live/test_flows_live.py`` uses) and never run in plain ``pytest`` or gating CI.
``make eval-shadow`` is the always-safe hermetic entry.

Layout: the opt-in live-LLM agent evals live in ``tests/eval/live/`` (default-live / real-app)
and ``tests/eval/simulate/`` (the SIMULATE-only skill-usage eval); the hermetic shadow/oracle
guards (``test_scorecard_shadow.py`` / ``test_oracle_unit.py``) and their support modules stay
directly under ``tests/eval/``.
"""
