# Simple Go REST API

How do I build a correct, production-shaped REST API in Go (routing, validation, status codes,
persistence, timeouts, graceful shutdown), and how much of it needs a framework? This prototype is
a small albums API on the standard library's `net/http` and PostgreSQL. It shows that since Go 1.22
the router needs no framework. The hard part of "production-shaped" is everything around the
routes: rejecting bad input with useful errors, owning IDs, bounding every request in size and
time, and shutting down without dropping work.

## Concept

**Routing is in the standard library now.** Since Go 1.22, `http.ServeMux` matches methods and
path wildcards: `mux.HandleFunc("GET /albums/{id}", h)` with `r.PathValue("id")`. A pattern with a
method is more specific than the same pattern without one. Registering a method-less fallback
(`/albums`) therefore catches only the methods a route lacks. This prototype uses that to answer
them with a JSON `405` and an `Allow` header, instead of ServeMux's plain-text one. `GET` patterns
also serve `HEAD`.

**One error shape, stable codes.** Clients program against `code`; people read `message`.
Validation lists every bad field at once, so a client fixes everything in one round trip. Every
response carries the request ID that also appears in the server's log line.

```json
{"error":{"code":"validation_failed","message":"the album is invalid","fields":[{"field":"title","message":"is required"},{"field":"price_cents","message":"must be between 0 and 10000000"}],"request_id":"demo-2"}}
```

**The server owns identity.** A client that picks IDs can pick duplicates. The first version
did exactly that. IDs come from the database (`GENERATED ALWAYS AS IDENTITY`). An `id` in a
request body is rejected as an unknown field, and `201 Created` carries a `Location` header.

**Money is an integer.** Binary floating point can't represent most decimal amounts (`0.1 + 0.2
!= 0.3`), so prices are `price_cents`, an integer number of minor units. The domain and a
database `CHECK` both bound them.

**Every request is bounded, in three layers:**
- *Connection:* `http.Server` timeouts, where a zero value means "forever":
  - `ReadHeaderTimeout` stops a client that trickles header bytes (Slowloris).
  - `ReadTimeout`, `WriteTimeout` and `IdleTimeout` bound the rest of the connection.
  - `MaxHeaderBytes` caps header size.
- *Request:* bodies are capped (`http.MaxBytesReader`, 16 KiB, `413`). Only `application/json` is
  accepted (`415`). Unknown fields and trailing data are rejected.
- *Dependency:* each database call runs under a per-request deadline (default 2 s). A hung
  database becomes `503` plus `Retry-After`, instead of requests piling up until clients time out.

**Graceful shutdown drains; it doesn't drop.** On SIGTERM, `http.Server.Shutdown` closes the
listeners, so new connections are refused, and waits for in-flight requests to finish. The trap is
`BaseContext`. If request contexts derive from the signal context, the signal cancels every
in-flight request at the moment the drain should protect them. Requests are cut off only when
`-shutdown-timeout` passes. The pool closes after the drain, so draining requests can still query.

**Liveness is not readiness.** `/healthz` says the process is up and never touches the database,
so a database outage doesn't get the API restarted in a loop. `/readyz` pings the database and says
whether this instance can serve traffic right now.

**Keyset pagination.** `GET /albums?after_id=N&limit=L` uses an index seek, `WHERE id > $1 ORDER BY
id LIMIT $2`. It's stable while rows are inserted, and costs the same on page 1 and page 1000. With
`OFFSET`, both of those fail. The server fetches `limit + 1` rows to learn whether another page
exists without a `COUNT(*)`.

## Design

```mermaid
flowchart LR
    C[HTTP client] --> I["instrument<br/>request ID, panic recovery, access log"]
    I --> M["http.ServeMux<br/>GET/POST /albums, GET /albums/{id},<br/>/healthz, /readyz, JSON 404/405"]
    M --> H["handlers<br/>decode, validate (internal/album), map errors"]
    H -->|"ctx with 2 s deadline"| S{{"api.Store interface"}}
    S --> MEM["store.Memory<br/>mutex, tests and -store memory"]
    S --> PG["store.Postgres<br/>pgxpool, schema under advisory lock"]
    PG --> DB[("PostgreSQL 17")]
    SIG["SIGINT / SIGTERM"] --> SRV["server.Serve<br/>Shutdown: drain, then cut off"]
    SRV -.owns.-> I
