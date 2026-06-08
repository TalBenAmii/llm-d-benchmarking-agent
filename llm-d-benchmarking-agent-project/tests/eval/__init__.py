"""Agent self-eval harness — two OPT-IN, quota-spending LLM layers + their always-on
deterministic shadow/oracle guards.

See ``docs/VALIDATION.md`` for the full picture. In brief:

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
``tests/flows/test_flows_live.py`` uses) and never run in plain ``pytest`` or gating CI.
``make eval-shadow`` is the always-safe hermetic entry.
"""
