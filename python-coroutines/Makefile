# Generator-based coroutines: demos, tests, and the lint/type-check gate.
# Tool versions are pinned so `make check` gives the same answer everywhere.

PYTHON ?= python3
RUFF ?= uvx ruff@0.16.10
MYPY ?= uvx mypy@2.4.0

.PHONY: run test lint check clean

run:
	$(PYTHON) basic.py
	$(PYTHON) pipeline.py
	$(PYTHON) dispatcher.py
	$(PYTHON) pubsub.py
	$(PYTHON) threaded.py

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) --strict .

check: lint test

clean:
	rm -rf __pycache__ tests/__pycache__ .mypy_cache .ruff_cache