```

```
cmd/albums/            flags, env, store choice, banner, exit codes; real-signal tests
cmd/democlient/        end-to-end checker used by make run and make docker-smoke
internal/album/        domain: Album, Draft, validation, sentinel errors (no I/O)
internal/api/          routing, JSON in/out, error shape, middleware
internal/server/       http.Server timeouts and graceful shutdown
internal/store/        Memory and Postgres stores, one shared contract test suite, schema.sql
internal/sigctx/       SIGINT/SIGTERM as context cancellation that remembers the signal
```

**Ownership and shutdown.**
- `run` (in `cmd/albums`) owns the signal context, the store and the listener.
- `server.Serve` owns the `http.Server`. When the context is cancelled it drains, cuts off what's
  left at the deadline, then returns.
- `run` then closes the store (the pgx pool) and exits `128 + signal`.

Nothing else starts goroutines, apart from pgx's pool health checker, which `Close` stops.

### API

| Method and path | Success | Errors |
|---|---|---|
| `GET /albums?limit=&after_id=` | `200 {"albums":[...], "next_after_id": N}` (`next_after_id` absent on the last page; `limit` 1–100, default 20) | `400 invalid_query` |
| `POST /albums` with `{"title","artist","price_cents"}` | `201` and `Location: /albums/{id}` | `400 invalid_json` / `validation_failed`, `409 conflict` (same title and artist), `413`, `415` |
| `GET /albums/{id}` | `200` | `400 invalid_id`, `404 not_found` |
| `GET /healthz` | `200 {"status":"ok"}` | |
| `GET /readyz` | `200 {"status":"ready"}` | `503 not_ready` |
| any other method on a route | | `405 method_not_allowed` and `Allow` |
| any other path | | `404 not_found` |

A store timeout is `503 unavailable` with `Retry-After: 1`. Anything unexpected is
`500 internal`, and its details go to the log, never to the client.

## Run

Requires any `go` 1.21 or newer on `PATH` and Docker (for PostgreSQL). The Makefile exports
`GOTOOLCHAIN` from go.mod's `toolchain go1.27.1` line, so builds, tests and linters all run on
Go 1.27.1, which `go` downloads on first use. `make run STORE=memory` needs no Docker.

```console
$ make run
docker compose up --detach --wait db
 Container psychic-compendium-postgres  Healthy
DATABASE_URL='postgres://albums:albums@127.0.0.1:5439/albums?sslmode=disable' bin/democlient \
		-server bin/albums -store postgres -addr 127.0.0.1:8089
level=INFO msg="connected to PostgreSQL; schema is up to date"
ok    readiness probe                        GET     /readyz                            want 200 got 200
ok    create Blue Train                      POST    /albums                            want 201 got 201
ok    create Jeru                            POST    /albums                            want 201 got 201
ok    create Sarah Vaughan and Clifford Brown POST    /albums                            want 201 got 201
ok    get it back by Location                GET     /albums/1                          want 200 got 200
ok    page through them, two at a time       GET     /albums?after_id=…&limit=2         want ids [1 2 3] got [1 2 3]
ok    same title and artist again            POST    /albums                            want 409 got 409
ok    malformed JSON                         POST    /albums                            want 400 got 400
...
ok    liveness probe                         GET     /healthz                           want 200 got 200
level=INFO msg="shutting down: no new connections; draining in-flight requests" timeout=10s
level=INFO msg="shutdown complete"
level=INFO msg="stopped by signal" signal=terminated
ok    server drains and exits on SIGTERM     SIGTERM                                    want exit 143 got 143

