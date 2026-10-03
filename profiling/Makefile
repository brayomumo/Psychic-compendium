# profiling: where a Python program spends its time and memory, and how far
# to trust what the profiler says.
# Target contract: STANDARDS.md section 7. Tool versions are pinned so
# `make check` gives the same answer everywhere.

PYTHON  ?= python3
RUFF    ?= uvx ruff@0.16.10
MYPY    ?= uvx mypy@2.4.0
PROFILE ?= build/workload.prof
ARGS    ?=

.PHONY: run profile bench test lint check clean

run:
	$(PYTHON) main.py $(ARGS)

profile:
	$(PYTHON) main.py cprofile --output $(PROFILE) $(ARGS)

bench:
	$(PYTHON) main.py bench $(ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) --strict .

check: lint test

clean:
	rm -rf build __pycache__ tests/__pycache__ .mypy_cache .ruff_cache
