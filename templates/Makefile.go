# <prototype>: <one line saying what it demonstrates>.
# Target contract: STANDARDS.md section 7. Copy to <prototype>/Makefile.

# Each prototype is its own module. Ignore any go.work in a parent directory.
export GOWORK := off
# Build, test and lint with the toolchain go.mod pins (its `toolchain` line).
# Go downloads that toolchain on first use, so nothing is installed globally,
# and linters built with it can analyse code written for it.
export GOTOOLCHAIN := $(shell sed -n 's/^toolchain //p' go.mod)

GO            ?= go
# Linters are pinned and run through `go run`, so every machine and CI use the
# same versions (STANDARDS.md appendix).
STATICCHECK   ?= $(GO) run honnef.co/go/tools/cmd/staticcheck@2026.2.1
GOLANGCI_LINT ?= $(GO) run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0
GOVULNCHECK   ?= $(GO) run golang.org/x/vuln/cmd/govulncheck@v1.8.0
BIN           ?= bin/<name>
ARGS          ?=

.PHONY: build run test lint check vulncheck clean

build:
	$(GO) build -o $(BIN) ./cmd/<name>

run: build
	$(BIN) $(ARGS)

test:
	$(GO) test -race -count=1 ./...

lint:
	@# The pinned toolchain's gofmt, not whichever gofmt is first on PATH.
	@unformatted="$$($(GO) run cmd/gofmt -l .)"; \
		if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	$(GO) vet ./...
	$(STATICCHECK) ./...
	$(GOLANGCI_LINT) run ./...

check: lint test

# Needs network access to the Go vulnerability database, so it runs as its own
# CI job rather than inside `check`.
vulncheck:
	$(GOVULNCHECK) ./...

# Build outputs only. Never delete user data here.
clean:
	rm -f $(BIN)