18 checks, 0 failed
```

The demo client starts the server, creates the first version's three albums, and pages through
them. It provokes every error class, then sends SIGTERM and checks for a clean `143` exit. Its
exit status is non-zero if any check fails. Albums are tagged per run (`Blue Train [run 3fa1c2]`),
so reruns against the persistent database don't collide. The server's per-request log lines
(stderr) are omitted above.

A real response, for reference:

```console
$ curl -i -H 'Content-Type: application/json' -H 'X-Request-ID: demo-1' \
    -d '{"title":"Blue Train","artist":"John Coltrane","price_cents":5699}' localhost:8089/albums
HTTP/1.1 201 Created
Content-Type: application/json
Location: /albums/1
X-Content-Type-Options: nosniff
X-Request-Id: demo-1

{"id":1,"title":"Blue Train","artist":"John Coltrane","price_cents":5699,"created_at":"2026-10-02T13:28:44.4327Z"}
```

**The container.** `make docker-smoke` builds the image, starts the compose stack, and runs the
same checks from the host through the published port. It then stops the API container and checks
that it exited `143`:

```console
$ make docker-smoke
...
17 checks, 0 failed
api container exited with 143 after SIGTERM (want 143)
```

The image is 4.0 MB (`distroless/static`), runs as uid 65532 (`nonroot`), and `albums` is PID 1,
so it receives `docker stop`'s SIGTERM directly.

| Make target | What it does |
|---|---|
| `make build` | Builds `bin/albums` and `bin/democlient`. |
| `make run` | Runs the demo above; exits 0 only if every check passes. |
| `make test` | Unit tests under `-race` (no Docker). |
| `make test-integration` | Starts PostgreSQL and runs every test, including the store suite, against it. |
| `make lint` | The pinned toolchain's gofmt, `go vet`, staticcheck 2026.2.1, golangci-lint v2.14.0. |
| `make check` | `lint`, then `test`. The merge gate; needs no Docker. |
| `make vulncheck` | govulncheck v1.8.0; fails if any known vulnerability is reachable. Needs network, so not part of `check`. |
| `make db-up` / `make db-down` | Start PostgreSQL and wait for its healthcheck / remove the containers and the data volume. |
| `make docker-smoke` | The container check above. |
| `make clean` | Removes `bin/`. Never the database. |

`make run`, `make test-integration` and `make docker-smoke` leave the database container
(`psychic-compendium-postgres`) running so reruns are fast. `make db-down` removes it.

| Make variable | Default | Meaning |
|---|---|---|
| `STORE` | `postgres` | Store for `make run`: `postgres` or `memory` |
| `API_ADDR` | `127.0.0.1:8089` | Where `make run` starts the server |
| `DB_PORT` | `5439` | Host port for PostgreSQL (127.0.0.1 only) |
| `API_PORT` | `8089` | Host port for the API container (127.0.0.1 only) |
| `DATABASE_URL` | `postgres://albums:albums@127.0.0.1:$(DB_PORT)/albums?sslmode=disable` | Local-only throwaway credentials |
| `GO` | `go` | Any Go 1.21+; `GOTOOLCHAIN` (from go.mod, not a knob) selects go1.27.1 |
| `STATICCHECK`, `GOLANGCI_LINT`, `GOVULNCHECK` | `$(GO) run honnef.co/go/tools/cmd/staticcheck@2026.2.1`, `$(GO) run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0`, `$(GO) run golang.org/x/vuln/cmd/govulncheck@v1.8.0` | Pinned linters, built on first use |
| `COMPOSE` | `docker compose` | Compose command |
| `BIN_DIR` | `bin` | Build output |

