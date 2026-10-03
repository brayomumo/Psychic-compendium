# g_to_RPC: a Go gRPC server and a typed Python client covering all four RPC
# kinds, deadlines, rich errors, interceptors, health checks and graceful
# shutdown. Target contract: STANDARDS.md section 7.

# Each module is self-contained. Ignore any go.work in a parent directory.
export GOWORK := off
# Build, test and lint with the toolchain server/go.mod pins (its `toolchain`
# line). Go downloads that toolchain on first use, so nothing is installed
# globally, and linters built with it can analyse code written for it.
export GOTOOLCHAIN := $(shell sed -n 's/^toolchain //p' server/go.mod)

GO            ?= go
# Linters are pinned and run through `go run`, so every machine and CI use the
# same versions (STANDARDS.md appendix).
STATICCHECK   ?= $(GO) run honnef.co/go/tools/cmd/staticcheck@2026.2.1
GOLANGCI_LINT ?= $(GO) run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0
GOVULNCHECK   ?= $(GO) run golang.org/x/vuln/cmd/govulncheck@v1.8.0
PYTHON        ?= python3
UV            ?= uv
RUFF          ?= uvx ruff@0.16.10
PORT          ?= 50059

# Code generators, pinned. The committed generated code was produced with
# exactly these versions; check-generated fails on any other.
export BUF_VERSION                := v1.73.0
export PROTOC_GEN_GO_VERSION      := v1.36.12
export PROTOC_GEN_GO_GRPC_VERSION := v1.6.2

# The generators are installed into a gitignored .tools/bin on first use, so a
# fresh machine or CI runner needs only Go and uv. The stamp's name carries
# every version, so changing one reinstalls them all.
TOOLS_DIR   := $(CURDIR)/.tools
TOOLS_BIN   := $(TOOLS_DIR)/bin
TOOLS_STAMP := $(TOOLS_DIR)/installed-buf-$(BUF_VERSION)-go-$(PROTOC_GEN_GO_VERSION)-grpc-$(PROTOC_GEN_GO_GRPC_VERSION)-$(GOTOOLCHAIN)
# Put .tools/bin first on PATH, for the recipes that need the generators only.
WITH_TOOLS  := PATH="$(TOOLS_BIN):$$PATH"

# Make uv build the venv with the python3 on PATH, not one it picks itself.
export UV_PYTHON := $(shell command -v $(PYTHON))
export UV

SERVER_BIN := server/bin/productserver
RUN        := cd client && $(UV) run --locked

.PHONY: build sync run test test-go test-py lint lint-go lint-py check \
	vulncheck generate check-generated clean clean-tools

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
	@# The pinned toolchain's gofmt, not whichever gofmt is first on PATH.
	@cd server && unformatted="$$($(GO) run cmd/gofmt -l .)"; \
		if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	cd server && $(GO) vet ./...
	cd server && $(STATICCHECK) ./...
	cd server && $(GOLANGCI_LINT) run ./...

lint-py: sync
	cd client && $(RUFF) check .
	cd client && $(RUFF) format --check .
	$(RUN) mypy --strict .

check: lint test

# Needs network access to the Go vulnerability database, so it runs as its own
# CI job rather than inside `check`.
vulncheck:
	cd server && $(GOVULNCHECK) ./...

$(TOOLS_STAMP):
	rm -rf $(TOOLS_DIR)
	GOBIN=$(TOOLS_BIN) $(GO) install github.com/bufbuild/buf/cmd/buf@$(BUF_VERSION)
	GOBIN=$(TOOLS_BIN) $(GO) install google.golang.org/protobuf/cmd/protoc-gen-go@$(PROTOC_GEN_GO_VERSION)
	GOBIN=$(TOOLS_BIN) $(GO) install google.golang.org/grpc/cmd/protoc-gen-go-grpc@$(PROTOC_GEN_GO_GRPC_VERSION)
	touch $@

# Regenerates server/gen (buf with the pinned Go plugins) and client/ecommerce
# (grpcio-tools and mypy-protobuf from the locked dev environment).
generate: $(TOOLS_STAMP) sync
	$(WITH_TOOLS) buf generate
	$(RUN) python -m grpc_tools.protoc -I ../proto \
		--python_out=. --grpc_python_out=. --mypy_out=. --mypy_grpc_out=. \
		ecommerce/v1/product.proto
	touch client/ecommerce/__init__.py client/ecommerce/v1/__init__.py

# Lints the proto API with buf and fails if the committed generated code is
# stale. Self-sufficient: installs the pinned generators on first use.
check-generated: $(TOOLS_STAMP) sync
	$(WITH_TOOLS) ./tools/check-generated.sh

# Build outputs and caches only; the installed generators stay (clean-tools).
clean:
	rm -rf server/bin client/.venv client/.mypy_cache client/.ruff_cache
	find client -name __pycache__ -type d -prune -exec rm -rf {} +

clean-tools:
	rm -rf $(TOOLS_DIR)
