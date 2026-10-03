# todo-cli: a testable Go CLI, with a pure core, I/O at the edges and atomic persistence.
# Target contract: STANDARDS.md section 7. Every variable can be overridden:
# make run DATA_FILE=work.json

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
BIN           ?= bin/todo
ARGS          ?=
DATA_FILE     ?= tasks.json
DEMO_FILE     ?= .demo-tasks.json

# What `make demo` types into the prompt: add two tasks, complete the first,
# try to delete a task that does not exist, list both views, quit.
DEMO_INPUT := e\nBuy milk\n2 litres\ne\nWrite README\n\nf\n1\nd\n9\nl\na\nq\n

.PHONY: build run demo test lint check vulncheck clean

build:
	$(GO) build -o $(BIN) ./cmd/todo

# Interactive session on a local, gitignored file. Ends on q or end of input.
run: build
	$(BIN) -file $(DATA_FILE) $(ARGS)

# Scripted session against a fresh file; exits on its own.
demo: build
	rm -f $(DEMO_FILE)
	printf '$(DEMO_INPUT)' | $(BIN) -file $(DEMO_FILE)

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

# Build and demo outputs only. DATA_FILE holds your tasks and is never deleted.
clean:
	rm -f $(BIN) $(DEMO_FILE)