| `albums` flag or env | Default | Meaning |
|---|---|---|
| `DATABASE_URL` (env only) | none | PostgreSQL DSN; required with `-store postgres`. Never a flag, so the password stays out of `ps`. Pool settings go in the DSN (`pool_max_conns=10`). |
| `-addr` / `REST_API_ADDR` | `:8080` | Listen address. All interfaces, because inside a container `localhost` is the container. `:0` picks a free port. |
| `-store` | `postgres` | `postgres` or `memory` |
| `-request-timeout` | `2s` | Deadline per database call |
| `-shutdown-timeout` | `10s` | How long in-flight requests may run after SIGTERM |
| `-startup-timeout` | `30s` | How long to wait for the database at startup |
| `-max-body-bytes` | `16384` | Largest request body |
| `-log-json` | off | JSON logs instead of text |

| Exit status | Meaning |
|---|---|
| 0 | Only for `-h`; a server runs until it's stopped |
| 1 | Startup failure (database unreachable, address in use), or a graceful shutdown that hit its deadline |
| 2 | Invalid flags or environment; every problem is listed, then usage |
| 130 / 143 | Clean shutdown after SIGINT / SIGTERM |

The readiness banner `listening on http://ADDR` goes to stdout once the socket is bound and
signal handling is installed. Logs, one line per request with its request ID, go to stderr.

## Test

```console
$ make check
go vet ./...
staticcheck ./...
golangci-lint run ./...
go test -race -count=1 ./...
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/cmd/albums	2.485s
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/cmd/democlient	1.940s
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/album	2.615s
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/api	3.959s
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/server	3.182s
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/sigctx	4.490s
ok  	github.com/brayomumo/Psychic-compendium/simple-go-rest-api/internal/store	3.892s
```

`make test-integration` runs the same suite with `REST_API_INTEGRATION=1` against PostgreSQL. The
store contract suite then runs against both stores. Each Postgres test gets its own schema through
`search_path`, so tests never see each other's rows. Without the variable, or without a reachable
database, those tests skip, so `make check` never needs Docker.

The tests check themselves by mutation. Each check below was done by reintroducing a bug in the
code and confirming the named test fails:
- **No advisory lock:** removing it from the migration makes
  `TestPostgresMigrationIsIdempotentUnderConcurrentStartup` fail. It fails on
  `duplicate key value violates unique constraint "pg_type_typname_nsp_index"` and on
  `relation "albums" already exists`, despite `IF NOT EXISTS`.
- **Request contexts tied to the signal:** wiring `BaseContext` to the signal context makes
  `TestShutdownLetsInFlightRequestsFinish` fail with `context canceled`.
- **Simple protocol allowed:** deleting the `simple_protocol` refusal from `OpenPostgres` makes
  `TestOpenPostgresRefusesTheSimpleProtocol` fail in 0.00 s with `store: database did not become
  reachable: context canceled`. The test passes an already-cancelled context, so that mutant
  fails instead of retrying the unreachable address until the test binary times out, which is what
  an earlier version of the test did.

## Failure modes

