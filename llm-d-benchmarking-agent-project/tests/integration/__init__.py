"""Opt-in integration test layer (Phase 26).

Everything under ``tests/integration`` is GATED on the ``LLMD_SIM_INTEGRATION`` env flag
AND on ``llm-d-inference-sim`` actually being available in the environment. When either is
absent, the integration tests SKIP cleanly so the default suite stays fully hermetic and
green with no new required dependency.

The *wiring* an integration test exercises end-to-end — parsing an inference-sim-shaped
Benchmark Report v0.2 through ``analyze_results`` / ``compare_reports`` — is ALSO covered
hermetically here (``test_sim_integration.py``) using a sim-shaped report fixture built from
the repo's own BR v0.2 example, so the integration logic is tested even when the sim binary
is absent.
"""
