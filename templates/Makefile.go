# <prototype>: <one line saying what it demonstrates>.
# Target contract: STANDARDS.md section 7. Copy to <prototype>/Makefile.

# Each prototype is its own module. Ignore any go.work in a parent directory.
export GOWORK := off

GO            ?= go
STATICCHECK   ?= staticcheck
GOLANGCI_LINT ?= golangci-lint
BIN           ?= bin/<name>
ARGS          ?=

.PHONY: build run test lint check clean

build:
	$(GO) build -o $(BIN) ./cmd/<name>

run: build
	$(BIN) $(ARGS)

test:
	$(GO) test -race -count=1 ./...

lint:
	@unformatted="$$(gofmt -l .)"; \
		if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	$(GO) vet ./...
	$(STATICCHECK) ./...
	$(GOLANGCI_LINT) run ./...

check: lint test

# Build outputs only. Never delete user data here.
clean:
	rm -f $(BIN)
