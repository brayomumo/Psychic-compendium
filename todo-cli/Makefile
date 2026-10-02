# todo-cli. Every variable below can be overridden: make run DATA_FILE=work.json

GO            ?= go
STATICCHECK   ?= staticcheck
GOLANGCI_LINT ?= golangci-lint
BIN           ?= bin/todo
DATA_FILE     ?= tasks.json
DEMO_FILE     ?= .demo-tasks.json

# The module is standalone: never pick up a go.work from a parent directory
# (for example when this repo is checked out inside another workspace).
export GOWORK := off

# What `make demo` types into the prompt: add two tasks, complete the first,
# try to delete a task that does not exist, list both views, quit.
DEMO_INPUT := e\nBuy milk\n2 litres\ne\nWrite README\n\nf\n1\nd\n9\nl\na\nq\n

.PHONY: build run demo test lint check clean

build:
	$(GO) build -o $(BIN) ./cmd/todo

# Interactive session on a local, gitignored file. Ends on q or Ctrl+D.
run: build
	./$(BIN) -file $(DATA_FILE)

# Scripted session against a fresh file; exits on its own.
demo: build
	rm -f $(DEMO_FILE)
	printf '$(DEMO_INPUT)' | ./$(BIN) -file $(DEMO_FILE)

test:
	$(GO) test -race -count=1 ./...

lint:
	@unformatted="$$(gofmt -l .)"; \
	if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	$(GO) vet ./...
	$(STATICCHECK) ./...
	$(GOLANGCI_LINT) run ./...

check: lint test

# Removes build and demo output only. DATA_FILE holds your tasks and is kept.
clean:
	rm -rf $(dir $(BIN)) $(DEMO_FILE)
