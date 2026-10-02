# bulkhead: why per-dependency bulkheads stop one slow dependency from
# starving everything else. Target contract: STANDARDS.md section 7.
# Tool versions are pinned so `make check` gives the same answer everywhere.

PYTHON ?= python3
RUFF   ?= uvx ruff@0.16.10
MYPY   ?= uvx mypy@2.4.0
ARGS   ?=

.PHONY: run test lint check bench clean

run:
	$(PYTHON) main.py $(ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) --strict .

check: lint test

bench:
	$(PYTHON) bench.py $(ARGS)

clean:
	rm -rf __pycache__ tests/__pycache__ .mypy_cache .ruff_cache
