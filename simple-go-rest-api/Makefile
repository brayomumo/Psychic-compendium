# simple-go-rest-api: a production-shaped REST API on net/http and PostgreSQL.
# Target contract: STANDARDS.md section 7.

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
COMPOSE       ?= docker compose
BIN_DIR       ?= bin

# What `make run` starts: STORE=memory needs no Docker.
STORE    ?= postgres
API_ADDR ?= 127.0.0.1:8089

# Host ports for the compose stack. Non-default, so a local PostgreSQL on 5432
# or another service on 8080 does not clash. compose.yaml reads both.
DB_PORT  ?= 5439
API_PORT ?= 8089
export DB_PORT API_PORT

DATABASE_URL ?= postgres://albums:albums@127.0.0.1:$(DB_PORT)/albums?sslmode=disable

.PHONY: build run test test-integration lint check vulncheck db-up db-down docker-smoke clean

build:
	$(GO) build -o $(BIN_DIR)/albums ./cmd/albums
	$(GO) build -o $(BIN_DIR)/democlient ./cmd/democlient

# The demo client starts the server, checks every endpoint, then stops it
# with SIGTERM and checks the clean exit.
run: build $(if $(filter postgres,$(STORE)),db-up)
	DATABASE_URL='$(DATABASE_URL)' $(BIN_DIR)/democlient \
		-server $(BIN_DIR)/albums -store $(STORE) -addr $(API_ADDR)

test:
	$(GO) test -race -count=1 ./...

test-integration: db-up
	REST_API_INTEGRATION=1 DATABASE_URL='$(DATABASE_URL)' $(GO) test -race -count=1 ./...

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

# Starts PostgreSQL (container psychic-compendium-postgres) and waits for its
# healthcheck. It keeps running so reruns are fast; `make db-down` removes it.
db-up:
	$(COMPOSE) up --detach --wait db

# Removes the containers and the data volume.
db-down:
	$(COMPOSE) down --volumes --remove-orphans

# Builds the image, runs the whole stack, checks the API from the host
# through the published port, then stops the API container and checks it
# exited 143 (a clean SIGTERM shutdown). The database is left running.
docker-smoke: build
	$(COMPOSE) up --detach --wait --build
	@status=0; \
	$(BIN_DIR)/democlient -base-url http://127.0.0.1:$(API_PORT) || status=$$?; \
	$(COMPOSE) stop api >/dev/null; \
	code=$$(docker inspect --format '{{.State.ExitCode}}' psychic-compendium-rest-api); \
	$(COMPOSE) rm --force api >/dev/null; \
	echo "api container exited with $$code after SIGTERM (want 143)"; \
	[ "$$code" = 143 ] || status=1; \
	exit $$status

# Build outputs only. The database volume is removed by db-down, never here.
clean:
	rm -rf $(BIN_DIR)