| Failure mode | What happens | How it's handled | Test |
|---|---|---|---|
| Concurrent POSTs | The first version raced on a shared slice and lost writes | Mutex-guarded memory store; the database assigns IDs | `concurrent creates are safe and unique_RegressionDataRace` (both stores), `TestConcurrentCreatesGetDistinctIDs_RegressionDataRace` |
| Client sends an `id` | The first version accepted duplicate IDs | `id` is an unknown field: `400` | `TestCreateAlbumRejectsBadRequests/client-supplied_id` |
| Same title and artist twice | Duplicate rows | `UNIQUE (title, artist)` gives `409 conflict` | `same title and artist conflicts`, `TestCreateDuplicateAlbumConflicts` |
| Empty, whitespace or over-long text; control characters; invalid UTF-8; negative or huge price; missing price | The first version stored them | `400 validation_failed`, listing every field | `TestNewDraftReportsEveryInvalidField_RegressionNoValidation`, `TestNewDraftBoundaries`, `TestCreateAlbumReportsEveryInvalidField_RegressionNoValidation` |
| Malformed, truncated, empty or multiple JSON values; wrong types; an array | The first version sent a bare 400 with no body | `400 invalid_json`, with a message naming the problem | `TestCreateAlbumRejectsBadRequests` |
| Body too large | Memory grows with the client's choice | 16 KiB cap: `413` | `TestCreateAlbumRejectsBadRequests/too_large` |
| Not JSON, or no Content-Type | Ambiguous parsing | `415` (charset parameter allowed) | `TestCreateAlbumRejectsBadRequests`, `TestCreateAlbumCharsetIsAccepted` |
| Bad path ID or query parameters | Panics or garbage in naive code | `400 invalid_id` / `invalid_query`, all problems listed | `TestGetAlbum`, `TestListAlbumsRejectsBadQueries` |
| Unsupported method or unknown route | ServeMux's plain-text 404/405 | JSON 404, and JSON 405 with `Allow` | `TestUnsupportedMethodsGetJSON405WithAllow`, `TestUnknownRouteGetsJSON404` |
| Database hangs | Requests pile up | Per-request deadline: `503` with `Retry-After` | `TestSlowStoreTimesOutAs503` |
| Database errors | Internal details leak to clients | Generic `500`; the full error goes to the log with the request ID | `TestStoreFailureDoesNotLeakDetails` |
| Handler panics | Connection dropped, nothing logged | Recovered: JSON `500`, stack logged, server keeps serving | `TestPanicIsRecoveredAsJSON500` |
| Database down at startup (container still booting) | Crash loop | Capped, jittered retries until `-startup-timeout`, then exit 1 | `TestOpenPostgresRetriesUntilTheContextEnds`, `TestRunFailsWhenTheDatabaseNeverAnswers` |
| Wrong password or missing database | Pointless retries until the deadline | Classified as permanent: fails at once | `TestOpenPostgresWrongPasswordFailsFast` |
| Malformed DSN | The error message echoes the password | A generic message | `TestOpenPostgresInvalidDSNDoesNotLeakThePassword` |
| Several replicas migrate at once | Catalog duplicate-key errors | Schema applied under `pg_advisory_xact_lock` | `TestPostgresMigrationIsIdempotentUnderConcurrentStartup` |
| A writer bypasses the API | Invalid rows | `CHECK` constraints repeat the domain rules | `TestPostgresSchemaRejectsInvalidRowsWrittenDirectly` |
| DSN requests `default_query_exec_mode=simple_protocol` | pgx would interpolate parameters into the SQL client-side, the code behind GO-2024-2605 and GO-2026-5004 | Refused before connecting: startup fails with exit 1. The four server-side modes are honoured, and metacharacters round-trip as data in each | `TestOpenPostgresRefusesTheSimpleProtocol`, `TestPostgresServerSideExecModesRoundTripMetacharacters` |
| Database down while running | Requests fail | `/readyz` returns `503` so a load balancer stops routing; `/healthz` stays `200` so the process isn't restarted | `TestProbes` |
| SIGINT / SIGTERM with requests in flight | Dropped requests | Drain; contexts stay live; exit 130 / 143 | `TestShutdownLetsInFlightRequestsFinish`, `TestSignalsShutDownCleanlyWith128PlusN` |
| A request outlives the drain | Shutdown hangs forever | Cut off at `-shutdown-timeout`: context cancelled, exit 1 | `TestShutdownTimeoutCutsOffStuckRequests` |
| Started with SIGINT ignored (shell `&`) | Ctrl+C silently does nothing | `signal.Notify` re-enables it | `TestSIGINTWorksWhenInheritedAsIgnored` |
| Bound to `localhost` in a container | Unreachable from the host | Default `:8080`; `docker-smoke` checks it from the host | `TestDefaultAddressListensOnAllInterfaces_RegressionLocalhostBind`, `make docker-smoke` |
| Port in use; bad flags or env | Unclear failure | Exit 1 with `listen failed`; exit 2 listing every problem | `TestRunFailsWhenTheAddressIsTaken`, `TestRunRejectsBadConfiguration`, `TestRunReportsEveryProblemAtOnce` |
| Request ID header with newlines or megabytes | Log injection | Only short URL-safe IDs are kept; others are replaced | `TestRequestIDIsKeptOrReplaced` |
| Image built with `RUN go run` | `docker build` never finishes | Multi-stage build with `ENTRYPOINT`; verified by building | `make docker-smoke` |

