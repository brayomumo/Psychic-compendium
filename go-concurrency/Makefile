# Demo parameters for `make run`. Override on the command line, e.g. `make run CONSUMERS=8 BUFFER=0`.
PRODUCERS ?= 2
CONSUMERS ?= 4
JOBS      ?= 100
BUFFER    ?= 10
WORK      ?= 10ms
SEED      ?= 1
ARGS      ?=

# Where `make build` writes the binary.
BIN       ?= bin/go-concurrency

.PHONY: build run test lint check clean

build:
	go build -o $(BIN) .

run: build
	$(BIN) -producers $(PRODUCERS) -consumers $(CONSUMERS) -jobs $(JOBS) \
		-buffer $(BUFFER) -work $(WORK) -seed $(SEED) $(ARGS)

test:
	go test -race -count=1 ./...

lint:
	@unformatted="$$(gofmt -l .)"; \
		if [ -n "$$unformatted" ]; then echo "gofmt needed:"; echo "$$unformatted"; exit 1; fi
	go vet ./...
	staticcheck ./...
	golangci-lint run ./...

check: lint test

clean:
	rm -f $(BIN)
