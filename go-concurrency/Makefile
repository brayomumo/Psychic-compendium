# go-concurrency. Every variable below can be overridden: make run CONSUMERS=8 BUFFER=0

# Demo parameters for `make run`.
PRODUCERS     ?= 2
CONSUMERS     ?= 4
JOBS          ?= 100
BUFFER        ?= 10
WORK          ?= 10ms
SEED          ?= 1
ARGS          ?=

# Tools, and where `make build` writes the binary.
GO            ?= go
STATICCHECK   ?= staticcheck
GOLANGCI_LINT ?= golangci-lint
BIN           ?= bin/go-concurrency

# The module is standalone: never pick up a go.work from a parent directory
# (for example when this repo is checked out inside another workspace).
export GOWORK := off

.PHONY: build run test lint check clean

build:
	$(GO) build -o $(BIN) .

run: build
	$(BIN) -producers $(PRODUCERS) -consumers $(CONSUMERS) -jobs $(JOBS) \
		-buffer $(BUFFER) -work $(WORK) -seed $(SEED) $(ARGS)

test:
	$(GO) test -race -count=1 ./...

lint:
	@unformatted="$$(gofmt -l .)"; \
		if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	$(GO) vet ./...
	$(STATICCHECK) ./...
	$(GOLANGCI_LINT) run ./...

check: lint test

clean:
	rm -f $(BIN)
