# Psychic-compendium

Collection of PoC to feed my demons.

Each directory is a self-contained prototype that answers one question about a concept. A prototype is
kept only if it demonstrably works, fails safely in every way we could think of, and its README teaches
the concept correctly. Every README has a **Failure modes** table, where each row names the test that
proves it, and a **What the first version got wrong** section that records the mistakes and their
lessons.

## Prototypes

| Prototype | The question it answers | Stack |
|---|---|---|
| [Piper](Piper/) | How do I fan work out from several producer processes to one consumer over OS pipes, and what can cross a process boundary? | Python multiprocessing |
| [go-concurrency](go-concurrency/) | How do goroutines and channels give me a producer/consumer pipeline with backpressure, clean shutdown and no leaks? | Go |
| [todo-cli](todo-cli/) | How do I structure a small, testable Go CLI, with pure domain logic, I/O at the edges, persistence and robust input handling? | Go |
| [lower_with_c_and_python](lower_with_c_and_python/) | How does Python call into C with ctypes, and how do callbacks, errors and Ctrl+C cross the boundary safely? | C, Python (ctypes) |
| [pub-sub](pub-sub/) | How do I publish and consume through RabbitMQ without losing messages, through broker restarts and bad messages? | Python, RabbitMQ |
| [python-coroutines](python-coroutines/) | What can generator-based coroutines do, how do they compose and bridge to threads, and when should I use asyncio instead? | Python |
| [g_to_RPC](g_to_RPC/) | How do I build a gRPC service in Go and call it from Python, and what does "production-ready" actually require? | Go, Python, gRPC |
| [simple-go-rest-api](simple-go-rest-api/) | How do I build a correct, production-shaped REST API in Go, and how much of it needs a framework? | Go, Postgres |
| [nats](nats/) | How do NATS subjects, queue groups and JetStream divide and isolate work, and what delivery guarantee does each give? | Go, Python, NATS |
| [bulkhead](bulkhead/) | How do I stop one slow or failing dependency from taking everything else down? | Python |
| [profiling](profiling/) | Where does a Python program actually spend its time and memory, and how far can I trust the profiler? | Python |

## Working in this repo

```console
$ make check                 # every prototype's lint + type checks + tests
$ make -k -j check           # all of them, in parallel, reporting every failure
$ make list                  # the prototypes the root Makefile found
$ make -C bulkhead run       # one prototype's demo; every `make run` exits on its own
```

Every prototype's `Makefile` has the same targets (`run`, `test`, `lint`, `check`, `clean`, plus
optional ones such as `bench` and `test-integration`). Its README lists the exact commands, variables
and exit codes. CI runs the same targets on Linux for every pull request.

- **[STANDARDS.md](STANDARDS.md)** is the contract every prototype meets: definition of done, git
  workflow, per-language rules, testing techniques, README structure, runtime conventions, and the
  patterns proven here.
- **[templates/](templates/)** holds copy-ready files that implement it: lint config, Makefiles,
  `.gitignore` files and a README skeleton.
- **[CLAUDE.md](CLAUDE.md)** briefs AI coding sessions working in the repo.

To add a prototype, follow [STANDARDS.md section 14](STANDARDS.md#14-starting-a-new-prototype).

## License

[Apache 2.0](LICENSE)
