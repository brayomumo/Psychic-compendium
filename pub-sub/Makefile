# RabbitMQ publish/consume prototype. Run `make help` for the targets.

PYTHON ?= python3
UV ?= uv
RUFF ?= uvx ruff@0.16.10
MYPY ?= uvx mypy@2.4.0
BROKER_CONTAINER ?= psychic-compendium-rabbitmq
BROKER_IMAGE ?= rabbitmq:4.1-management
AMQP_PORT ?= 5679
MANAGEMENT_PORT ?= 15679
BROKER_WAIT_S ?= 90
RABBITMQ_URL ?= amqp://guest:guest@localhost:$(AMQP_PORT)/%2F

# Build the virtualenv on the python3 from PATH rather than whichever
# interpreter uv would discover first.
export UV_PYTHON := $(shell command -v $(PYTHON))
export RABBITMQ_URL
# The integration tests restart the broker through this name.
export PUBSUB_BROKER_CONTAINER := $(BROKER_CONTAINER)

RUN := $(UV) run --locked python -m

.PHONY: help sync run publish consume test test-integration lint check \
	broker-up broker-down clean

help:
	@echo "make run               broker up, then the end-to-end demo"
	@echo "make publish           run the publisher (PUBSUB_* env vars apply)"
	@echo "make consume           run the consumer until Ctrl+C"
	@echo "make test              unit and process tests (no broker needed)"
	@echo "make test-integration  tests against the local broker"
	@echo "make check             lint, format check, type check, then test"
	@echo "make broker-up         start the local RabbitMQ and wait for it"
	@echo "make broker-down       remove the local RabbitMQ container"
	@echo "make clean             remove the virtualenv and caches"

sync:
	$(UV) sync --locked

run: sync broker-up
	$(RUN) pubsub.demo

publish: sync
	$(RUN) pubsub.publisher

consume: sync
	$(RUN) pubsub.consumer

test: sync
	$(RUN) unittest discover -s tests -v

test-integration: sync broker-up
	PUBSUB_INTEGRATION=1 $(RUN) unittest discover -s tests \
		-p 'test_integration.py' -v

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) --strict .

check: lint test

# Ports are bound to 127.0.0.1 only: this broker uses the default
# guest/guest account and must not be reachable from the network.
broker-up:
	@if docker container inspect $(BROKER_CONTAINER) >/dev/null 2>&1; then \
		docker start $(BROKER_CONTAINER) >/dev/null; \
	else \
		docker run --detach \
			--name $(BROKER_CONTAINER) --hostname $(BROKER_CONTAINER) \
			--publish 127.0.0.1:$(AMQP_PORT):5672 \
			--publish 127.0.0.1:$(MANAGEMENT_PORT):15672 \
			--health-cmd 'rabbitmq-diagnostics -q check_port_connectivity' \
			--health-interval 2s --health-timeout 10s \
			--health-retries 30 --health-start-period 5s \
			$(BROKER_IMAGE) >/dev/null; \
	fi
	@echo "waiting for $(BROKER_CONTAINER) to become healthy..."
	@for _ in $$(seq $(BROKER_WAIT_S)); do \
		status=$$(docker inspect -f '{{.State.Health.Status}}' \
			$(BROKER_CONTAINER)); \
		[ "$$status" = healthy ] && exit 0; \
		sleep 1; \
	done; \
	echo "$(BROKER_CONTAINER) not healthy after $(BROKER_WAIT_S)s;" \
		"see: docker logs $(BROKER_CONTAINER)" >&2; \
	exit 1

broker-down:
	docker rm --force $(BROKER_CONTAINER)

clean:
	rm -rf .venv .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
