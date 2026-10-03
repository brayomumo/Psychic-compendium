# Piper: producer processes fan in over pipes to a bounded thread pool.
# Run every target from this directory. `make check` is the gate.

PYTHON ?= python3
UVX ?= uvx
RUFF ?= $(UVX) ruff@0.16.10
MYPY ?= $(UVX) mypy@2.4.0
# Extra flags for demo.py (make run) or bench.py (make bench).
ARGS ?=

.PHONY: run test lint check bench clean

run:
	$(PYTHON) demo.py $(ARGS)

test:
	$(PYTHON) -m unittest discover -s tests -v

# Formatters, linters and the type checker.
lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) --strict .

check: lint test

bench:
	$(PYTHON) bench.py $(ARGS)

clean:
	rm -rf __pycache__ tests/__pycache__ .mypy_cache .ruff_cache