## What the first version got wrong

The first version was the Go tutorial's albums API on Gin, with a Dockerfile and a compose file
added. Every claim below was reproduced against that code (commit `ade3484` imports it unchanged).

1. **The Docker build could never produce an image.**
   - On the pinned `golang:1.16-alpine` it failed outright: Gin's dependencies need Go 1.17
     (`undefined: unsafe.Slice`, `note: module requires Go 1.17`).
   - With a current image it was worse. `RUN go run main.go` starts the server *during the build*
     (`Listening and serving HTTP on localhost:8080`), and `docker build` waited forever. Killed
     after 150 s.
   - *Lesson:* `RUN` executes at build time and `CMD`/`ENTRYPOINT` at run time. Build images in
     stages, and pin a toolchain that satisfies `go.mod`.
2. **It listened on `localhost:8080`.** Inside a container that is the container's own loopback,
   so even a working image would have been unreachable from the host. *Lesson:* servers bind all
   interfaces (`:8080`), and the deployment decides exposure. Here, compose publishes only on
   `127.0.0.1`.
3. **compose was miswired.**
   - `expose: "8080:8080"` publishes nothing: `expose` takes container ports, and `ports` is what
     publishes.
   - Postgres was published on `0.0.0.0:5432` to the whole network.
   - Postgres had no healthcheck, and `depends_on` waited only for the container to start.
   - The app never connected to the database at all.
4. **A data race lost writes.** Handlers appended to a package-level slice from concurrent request
   goroutines. Under `-race`, 20 concurrent POSTs produced **7 race reports**, and of 20 POSTs that
   all reused `"id":"1"`, every one was accepted as a duplicate while **3 were lost** outright.
   *Lesson:* shared mutable state in handlers needs synchronization, and the store should own it.
5. **Clients chose IDs** and nothing checked uniqueness, so `GET /albums/1` became ambiguous.
6. **There was no validation.** `{"title":""}` and `{"id":"9","price":-5}` were both answered
   `201 Created`.
7. **Money was `float64`**, so `56.99` can't be stored exactly. *Lesson:* use integer minor units.
8. **Errors weren't API responses.** Malformed JSON got a `400` with an empty body and no
   Content-Type. The client couldn't tell what was wrong. *Lesson:* every response, errors
   included, is JSON in one shape.
9. **Nothing was bounded or stoppable.**
   - No server timeouts: one slow client could hold a connection forever.
   - No body limit.
   - No shutdown handling: SIGTERM killed requests mid-flight.
10. **Smaller issues.**
    - `IndentedJSON` pretty-printed every response, which costs bandwidth for no program's
      benefit.
    - The Makefile's `go get .` mutated `go.mod`.
    - The module was named `library_manager`.
    - The README listed the route as `albums/:id`, missing its leading slash.

## When to use what

- **Standard library (`net/http`, Go 1.22+):** method and wildcard routing, middleware as plain
  `func(http.Handler) http.Handler`, `encoding/json`, and `httptest` for tests. That covers
  this API with zero routing dependencies, and the code reads as ordinary Go.
- **Gin, Echo, chi:**
  - Struct-tag binding and validation (`binding:"required"`), a middleware ecosystem (CORS, rate
    limiting, auth) and route groups.
  - Before Go 1.22 they also gave you method and parameter routing.
  - Reach for one when those conveniences save real code across many endpoints. Know that
    tag-based validation is easy to get subtly wrong: zero values versus missing fields, which
    this prototype handles with `*int64`.
