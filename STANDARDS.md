# Engineering standard

This repo is an ideation lab. Each prototype exists to answer one question about a concept, and is
only worth keeping if:

1. it demonstrably works,
2. it fails safely in every way we can think of, and
3. its README teaches the concept correctly.

Wrong-but-running is worse than broken, because it teaches the wrong lesson.

The bar: code that a principal engineer would approve in review, a design that a senior staff engineer
can't poke holes in, and verification thorough enough that nothing the README claims is unproven.

This file is the contract. Ready-to-copy files that implement it live in [`templates/`](templates/).
If this file and a template disagree, this file wins; fix the template in the same change. Every
convention a prototype adopts belongs here. Undocumented conventions don't count.

**Contents**
[Definition of done](#definition-of-done) ·
[1 Layout](#1-repository-layout) ·
[2 Git](#2-git-workflow) ·
[3 Design](#3-design-principles) ·
[4 Python](#4-python) ·
[5 Go](#5-go) ·
[6 C and FFI](#6-c-and-ffi) ·
[7 Makefiles](#7-developer-interface-makefiles) ·
[8 Testing](#8-testing) ·
[9 README](#9-readme-structure) ·
[10 Runtime](#10-runtime-conventions) ·
[11 Services](#11-external-services-and-containers) ·
[12 Patterns](#12-proven-patterns) ·
[13 Performance](#13-performance-claims) ·
[14 New prototype](#14-starting-a-new-prototype) ·
[15 Changing this file](#15-changing-this-standard) ·
[Tool versions](#appendix-tool-versions)

## Definition of done

A prototype change is done when every box is ticked:

- [ ] The README states the question the prototype answers and follows [section 9](#9-readme-structure).
- [ ] `make check` passes from a clean tree (`make clean` first, `git status` clean).
- [ ] `make run` exits 0 on its own with default settings.
- [ ] SIGINT and SIGTERM during a long run exit promptly with 130 or 143, leaving no stray processes,
      threads, containers or temp files. The test that proves it was launched correctly (see
      [section 8](#signal-tests)).
- [ ] Every known bug has a regression test, and every row of the failure-modes table names its test.
- [ ] Every performance or reliability claim in the README is backed by a test or a `make` target.
- [ ] No build outputs, binaries, virtualenvs, caches or secrets are committed.
- [ ] Commits follow [section 2](#2-git-workflow), and new conventions are added to this file.

## 1. Repository layout

- **One directory per prototype**, fully self-contained: its own `Makefile`, `README.md`, tool config,
  dependency pins and `.gitignore`. Prototypes never import each other. When two need the same helper,
  each keeps its own copy.
- **No `go.work`.** Every Go module is independent, and Makefiles export `GOWORK=off` so that a
  workspace file in a parent directory can't leak in.
- **The root holds only repo-wide files:** `README.md` (the index), `STANDARDS.md`, `CLAUDE.md`,
  `Makefile`, `templates/`, `.editorconfig`, `.gitignore`, `LICENSE`, and `.github/`. The root
  `Makefile` discovers prototypes as any top-level directory containing a `Makefile`.
- **Directory names:** existing names stay for history (e.g. `lower_with_c_and_python`, `Piper`). New
  prototypes use `kebab-case`.
- **Whitespace** is set by [`.editorconfig`](.editorconfig) for every file type: UTF-8, LF, a final
  newline, no trailing whitespace except in Markdown, tabs for Go and Makefiles, 4 spaces for
  Python, 2 for C, YAML and JSON. Formatters (ruff, gofmt) have the last word in their languages.
- **Polyglot prototypes** (e.g. a Go server with a Python client) use one subdirectory per language,
  each with its own toolchain config. The prototype's `Makefile` orchestrates them.

## 2. Git workflow

- **One branch per prototype change**, named `<type>/<prototype>` (e.g. `fix/piper`,
  `feat/bulkhead`). Repo-wide changes use `chore/<topic>`. `main` is protected, so everything merges
  through a pull request, and all review conversations must be resolved first.
- **[Conventional Commits](https://www.conventionalcommits.org/):** `<type>(<scope>): <summary>`.
  - Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `chore`.
  - Scope: the prototype's short name (`piper`, `go-concurrency`, `todo-cli`, `c-python`, `pub-sub`,
    `python-coroutines`). Commits that touch only root files use `build:` or `chore:` with no scope.
  - Summary in the imperative, 72 characters or fewer. The body explains *why*.
- **Trailer:** every commit message ends with this line, after a blank line:

  ```
  Co-Authored-By: God's of Nature
  ```

- **Commit order for a rewrite:**
  1. A rename-only commit, so `git log --follow` keeps history.
  2. One fix commit per module, with its tests.
  3. Build files.
  4. Docs.

  Each commit should pass `make check` on its own where practical. A removed file is deleted in the
  commit that replaces it.
- **Fixups:** use `git commit --fixup <sha>`, then `git rebase --autosquash main` (non-interactive,
  git 2.44 or newer). Interactive rebases are not used.
- **Importing an idea branch:**
  - Start a new branch from `main`. Import the original files verbatim in one commit.
  - Take the author from the source commit (`git show -s --format='%an <%ae>' <sha>`), and pass it
    with `--author`. Name the source branch and commit in the message.
  - Verify the import is verbatim by comparing blob hashes (`git rev-parse <sha>:<path>`). Only this
    commit may fail lint.
  - Put the fixes on top, and leave the original branch untouched.
- **Stacked work** (a branch built on another unmerged branch) gets a pull request whose base is the
  parent branch. GitHub retargets it once the parent merges.
- **Merge order:**
  1. Prototype branches.
  2. `chore/*` branches that change root files.
  3. CI.

  The root `make check` and CI assume every prototype meets this standard.
- **Never commit:** build outputs, binaries, `.so`/`.dylib` files, virtualenvs, caches, coverage
  output, `.DS_Store`, `.env` files or credentials. Each prototype's `.gitignore` covers its own outputs
  with anchored paths (`/bin/`). The root `.gitignore` covers repo-wide noise.

## 3. Design principles

- **Smallest correct design.** Build the smallest thing that demonstrates the concept *correctly*. No
  frameworks, no speculative abstractions, no configuration nobody uses. Use the standard library
  only, unless the concept *is* the dependency (pika for RabbitMQ, gRPC, a NATS client).
- **Keep the question, fix the answer.** When a premise turns out wrong, keep the question it was
  asking, answer it honestly, and record the correction in the README.
- **Ownership and shutdown.** Every process, thread, goroutine, coroutine, connection, file and
  container has exactly one owner and a defined shutdown path. Programs terminate on their own when
  the work is done, and exit cleanly on SIGINT and SIGTERM. No orphans, no leaks, no hangs.
- **Bounded resources.** Queues, buffers, in-flight work and user-supplied sizes are bounded (exported
  `Max*` / `MAX_*` constants), and backpressure is explicit. An unbounded queue is a memory leak with
  a delay.
- **Validate at the boundary.** Flags, environment variables, stdin, files and network input are
  validated where they enter. Report every problem at once, not just the first. Library code returns
  errors (or raises), and only the entry point decides to exit.
- **No sleeps as synchronization.** Use channels, events, joins, futures, barriers or condition
  variables. Sleeps are allowed only to simulate work, and must be configurable.
- **Retries** use capped exponential backoff with jitter, retry only errors that can succeed later,
  and give up or surface after a bound (see the AWS Builders' Library, *Timeouts, retries and backoff
  with jitter*). Authentication and authorization failures are fatal, not retried.
- **Determinism where it matters.** Randomness is seedable (a `-seed`/`--seed` flag, default 1). Tests
  never depend on timing luck.
- **Sentinels are private `enum.Enum` members** (or unexported values in Go) compared by identity,
  never in-band data values. Anything a caller can send is data.
- **Extract on the second duplicate** within a prototype (e.g. a shared `cli.py`), never across
  prototypes.
- **Thread ownership.**
  - Every thread is named and joined by exactly one owner.
  - Worker exceptions are recorded under a lock and re-raised once in the owner, with `add_note()`
    naming the thread. A secondary error is logged with its traceback, never dropped.
  - Blocking waits on a peer use `timeout=` loops that re-check the peer is alive. The interval is a
    named constant, documented as never delaying data.
- **Two shutdown modes.** A normal `close()` drains, and any other exception (Ctrl+C included) aborts
  after the current item. Where both matter, a context-manager helper encodes the choice.
- **Paired resources use `try`/`finally`** (`enable()`/`disable()`, `start()`/`stop()`). On Python
  3.12+, a profiler left enabled blocks every later profiler.
- **Never report success on a partial result.** If work can go missing without cancellation, that is
  a bug: fail loudly with exit 1.
- **Comments explain why, not what.** Public functions, types and packages carry doc comments. No dead
  code, no commented-out code, no debug prints. A suppression always names the rule and gives a
  reason (see the language sections).

## 4. Python

- **Version and style.**
  - Target 3.11+ and verify on the newest local interpreter (also 3.11 when cheap: `uv run
    --python 3.11`). Run it as `python3`, never `python`.
  - Follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html): 80
    columns, Google-style docstrings (`Args:`, `Returns:`, `Raises:`, `Yields:`), full type hints, and
    `__all__` in library modules.
- **Tool config.** Copy [`templates/pyproject.toml`](templates/pyproject.toml) verbatim. Settings
  specific to one prototype go *below* the canonical block, with a comment explaining why. The only
  accepted override so far is a scoped `[[tool.mypy.overrides]]` with `ignore_missing_imports` for
  an untyped dependency. The gate is ruff check, ruff format `--check`, and `mypy --strict`, with
  versions pinned in the Makefile (see the [appendix](#appendix-tool-versions)). Inline suppressions
  are written `# noqa: CODE - reason`.
- **Layout.**
  - Small prototypes keep modules flat in the prototype directory.
  - Multi-module apps use a package (`pubsub/`) run with `python -m <pkg>.<module>`.
  - Tests live in `tests/test_*.py` with no `__init__.py`, and run from the prototype directory with
    `python3 -m unittest discover -s tests -v`.
  - Shared test helpers go in files that don't match `test_*.py` (`tests/fakes.py`,
    `tests/procs.py`).
- **Entry points.** Each runnable module exposes `def main(argv: Sequence[str] | None = None) -> int`
  and ends with `raise SystemExit(main())`. Demos without options do `del argv`.
- **CLI validation.** Use argparse `type=` functions (e.g. `positive_int`, `non_negative_int`,
  `non_negative_float`) that raise `ArgumentTypeError("must be in LOW..HIGH, got X")`. Reject NaN and
  infinity, and cap every flag that controls resource use with a module constant.
  - Show defaults by writing `(default: %(default)s)` into each help string.
    `ArgumentDefaultsHelpFormatter` prints "(default: None)".
  - Short metavars (`N`, `MS`). Verbosity is `-v` for INFO and `-vv` for DEBUG.
  - A prototype with several entry points may share one private `_cli.py`: exit constants, logging
    setup, validators, and a `run_main(body)` that maps signals to 130/143.
- **Errors.** Define a module exception hierarchy. Custom exceptions subclass the closest built-in, and
  use mix-ins where it helps callers: `SumOverflowError(SumError, OverflowError)`,
  `LibraryLoadError(OSError)`, `BrokerClosedError(RuntimeError)`.
- **Logging.**
  - Configure `logging` once in the entry point, writing to stderr. Name entry-point loggers
    explicitly, because `__name__` is `__main__` under `python -m`.
  - Formats: `%(levelname)s %(message)s` for demos; `%(asctime)s %(levelname)-7s %(name)s:
    %(message)s` for long-running services. Silence noisy third-party loggers unless the level is
    DEBUG.
  - Levels:
    - `logger.exception`: a component failed and was isolated.
    - WARNING: data was dropped.
    - INFO: normal lifecycle events.
- **Multiprocessing.** Code must work under `spawn` (the macOS default), `fork`, and `forkserver` (the
  Linux default since 3.14), and the tests cover all three. Anything passed to a child must pickle.
  Never rely on state inherited through fork by accident, and remember that fork copies, it doesn't
  share.
- **Third-party dependencies** use uv:
  - Declare them in a `[project]` table at the top of `pyproject.toml`, with the canonical block
    unchanged below it.
  - Commit `uv.lock`, and keep `.venv/` gitignored.
  - Makefiles run `uv sync --locked` and `uv run --locked`, and export `UV_PYTHON := $(shell command -v
    $(PYTHON))` so the venv uses the `python3` on PATH.
  - Don't use `requirements.txt`.
- **ctypes / FFI:** see [section 6](#6-c-and-ffi).

## 5. Go

- **Versions and module.**
  - Only supported Go releases (the two newest minor versions; check <https://go.dev/dl/>). `go.mod`
    says `go 1.26`, the oldest supported release, or the minimum a dependency requires if that's
    higher, with a comment naming the dependency.
  - `go.mod` also pins the build toolchain: `toolchain go1.27.1`, the newest patch. Go downloads
    pinned toolchains on first use (`GOTOOLCHAIN=auto`), so nothing is installed globally. Makefiles
    export `GOTOOLCHAIN` from that line, so linters are built with the same toolchain they analyse.
  - Dependencies are kept at their latest releases, and `make vulncheck` (govulncheck) must report no
    vulnerabilities. When it doesn't, bump the toolchain or the dependency; don't argue with it.
  - Module path: `github.com/brayomumo/Psychic-compendium/<dir>`.
  - Standard library only, unless the concept is the dependency.
- **Style.** Follow the [Google Go Style Guide](https://google.github.io/styleguide/go/) and [Effective
  Go](https://go.dev/doc/effective_go):
  - MixedCaps names.
  - Doc comments on every package and exported identifier (enforced by revive).
  - Errors wrapped with `%w`, sentinel errors matched with `errors.Is` and wrapped with detail
    (`fmt.Errorf("%w: %d", ErrNotFound, id)`).
  - `context.Context` as the first parameter of anything that blocks.
  - `internal/` for packages that aren't a public API.
  - Interfaces are declared by the consumer, and only when there is a second implementation (a test
    fake counts).
- **Layout.** Single-package programs keep `main.go` at the module root, with logic in
  `internal/<pkg>`. Multi-binary or multi-package apps use `cmd/<binary>/main.go` (the binary is named
  after its `cmd` directory) plus `internal/<pkg>`.
- **Entry point.**
  - `main()` only wires process globals and calls `os.Exit(run(...))`.
  - `run` takes the arguments and the standard streams (plus a context when `main` owns the signals)
    and returns the exit code, so tests can drive it.
  - Exit codes are named constants: `exitOK`, `exitFailure`, `exitUsage`.
- **Flags.**
  - Use `flag.NewFlagSet(name, flag.ContinueOnError)` writing to stderr.
  - `-h` exits 0. Parse errors, stray positional arguments and validation failures exit 2 with usage.
  - A library takes a `Config` with `Validate() error`, which reports every problem via `errors.Join`.
    `Run` re-validates and wraps failures as `invalid config: %w`.
- **Signals.** Use a small `notifyContext()` helper built on `context.WithCancelCause` that records the
  received signal as the cause and calls `signal.Stop` after the first signal, so a second Ctrl+C kills
  immediately. Exit with `128 + signal`. Plain `signal.NotifyContext` can't tell SIGINT from SIGTERM,
  so it isn't enough.
- **Time and randomness.**
  - Domain functions take `now time.Time`. Edges take `Now func() time.Time` and a `*time.Location`.
  - Store times in UTC and display them in local time, using one layout constant built from the
    reference time `Mon Jan 2 15:04:05 MST 2006` (or `time.DateTime` / `time.DateOnly`).
  - Seeded randomness uses `math/rand/v2` with one PCG source per goroutine, seeded with `(seed,
    index)`.
- **Diagnostics.** Use `log.New(stderr, "<prog>: ", 0)` for CLIs, or `log/slog` for services. Per-event
  logging is opt-in behind `-v`. Interactive rejections go to stderr prefixed `error: `, and echoed
  user input uses `%q`.
- **Tool config.** Copy [`templates/.golangci.yml`](templates/.golangci.yml) (golangci-lint v2 format)
  verbatim. It deliberately leaves out the `comments` exclusion preset, so revive really does require
  doc comments on exported identifiers. golangci-lint v1 suppressed that check by default. Write
  suppressions as `//nolint:<linter> // <reason>` on their own line above the declaration.
- **The gate:**
  - the pinned toolchain's gofmt (`go run cmd/gofmt -l .` must list nothing; a bare `gofmt` on PATH
    may be older);
  - `go vet`, staticcheck, golangci-lint;
  - `go test -race -count=1 ./...`.

  Linters run pinned through `go run <pkg>@<version>`, as [`templates/Makefile.go`](templates/Makefile.go)
  shows, so no machine needs them installed. `make vulncheck` runs separately, because it needs network
  access to the vulnerability database.

## 6. C and FFI

- **Style:** C11, 2-space indent (Google style).
  - Prefix public names with the library name (`sum_range`).
  - Status codes are an `enum <lib>_status` of prefixed UPPER_SNAKE constants with explicit values,
    marked "never renumber", and returned as `int` (an enum's size is implementation-defined).
  - Callbacks are typedef'd `<lib>_<name>_fn`, take a trailing `void *user_data`, and return nonzero to
    stop. Out-parameters come last.
  - Headers have an `<LIB>_H` include guard and `extern "C"`, and document every parameter's contract.
    Internal helpers are `static`.
  - Libraries never print and never abort. Every outcome is a status code.
- **Arithmetic:** use fixed-width integer types at API boundaries. Signed overflow is undefined
  behaviour, so check *before* the operation; the compiler may delete an after-the-fact check.
- **Build:**
  - `STRICT_CFLAGS = -std=c11 -Wall -Wextra -Wpedantic -Werror`, plus `-Wconditional-uninitialized` on
    clang, followed by `CFLAGS ?= -O2`.
  - `clang --analyze -Xclang -analyzer-werror` runs in `lint`. `-Wall -Wextra` and UBSan both miss
    uninitialised reads, while the analyzer and `-Wconditional-uninitialized` catch them.
  - Outputs go in a gitignored `build/`. Recipes use `mkdir -p` rather than a directory target, which
    would clash with the phony `build`.
- **Sanitizers:** a native C test built with `-fsanitize=address,undefined -fno-sanitize-recover=all`
  is part of `make check`.
  - Apple clang 17 on macOS 26 hangs in ASan at startup, so the Makefile probes ASan with a timeout
    and falls back to UBSan with a warning.
  - CI sets `REQUIRE_ASAN=1`, which turns that fallback into a failure. Strictness knobs are named
    `REQUIRE_<THING> ?= 0`, accept only `0` or `1`, and exit 2 on anything else.
  - On Linux, `make test-msan` (clang only) runs the native test under MemorySanitizer, the one
    sanitizer that catches uninitialised reads directly. Platform-only targets exit 2 elsewhere, with a
    message naming `CC` and the OS. The Makefile gates them with `UNAME_S := $(shell uname -s)` and
    `HAS_<FEATURE>` flags.
  - Show that each sanitizer works by injecting one bug per sanitizer into a scratch copy, and record
    the results in a README table. A demo row that couldn't run its tool reports that failure, never
    "not detected".
  - CI uses `CC=clang`, installs `libclang-rt-<ver>-dev` (without it, ASan can't link on Ubuntu) and
    `llvm-<ver>` (for `llvm-symbolizer`, so reports show file and line), and puts
    `/usr/lib/llvm-<ver>/bin` on `PATH`. LeakSanitizer stays on. Under emulation (e.g. `act` on Apple
    Silicon), LSan and MSan can't start, so set `ASAN_OPTIONS=detect_leaks=0` there.
  - Verify Linux behaviour locally in `ubuntu:24.04`, on a copy of the prototype so root-owned build
    outputs stay out of the worktree. Treat native-architecture results as the evidence, and label
    emulated results with their limits.
- **ctypes:**
  - Declare `argtypes` and `restype` for every function.
  - Range-check integers before converting, because `c_int64(2**64 + 5)` is silently `5`.
  - Keep each `CFUNCTYPE` object referenced for as long as C may call it.
  - Exceptions can't unwind through C, so the trampoline captures them, returns "stop", and the wrapper
    re-raises them after the call.
  - While C may call back into Python, defer SIGINT (only when Python's default handler is installed)
    and re-deliver it after the call. Otherwise Ctrl+C is reported as unraisable and lost.
  - Resolve the library path relative to `__file__`, with the platform suffix (`.dylib` / `.so`), and
    give a clear "run make" error when it's missing.

## 7. Developer interface (Makefiles)

Every prototype's `Makefile` lists all non-file targets in `.PHONY` and provides:

| Target | Contract |
|---|---|
| `make run` | Runs the demo with sensible defaults and **terminates on its own**, exit 0. Interactive programs exit on end of input. |
| `make test` | Runs the automated tests (unit tests; see `test-integration`). |
| `make lint` | Formatters (in check mode), linters, type checkers and static analysis. |
| `make check` | `lint`, then `test`. This is the merge gate. It must pass without external services. |
| `make clean` | Removes every build output and cache the Makefile can create. Never user data. |

Optional targets, used with these names when needed:

| Target | When to add it |
|---|---|
| `make build` | The prototype compiles something. |
| `make demo` | An interactive program needs a scripted session. |
| `make bench` | The README makes a performance claim. |
| `make vulncheck` | Every Go prototype (govulncheck; needs network, so not part of `check`). |
| `make test-integration` | Some tests need an external service. |
| `make broker-up` / `make broker-down` | The prototype uses a service container (see [section 11](#11-external-services-and-containers)). |
| `make sync` | The prototype has uv dependencies. |
| `make help` | The Makefile has many variables. |

**Variables:**
- UPPER_SNAKE_CASE with `?=` defaults: `PYTHON ?= python3`, `RUFF ?= uvx ruff@<version>`,
  `MYPY ?= uvx mypy@<version>`, `GO ?= go`, `STATICCHECK ?= staticcheck`,
  `GOLANGCI_LINT ?= golangci-lint`, `CC ?= cc`, `BIN ?= bin/<name>`, `ARGS ?=` (extra flags passed
  through to the program).
- Demo parameters get their own variables (`CONSUMERS ?= 4`).
- Internal constants use `:=` and aren't documented as knobs.
- The README documents every knob and its default, with names that match exactly.

**Recipes** fail fast. Avoid `|| true` unless the failure is genuinely irrelevant, and comment why.
Never `rm -rf` a variable that could be empty.

The root `Makefile` runs every prototype's `make check`. Use `make -k check` to see every failure at
once, `make -j check` to run them in parallel, and `make list` to show what it found.

## 8. Testing

**Coverage: "validated" means all of these.**
- **Regression tests.** Every bug found gets a test named after it: `test_<bug>_bug_<expected>` in
  Python, `Test<Behaviour>_Regression<Bug>` in Go, or a name that states the behaviour plus a
  `// Regression:` comment.
- **Behaviour.** The happy path, and termination (the program finishes on its own).
- **Edge-case inputs:** zero, negative, huge, malformed, empty, NaN/infinity, EOF, and closed output.
- **Cancellation.** Cancellation mid-run (signal or context) is prompt and leak-free.
- **Component failure.** Each component's failure is handled: a producer crashes, a consumer crashes,
  a worker raises, the broker goes down, the disk is unwritable, a file is corrupt.
- **Speed and determinism.** The suite runs well under 30 seconds, with no sleeps as synchronization.
- **Claims are proven.** Each claim in the README is backed by a test or a reproducible `make`
  target.

**Techniques this repo has proven.**
- **Prove concurrency structurally, never by wall-clock time.** Use peak-in-flight counters,
  `asyncio.Barrier` / `sync.WaitGroup` sized to the worker count with a timeout guard, and exact
  multisets of processed IDs.
- **Check for leaks.**
  - Goroutines: poll `runtime.NumGoroutine()` back to its baseline within a deadline, in
    non-parallel tests only.
  - Python threads and processes: check `threading.active_count()`,
    `multiprocessing.active_children()`, and `pgrep` in process tests.
- **Guard anything that could hang** with a deadline (`runWithDeadline`, `asyncio.timeout`,
  `subprocess` timeouts), so a regression fails instead of blocking CI.
- **Synchronise on output markers**, such as a readiness banner printed after the signal handlers are
  installed. Timeouts are only a guard against hangs.
- **Real-process tests:**
  - Go: re-exec the test binary through `TestMain` with a `<PROG>_TEST_RUN_MAIN=1` environment
    variable, under `//go:build unix`, with `GORACE=atexit_sleep_ms=0` for children.
  - Python: launch with `subprocess.Popen` and close pipes with `communicate()`.
- **Python test base class.** Long or concurrent suites use a `WatchdogTestCase` (`tests/support.py`).
  It sets a per-test `faulthandler` watchdog (60 s, `exit=True`), turns warnings into errors, and
  resets SIGINT to `default_int_handler`, restoring all three afterwards. Helpers that poll only
  observe; they never synchronise.
- **Start-method contract tests.** Put the shared tests in a holder class (`class Contract: class
  Pipeline(WatchdogTestCase)`) so unittest doesn't collect the base. Subclass it once per start method
  (`SpawnPipelineTest`, `ForkPipelineTest`, `ForkserverPipelineTest`). Picklable test-only callables
  live in `tests/fixtures.py`, so spawned children can import them.
- **Process-group tests.**
  - Start the CLI with `subprocess.Popen(start_new_session=True)`, and emulate Ctrl+C with
    `os.killpg(pgid, SIGINT)`, which reaches the whole group like a terminal would.
  - Assert the exit code, the handler's log line, and that no `Traceback` was printed.
  - Prove the group is empty before any cleanup kill: `os.killpg(pgid, 0)` must raise
    `ProcessLookupError`.
- **Plant the ground truth.** A prototype about finding problems (profiling, leaks, races) plants
  known ones and asserts the tools find them. Locate them by marker comment (`# LEAK:`) or
  `inspect.getsourcelines`, never by hard-coded line numbers.
- **Cold caches.** A regression test that depends on caches that last the whole process (compiled
  regexes, imports) runs in a fresh interpreter: `subprocess.run([sys.executable, "-c", ...])`.
- **Nested timeouts.** Subprocess timeouts (30 s) stay below the faulthandler watchdog (60 s), so a
  hang fails one test instead of killing the run.
- **Testing signal handlers in-process.** Mock `os._exit` with `side_effect=SystemExit`, because the
  real one never returns. Mock `os.write` to keep the output clean.
- **Version-dependent tests** use `skipUnless` / `skipIf` with a small `python_at_least(minor)` helper,
  and are verified on the oldest supported version via `uv run --python 3.11`.
- **Guard calls that are expected to fail or block.** Run them on a guarded thread (e.g.
  `expect_error(fn, exc)` in `tests/support.py`) with one named upper bound (`GUARD_S`), so a
  regression fails instead of hanging the main thread. Register every resource's cleanup
  (`addCleanup` or `finally`) *before* any assertion that could fail. Non-daemon threads stuck in a
  simulated hang otherwise block interpreter exit.
- **Golden tests** pin every on-disk or on-the-wire format.
- **Check the tests with mutation.** Before reporting, reintroduce key bugs in a scratch copy and
  confirm the tests fail cleanly, without hanging. Don't commit this check.
- **Keep integration tests separate:** `tests/test_integration.py` (or a build tag in Go), skipped
  unless `<PROTO>_INTEGRATION=1` is set and the service answers. Each test uses unique resource names,
  and they run via `make test-integration`.
- **Stress concurrency before reporting.** Go: `go test -race -count=10 -cpu 1,2,8 ./...`. Python: 50
  sequential runs under `-X dev`, plus a run with 8 suites in parallel.
- **A mutant that hangs the suite is a test-design defect.** Fix the test so it fails instead. The
  watchdog base class exists for this. Mutation scripts give each mutant a generous timeout and
  report HUNG separately from CAUGHT.
- **Regenerated sample output in a README matches exactly,** including counts such as "10 files
  already formatted".
- **Test seams without API changes.** Swap collaborators with `mock.patch.object(queue, "Queue",
  factory)` and similar, rather than adding test-only parameters.
- **Trigger Ctrl+C tests from a structural event** (e.g. the producer is blocked), never a timer:
  `os.kill(os.getpid(), SIGINT)` from inside the code under test.
- **Simulate CPU work with `time.thread_time()`.** Wall-clock spinning fakes parallel speedups under
  the GIL. Simulate I/O with `time.sleep`.

### Signal tests
- **A process started with `&` from a non-interactive shell begins with SIGINT ignored (`SIG_IGN`).**
  Python keeps it ignored and never raises KeyboardInterrupt, and multiprocessing children inherit the
  same disposition. A Ctrl+C test launched that way silently tests nothing. Go's `signal.Notify`
  re-enables the signal, but Python does not.
- **Launch signal tests correctly:** use `subprocess.Popen`, or `set -m` in a shell. To control the
  child's starting disposition, use an exec launcher, never `preexec_fn`, which is unsafe in threaded
  programs (ruff PLW1509):
  - `sh -c 'trap "" INT; exec "$0" "$@"' <cmd>` to start it with SIGINT ignored;
  - `python3 -c 'import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL);
    os.execv(sys.argv[1], sys.argv[1:])' <cmd>` to reset it. Then assert that the handler path actually ran: its log line, the
  final state, and exit code 130 or 143.
- **Programs that must survive an inherited `SIG_IGN`** install their handlers explicitly, and a test
  starts them with SIGINT ignored to prove it, e.g. `sh -c 'trap "" INT; exec "$0" "$@"' <cmd>`.
- **In-process Python signal tests** use `threading.Timer` plus `os.kill`, and check the exit code and
  that the previous handler was restored.
- **Wait for the readiness banner before signalling, never a fixed delay.** A signal that arrives
  before the handlers are installed is lost or kills by default, and macOS can delay the first
  launch of a newly built binary by seconds. Verification scripts SIGKILL after a deadline so they
  can't hang.
- **Every claim about a second signal gets a test.** For example, prove a deliberately hung shutdown
  dies on the second SIGINT, and mutation-check that the test fails without the mechanism.
- **Two signals sent back to back can merge into one** under POSIX. Wait for the first signal's notice
  line before sending the second.

## 9. README structure

One `README.md` per prototype, in plain, direct prose: no marketing, no emojis, no filler. Start from
[`templates/README.md`](templates/README.md). The sections, in order:

1. **`# <Name>`**: one paragraph that opens with **the question this prototype answers**.
2. **`## Concept`**: the idea, taught correctly and concisely, with the mechanics a reader must
   understand. Link primary sources.
3. **`## Design`**: components, data flow, ownership and shutdown. Use a mermaid diagram when it helps.
   Diagrams must match the code, so replace any that don't.
4. **`## Run`** and **`## Test`**: exact commands with real, abbreviated output, a table of Make
   targets and variables with their defaults, and an exit-status table.
5. **`## Failure modes`**: a table with the columns **Failure mode | What happens | How it's handled |
   Test**. Every test named must exist (check with a script). "By construction" is allowed only with
   a one-line justification.
6. **`## What the first version got wrong`**: each original bug or misconception, why it happened, and
   the lesson. Be specific and honest. When files were renamed, include an old → new mapping. Omit
   this section for brand-new prototypes.
7. **`## Trade-offs and limits`**: what the design does *not* prove, when you would not use it, and what
   you would do next.

Allowed extra sections:
- **`## Benchmark`**, after Test, when the prototype makes performance claims.
- **`## When to use what`**, before Trade-offs, when the concept has competing alternatives (for
  example generators vs asyncio vs threads). Label results that depend on a
toolchain with that toolchain (for example "Apple clang 17").

## 10. Runtime conventions

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime failure, including a missing build artifact or local I/O failure (e.g. closed stdout) |
| 2 | Usage or configuration error: bad flag, invalid env var, stray argument |
| 130 | Clean shutdown after SIGINT (128 + 2) |
| 143 | Clean shutdown after SIGTERM (128 + 15) |

**Signals**
- The entry point owns signal handling and turns a signal into cancellation (a context, an event, a
  flag polled by the I/O loop).
- The first signal writes a one-line notice to stderr and starts the graceful path. In Python signal
  handlers, write it with `os.write`, which is async-signal-safe. A second signal exits immediately with
  `128 + n`.
- A cancellation that no signal caused (an internal cancel) exits 1, never 130 or 143.
- Workers and children don't install their own handlers unless the design requires it, and then the
  README says why.

**Streams and configuration**
- **Streams:** program results go to stdout (line-buffered when another program consumes them). Logs,
  usage and diagnostics go to stderr. A long-running program prints a readiness banner to stdout once
  its signal handlers are installed.
- **Configuration:**
  - Use flags for what a user varies per run, and environment variables for deployment settings.
  - Env vars are UPPER_SNAKE_CASE: `<SERVICE>_URL` for endpoints, and a `<PROTO>_` prefix for the
    rest. An empty value means unset.
  - `--help` lists every env var. Defaults are safe for local use, and no secret is ever committed.
- **Reconnect loops** retry only protocol and connection errors. Local errors exit.

## 11. External services and containers

- **Containers:**
  - Services run in Docker under a repo-unique name, `psychic-compendium-<service>`, with ports bound
    to `127.0.0.1` only.
  - Choose host ports that won't clash with a locally installed service (e.g. 5679 instead of 5672),
    and give each container a healthcheck.
- **Makefile:** `<service>-up` / `<service>-down` targets (`broker-up`, `broker-down`), with variables
  for the container name, image tag, ports and wait time.
  - `make run` and `make test-integration` may start the service and leave it running for fast
    reruns. When they do, the README says so and names the target that removes it.
- **Pin images to a minor version** (`rabbitmq:4.1-management`). Dependency-free unit tests use fakes
  so that `make check` never needs the service.

## 12. Proven patterns

These are the designs the prototypes validated. Reuse them rather than reinventing them.

- **Producer/consumer:**
  - The sending side owns the channel or queue and closes it exactly once, after every sender has
    returned.
  - Consumers stop on close (`for range`, sentinel or end-of-stream marker).
  - A blocking send is the backpressure, and in-flight work is bounded by `consumers + buffer`.
- **Process fan-in over pipes (Python):**
  - One `Pipe(duplex=False)` per producer, because one connection end is not safe for concurrent
    writers. The consumer multiplexes with `multiprocessing.connection.wait()`.
  - The parent closes its copy of each write end right after `start()`, so EOF can arrive and later
    children don't inherit it. Each child closes its read end, so a dead parent makes the child's next
    send fail.
  - An explicit end-of-stream marker (`DONE` / `FAILED`) tells a clean finish from a crash.
  - Check liveness with `is_alive()` (`waitpid`), not process sentinels: under fork and spawn,
    sentinels are pipes too, and grandchildren inherit them.
  - Create thread pools only after the last fork. Block SIGINT/SIGTERM on the starting thread while
    children start. Children ignore SIGINT, and the parent coordinates shutdown with SIGTERM.
  - Pipes pickle everything. Objects that can't pickle can't cross, so send rebuildable state or keep
    the object in one process.
- **At-least-once messaging (RabbitMQ):**
  - Both sides declare the topology idempotently: durable exchange, quorum queue with
    `x-delivery-limit`, and a dead-letter exchange and queue.
  - The publisher sends persistent messages with publisher confirms and `mandatory=True`.
  - The consumer acks manually *after* processing, with a bounded prefetch.
  - Poison messages get `nack(requeue=False)` and go to the DLQ.
  - Record before you ack, so a lost ack shows up as a duplicate, which consumers dedupe by
    `message_id`.
  - Message envelope: JSON `{message_id (lowercase UUID), type, timestamp (ISO 8601 with a timezone),
    payload}` with a size cap. AMQP properties mirror it, including
    `app_id=psychic-compendium.<proto>`.
  - Topology names are dotted, as in `<proto>.events` and `<proto>.events.worker.dlq`.
- **Atomic file persistence:**
  - Write a temp file in the same directory, then write, fsync, close, rename, and fsync the
    directory. Files are 0600, directories 0700, and symlinks are followed.
  - Keep a versioned on-disk record type separate from the domain type, and decode strictly
    (unknown fields and trailing data are errors).
  - A corrupt file is an `ErrCorrupt` error and is never overwritten.
- **Apply after save:** clone the state, apply the change, persist it, and only then adopt it, so what
  the user sees never runs ahead of what is on disk.
- **FFI error reporting:** return a status code and pass the result through an out-param, and let a
  callback ask to stop.

## 13. Performance claims

- **Report the median of N runs** (5 by default), with min and max, plus the machine, OS, CPU count,
  language and toolchain versions, the machine load when relevant, and the exact command.
- **Include a control experiment** when claiming a mechanism, e.g. PyDLL vs CDLL to show the GIL
  being released, or the same workload in-process vs across processes.
- **Measure end to end:** until every item is processed, with nothing printed in the hot path.
- **Show both sides of a trade-off,** e.g. a trivial workload alongside a CPU-bound one.
- **Report the range observed across runs,** not the best run. Table cells read `median (min-max)`,
  with a separate ratio column, and modes are interleaved within each repetition so that drift hits
  them equally.
- **Demos whose results depend on the GIL print `sys._is_gil_enabled()`,** falling back to `True` on
  versions without it.
- **Verify each run's result** before its time counts. A fast wrong answer isn't a data point.
- **Give every benchmark a `--quick` smoke mode** that the test suite runs, so it can't rot.
- **Commit raw numbers only in the README,** in a `## Benchmark` section after `## Test`, next to the
  `make bench` command that regenerates them.

## 14. Starting a new prototype

1. Create a `kebab-case` directory on a branch named `feat/<name>`.
2. Copy what you need from `templates/`:
   - `README.md`;
   - `pyproject.toml` plus `Makefile.python` plus `gitignore.python`, or `.golangci.yml` plus
     `Makefile.go` plus `gitignore.go`.
   Rename `Makefile.*` to `Makefile` and `gitignore.*` to `.gitignore`.
3. Write the question first, in the README's opening paragraph. Then the failure-modes table, then the
   code.
4. Add a line to the index in the root `README.md`. The root `Makefile` picks the directory up on its
   own.
5. Work through the [definition of done](#definition-of-done).

## 15. Changing this standard

Change `STANDARDS.md` and the affected `templates/` files in the same pull request. Propagate the
change to every existing prototype in that pull request, and run the root `make check`. A rule that
only some prototypes follow is not a rule.

## Appendix: tool versions

| Tool | Version | How it runs |
|---|---|---|
| Python | 3.11+ (verified on 3.14) | `python3` |
| uv | 0.x, current | dependency management, runs ruff and mypy |
| ruff | 0.16.10 | `uvx ruff@0.16.10` |
| mypy | 2.4.0 | `uvx mypy@2.4.0` |
| Go | `go 1.26` in go.mod, `toolchain go1.27.1` | any `go` ≥ 1.21 on PATH; it downloads the pinned toolchain |
| staticcheck | 2026.2.1 | `go run honnef.co/go/tools/cmd/staticcheck@2026.2.1` |
| golangci-lint | v2.14.0 (v2 config format) | `go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0` |
| govulncheck | v1.8.0 | `go run golang.org/x/vuln/cmd/govulncheck@v1.8.0` (`make vulncheck`) |
| C compiler | clang (Apple clang 17 locally, distro clang in CI) | `CC ?= cc` |
