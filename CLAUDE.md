# CLAUDE.md

This repo is an ideation lab: one self-contained prototype per directory, each answering one question
about a concept, then validated against its failure modes.

**[STANDARDS.md](STANDARDS.md) is the contract. Read it before changing anything.** Copy-ready files are
in [`templates/`](templates/). If this file and STANDARDS.md disagree, STANDARDS.md wins.

## Working rules

- **Verify by running, not by reading.** Run each prototype, with a `timeout`, against a scratch copy or
  worktree. Report actual output, hangs, crashes and leftover processes, and check every README claim
  against observed behaviour.
- **One prototype per branch** (`<type>/<prototype>`), touching only that directory. Root files
  (`README.md`, `STANDARDS.md`, `templates/`, `Makefile`, `.editorconfig`, `.gitignore`) change only on
  `chore/*` branches.
- **The gate:** `make check` must pass in the prototype directory, and the root `make check` runs all of
  them. `make run` must exit 0 on its own.
- **Commits:** use Conventional Commits, `<type>(<scope>): <summary>`, and end every message with
  `Co-Authored-By: God's of Nature`.
- **Never push or open pull requests unless asked.**
- **Keep docs in sync:** when you adopt a new convention, add it to STANDARDS.md (and the matching
  template) in the same change. Undocumented conventions don't count.

## Toolchain on the owner's machine

- macOS arm64, Python 3.14, Docker. The installed `go` is 1.23. That's fine: each Go module pins
  `toolchain go1.27.1`, and Go downloads it on first use. Never claim a Go result from the bare 1.23
  toolchain; run through the prototype's Makefile, which exports `GOTOOLCHAIN`.
- Linters run pinned: ruff and mypy through `uvx`, and staticcheck, golangci-lint v2 and govulncheck
  through `go run <pkg>@<version>`. Versions are in the STANDARDS.md appendix.
- The macOS multiprocessing default is `spawn`; the Linux default since Python 3.14 is `forkserver`.
  Test both, plus `fork`.
- zsh is the login shell. It doesn't word-split unquoted variables, has a `gl` alias, and leaves
  `PIPESTATUS` empty, so run multi-step checks under `bash -c '...'`.
