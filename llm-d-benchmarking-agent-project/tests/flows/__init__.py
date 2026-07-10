"""Flow validation: prove the agent drives the *right commands* for each end-to-end
flow (kind quickstart, optimized-baseline, teardown, …), both deterministically (golden
transcripts, hermetic, CI-gating) and — opt-in — against a live LLM from mock input.

See ``docs/reference/VALIDATION.md`` for the why and how-to-add-a-flow guide.
"""