- **chi** is the closest to the standard library: it builds on `net/http` types, so moving
  between the two is cheap.

## Trade-offs and limits

- **Vulnerabilities: none known, and `make vulncheck` enforces it.** With Go 1.27.1, pgx 5.11.0
  and `golang.org/x/text` 0.42.0, govulncheck v1.8.0 reports `No vulnerabilities found.` A new
  advisory now fails the target, and the fix is a version bump. It used to be informational: on
  the repo's old Go 1.23.4 toolchain, govulncheck 1.1.4 reported 37 reachable vulnerabilities.
  - **35 were in the Go standard library** (`crypto/tls`, `crypto/x509`, `net/http`, `net/url`,
    …). Go 1.23 no longer gets security fixes; moving to a supported toolchain cleared them.
  - **[GO-2026-5004](https://pkg.go.dev/vuln/GO-2026-5004)** in pgx 5.7.6: SQL injection through
    dollar-quoted literals in client-side parameter interpolation, fixed in pgx 5.9.2.
  - **[GO-2026-5970](https://pkg.go.dev/vuln/GO-2026-5970)** in `x/text` 0.24.0: an infinite loop
    on invalid input, reachable only through pgx's password normalization. Fixed in 0.39.0.

  pgx 5.7.6 was the newest release that built with Go 1.23. That's why the fix had to be
  repo-wide (toolchain, linters and CI together) rather than a dependency bump.
  `go.mod` now says `go 1.26.0`. That's the oldest supported Go, and also the minimum that `x/text`
  0.42.0 and `x/sync` 0.23.0 declare. pgx 5.11.0 alone needs 1.25.
- **The simple-protocol refusal outlived the bug it mitigated, on purpose.** With pgx 5.7.6,
  forcing the extended protocol was the mitigation for GO-2026-5004. pgx 5.11.0 fixes that bug, and
  the refusal stays as defence in depth:
  - The client-side sanitizer behind `default_query_exec_mode=simple_protocol` has had two SQL
    injection advisories ([GO-2024-2605](https://pkg.go.dev/vuln/GO-2024-2605) in pgx v4 and
    GO-2026-5004 in v5). Binding parameters on the server rules out the whole class instead of
    trusting the next fix.
  - It costs nothing. pgx documents `exec` as behaving like `simple_protocol` for applications,
    and recommends preferring it. It also works behind poolers without prepared-statement
    support, such as PgBouncer in transaction mode.

  What changed is the mechanism. The first mitigation silently overwrote whatever mode the DSN
  asked for with `cache_statement`, so an operator who set `exec` for PgBouncer didn't get it. Now
  only `simple_protocol` is rejected, with an error that names `exec`. The other four modes are
  honoured as written.
- **No authentication, authorization, TLS or rate limiting.** Put this behind a gateway that
  provides them, or add middleware. Any write endpoint exposed beyond localhost needs at least
  auth and rate limiting.
- **Only create, read and list.** Update and delete would add `PUT`/`PATCH` with optimistic
  concurrency (`ETag` / `If-Match`), plus `DELETE`. POST is not idempotent: a client that retries
  after a lost response may get `409` for its own album. `Idempotency-Key` headers are the
  standard fix.
- **The schema is applied at startup.** That's fine for one table. Real schema evolution needs
  versioned migrations (goose, golang-migrate) run as a deploy step, not by every replica.
- **No drain delay before shutdown.** Behind a load balancer you'd fail `/readyz` and wait a few
  seconds, until the balancer stops routing, before calling `Shutdown`. Here `Shutdown` starts at
  once, which suits a single instance.
- **Logs only.** No metrics or traces. The request ID is the hook for adding OpenTelemetry later.
- **Images are pinned to tags, not digests:** `golang:1.27.1-alpine` (the same patch release as
  go.mod's `toolchain` line), `postgres:17.6-alpine`, and `distroless/static-debian12`. Pinning
  by digest makes builds fully reproducible.
