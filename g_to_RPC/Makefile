# g_to_RPC: a Go gRPC server and a typed Python client covering all four RPC
# kinds, deadlines, rich errors, interceptors, health checks and graceful
# shutdown. Target contract: STANDARDS.md section 7.

# Each module is self-contained. Ignore any go.work in a parent directory.
export GOWORK := off

GO            ?= go
STATICCHECK   ?= staticcheck
GOLANGCI_LINT ?= golangci-lint
BUF           ?= buf
PYTHON        ?= python3
UV            ?= uv
RUFF          ?= uvx ruff@0.16.10
PORT          ?= 50059

# The plugin versions the committed generated code was produced with.
export PROTOC_GEN_GO_VERSION      := v1.36.11
export PROTOC_GEN_GO_GRPC_VERSION := v1.6.1
export BUF UV

# Make uv build the venv with the python3 on PATH, not one it picks itself.
export UV_PYTHON := $(shell command -v $(PYTHON))

SERVER_BIN := server/bin/productserver
GOBIN_DIR  := $(shell $(GO) env GOPATH)/bin
RUN        := cd client && $(UV) run --locked

.PHONY: build sync run test test-go test-py lint lint-go lint-py check \
	generate check-generated clean

build:
	cd server && $(GO) build -o bin/productserver .

sync:
	cd client && $(UV) sync --locked

run: build sync
	$(RUN) python -m productclient.launch --server ../$(SERVER_BIN) --port $(PORT)

test: test-go test-py

test-go:
	cd server && $(GO) test -race -count=1 ./...

# The Python suite includes end-to-end tests against the built server.
test-py: build sync
	$(RUN) python -m unittest discover -s tests -v

lint: lint-go lint-py

lint-go:
	@cd server && unformatted="$$(gofmt -l .)"; \
		if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	cd server && $(GO) vet ./...
	cd server && $(STATICCHECK) ./...
	cd server && $(GOLANGCI_LINT) run ./...

lint-py: sync
	cd client && $(RUFF) check .
	cd client && $(RUFF) format --check .
	$(RUN) mypy --strict .

check: lint test

# Regenerates server/gen (buf with the local Go plugins) and client/ecommerce
# (grpcio-tools and mypy-protobuf from the locked dev environment).
generate: sync
	PATH="$(GOBIN_DIR):$$PATH" $(BUF) generate
	$(RUN) python -m grpc_tools.protoc -I ../proto \
		--python_out=. --grpc_python_out=. --mypy_out=. --mypy_grpc_out=. \
		ecommerce/v1/product.proto
	touch client/ecommerce/__init__.py client/ecommerce/v1/__init__.py

# Lints the proto API with buf and fails if the committed generated code is
# stale. Needs buf and the pinned plugins, so it is separate from `check`.
check-generated: sync
	PATH="$(GOBIN_DIR):$$PATH" ./tools/check-generated.sh

clean:
	rm -rf server/bin client/.venv client/.mypy_cache client/.ruff_cache
	find client -name __pycache__ -type d -prune -exec rm -rf {} +
