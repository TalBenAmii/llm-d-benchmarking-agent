"""Opt-in LIVE-mode agent evals — spend LLM quota, never run in plain ``pytest`` / gating CI.

These drive the REAL model (their *default* / primary mode) and are gated behind
``LLM_EVAL_LIVE=1`` (bughunt also needs ``BUGHUNT=1``):

  * ``test_flows_live.py`` — does the live LLM choose the right commands per flow? (dual-mode:
    also runs the simulate set via ``LLM_EVAL_SIMULATE=1``; here by its default "live" mode)
  * ``test_judge_live.py`` — a judge LLM scores each session transcript vs. ``../rubric.md``
    (same dual-mode switch as the flow eval)
  * ``test_bughunt_live.py`` — an LLM drives the real app HTTP+WS surface; deterministic oracle gates

The purely-simulate eval lives next door in ``tests/eval/simulate/``. Shared infra
(``harness``/``flows`` under ``tests/flows/``; ``judge``/``scorecard``/``explorer``/``bug_report``
under ``tests/eval/``) stays put and is imported by absolute path.
"""
