# Developer entry points. Uses the project venv when present, else the system python
# (so CI, which builds a fresh interpreter, works without changes).
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

.PHONY: help install test validate validate-pytest validate-live snapshot-catalog flows

help:
	@echo "make validate         # prove the agent runs the right commands per flow (deterministic, hermetic)"
	@echo "make validate-live    # same, but a real LLM drives each flow from mock input (needs an API key)"
	@echo "make validate-pytest  # the same checks as gating pytest (tests/flows)"
	@echo "make flows            # list the known flows"
	@echo "make test             # full test suite"
	@echo "make install          # editable install with dev extras"
	@echo "make snapshot-catalog # regenerate the frozen catalog snapshot from the live repo"

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest tests/ -v

# --- the headline: "validate that the agent runs the right commands" ---------
validate:
	$(PY) scripts/validate_flows.py --show

validate-pytest:
	$(PY) -m pytest tests/flows/test_flows.py -v

validate-live:
	LLM_EVAL_LIVE=1 $(PY) scripts/validate_flows.py --live

flows:
	$(PY) scripts/validate_flows.py --list

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
