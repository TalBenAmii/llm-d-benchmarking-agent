# Developer entry points. Uses the project venv when present, else the system python
# (so CI, which builds a fresh interpreter, works without changes).
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

# Coverage gate (Phase 14): set a few points BELOW the measured baseline (~89%).
# Tune here, not in tool config, so it can move with the suite without churn.
COV_FAIL_UNDER := 85

# Container image coordinates (override on the CLI, e.g. `make image VERSION=0.2.0`).
IMAGE   ?= ghcr.io/llm-d/llm-d-benchmarking-agent
VERSION ?= 0.1.0
# Pinned llm-d-benchmark ref baked into the image (the Dockerfile default matches this).
BENCH_REF ?= v0.7.0

.PHONY: help install test validate validate-pytest validate-live validate-live-iso \
        validate-simulate-iso snapshot-catalog flows \
        lint typecheck coverage quality eval-shadow eval-judge bughunt \
        image image-publish

help:
	@echo "make validate         # prove the agent runs the right commands per flow (deterministic, hermetic)"
	@echo "make validate-live    # same, but a real LLM drives each flow from mock input (needs an API key)"
	@echo "make validate-live-iso     # validate-live, but per-flow process isolation + an EXTERNAL hard timeout (a stuck flow can't wedge the run)"
	@echo "make validate-simulate-iso # same isolation, SIMULATE mode (FLOWS=\"a b\" runs a subset)"
	@echo "make eval-shadow      # agent-quality + bug-oracle SHADOW checks (deterministic, hermetic, ZERO quota)"
	@echo "make eval-judge       # LLM-judge agent-quality scorecard (OPT-IN, SPENDS QUOTA, needs a key)"
	@echo "make bughunt          # autonomous exploratory bug-hunter (OPT-IN, SPENDS QUOTA, needs a key)"
	@echo "make validate-pytest  # the same checks as gating pytest (tests/flows)"
	@echo "make flows            # list the known flows"
	@echo "make test             # full test suite"
	@echo "make lint             # ruff static checks (no reformatting)"
	@echo "make typecheck        # mypy type check of app/"
	@echo "make coverage         # full suite under coverage, gated at $(COV_FAIL_UNDER)%"
	@echo "make quality          # lint + typecheck + coverage (the CI quality gate)"
	@echo "make install          # sync .venv from uv.lock (with dev extras)"
	@echo "make snapshot-catalog # regenerate the frozen catalog snapshot from the live repo"
	@echo "make image            # build the self-contained container image ($(IMAGE):$(VERSION))"
	@echo "make image-publish    # build then docker push it to the registry (needs docker login)"

install:
	uv sync --extra dev

test:
	$(PY) -m pytest tests/ -v

# --- Quality gates (Phase 14): ruff + mypy + coverage -------------------------
lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy app

coverage:
	$(PY) -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=$(COV_FAIL_UNDER)

# Everything the CI quality-gates job enforces, in one local command.
quality: lint typecheck coverage

# --- the headline: "validate that the agent runs the right commands" ---------
validate:
	$(PY) scripts/validate_flows.py --show

validate-pytest:
	$(PY) -m pytest tests/flows/test_flows.py -v

validate-live:
	LLM_EVAL_LIVE=1 $(PY) scripts/validate_flows.py --live

# Isolated variants: each flow runs in its OWN process under an EXTERNAL `timeout -s TERM -k`, so a
# wedged event loop / deadlocked SDK subprocess can never stall the whole run (the in-process asyncio
# caps in validate_flows.py can be defeated by a frozen loop — see docs/reference/VALIDATION.md). Optional
# FLOWS="name1 name2" re-runs just a subset (e.g. confirming suspected-infra failures flip to PASS).
# In a git WORKTREE the sibling repos are empty, so point REPOS_DIR at the primary checkout.
validate-live-iso:
	scripts/run_eval_isolated.sh live $(FLOWS)

validate-simulate-iso:
	scripts/run_eval_isolated.sh simulate $(FLOWS)

# --- Self-eval (Layer 3 + 4): agent-quality judge + exploratory bug-hunter ----
# eval-shadow is HERMETIC + ZERO quota (the always-on guard, runs in plain pytest too).
# eval-judge + bughunt are OPT-IN and SPEND LLM QUOTA — they share the LLM_EVAL_LIVE switch
# (bughunt also needs BUGHUNT=1), exactly like validate-live. NEVER run them in CI gating.
eval-shadow:
	$(PY) -m pytest tests/eval/test_scorecard_shadow.py tests/eval/test_oracle_unit.py -v

eval-judge:
	LLM_EVAL_LIVE=1 $(PY) -m pytest tests/eval/live/test_judge_live.py -v --timeout=600

bughunt:
	LLM_EVAL_LIVE=1 BUGHUNT=1 $(PY) -m pytest tests/eval/live/test_bughunt_live.py -v --timeout=600

flows:
	$(PY) scripts/validate_flows.py --list

# --- Container image (self-contained "full-bake": app + CLI venv + repos + toolchain) ---------
# Large (~1 GB) and needs build-time network egress — see the Dockerfile header.
image:
	docker build -t $(IMAGE):$(VERSION) --build-arg BENCH_REF=$(BENCH_REF) .

# Push assumes you have already run `docker login ghcr.io` (or your registry) with push rights.
image-publish: image
	docker push $(IMAGE):$(VERSION)

# Print the live catalog so you can refresh tests/flows/catalog_snapshot.py when the
# upstream repo changes (the drift guard flags when a refresh is due).
snapshot-catalog:
	@$(PY) -c "import json; from app.tools.catalog import build_catalog; \
c = build_catalog('../llm-d-benchmark'); \
print('present:', c['present']); \
print('SPECS =', json.dumps(c['specs'])); \
print('HARNESSES =', json.dumps(c['harnesses'])); \
print('WORKLOADS =', json.dumps(c['workloads'])); \
print('WORKLOADS_BY_HARNESS =', json.dumps(c['workloads_by_harness']))"
