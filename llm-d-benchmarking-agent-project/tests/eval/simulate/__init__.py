"""Opt-in SIMULATE-mode agent evals — spend LLM quota, never run in plain ``pytest`` / gating CI.

These drive the REAL model but *only* in the app's SIMULATE walk (hermetic; nothing is executed),
gated behind ``LLM_EVAL_LIVE=1``:

  * ``test_skill_usage_live.py`` — asserts the agent fetches the operation's ``llm-d-skills``
    SKILL.md (``fetch_key_docs(task=<*_skill>)`` or ``read_repo_doc`` under ``llm-d-skills/``)
    BEFORE it deploys/benchmarks/tears-down/compares/autoscales. Always runs ``simulate=True``.

The default-live / real-app evals live next door in ``tests/eval/live/``. Shared infra
(``harness``/``flows``) stays under ``tests/flows/`` and is imported by absolute path.
"""
