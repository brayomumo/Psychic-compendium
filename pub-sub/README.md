# RabbitMQ pub-sub

How do you publish and consume through RabbitMQ without losing messages, and
keep running when the broker restarts or someone sends a message that can
never be processed? This prototype answers that with one publisher process and
one consumer process (Python, pika 1.4's blocking adapter, RabbitMQ 4.1). It
backs every claim below with a test that runs against a real broker.

## Concept

### Where messages get lost

A message can disappear at three points. The guarantee holds only if all
three are closed.

1. **Between the exchange and a queue.** An exchange routes; it does not
   store. A message that matches no binding is silently dropped. The fix has
   two parts. Both sides declare the full topology on every connect, so a
   publisher that starts before any consumer still has a queue to route into.
   And the publisher sets `mandatory=True`, so the broker returns anything
   unroutable instead of discarding it.
2. **Inside the broker.** A message in memory dies with the broker. The fix
   has three parts:
   - a *durable* queue, so the queue itself survives a restart;
   - *persistent* messages (`delivery_mode=2`), so the messages in it
     survive too;
   - **publisher confirms**, so the publisher knows when the broker has
     taken responsibility.

   A quorum queue confirms only after the message is committed to its log.
3. **Inside the consumer.** With `auto_ack=True` the broker forgets a message
   the moment it is sent. The consumer uses manual acks instead, and acks only
   after processing. A crash at any point leaves the message unacked, and the
   broker delivers it again.

Closing all three gives **at-least-once** delivery. Duplicates are normal, not
a bug. A publisher whose connection drops before the confirm arrives cannot
know whether the broker stored the message, so it publishes it again. A
consumer whose ack is lost receives the message again. So the consumer must
be idempotent. This one remembers recently processed `message_id`s and acks
duplicates without reprocessing them.

### Messages that can never succeed

A message is *poison* if processing it fails every time. Requeueing it
forever makes the consumer spin on it and starves everything behind it. The
consumer separates two cases:

- **Malformed** (bad JSON, wrong content type, unknown type, missing fields):
  `basic_nack(requeue=False)`. The queue's dead-letter exchange routes the
  message to a dead-letter queue (DLQ), where someone can inspect it. Retrying
  cannot help.
- **The handler raised:** `basic_nack(requeue=True)`. The failure may be
  transient, such as a database blip. A quorum queue counts deliveries, and
  at `x-delivery-limit` it dead-letters the message with reason
  `delivery_limit`. Retries are therefore bounded without any bookkeeping in
  the consumer.

### Staying up through restarts

Both processes treat a lost connection as routine. They reconnect with
**capped exponential backoff with full jitter**: a random delay between zero
and `min(cap, base * 2^attempt)`. When a broker restarts, every client notices
at the same instant. Jitter keeps their reconnects from arriving in
synchronised waves. Errors that retrying cannot fix fail fast instead of
looping forever: a wrong password, a missing vhost, or a queue that already
exists with different arguments.

### Exchange types (reference)

| Type | Routes a message to | Typical use |
|---|---|---|
| direct | queues whose binding key equals the routing key | work queues, this prototype |
| topic | queues whose binding pattern matches the dotted routing key (`*` = one word, `#` = zero or more) | events by category, e.g. `user.*` |
| fanout | every bound queue, ignoring the routing key | broadcast; the dead-letter exchange here |
| headers | queues whose binding arguments match message headers (`x-match` = `all` or `any`) | routing on several attributes |
| default (`""`) | the queue named by the routing key; every queue is bound to it automatically | quick tests; skips your own exchanges |
| dead-letter | not a type: any exchange named in a queue's `x-dead-letter-exchange` | rejected, expired or over-limit messages |

## Design

```mermaid
flowchart LR
    P[publisher] -- "persistent, mandatory,<br/>waits for confirm" --> X{{"pubsub.events<br/>(direct)"}}
    X -- "user.created" --> Q[("pubsub.events.worker<br/>quorum, delivery limit 20")]
    Q -- "prefetch 10,<br/>manual ack" --> C[consumer]
    C -. "nack, requeue=False<br/>(malformed)" .-> Q
    Q -- "rejected or<br/>over the limit" --> DLX{{"pubsub.events.dlx<br/>(fanout)"}}
    DLX --> DLQ[("pubsub.events.worker.dlq<br/>quorum")]
```

| Module | Responsibility |
|---|---|
| `pubsub/config.py` | Reads env vars, validates all of them, and reports every problem at once |
| `pubsub/messages.py` | JSON envelope (`message_id`, `type`, `timestamp`, `payload`) and matching AMQP properties; the same validation on encode and decode |
| `pubsub/broker.py` | Connection parameters, idempotent topology declaration, retry-or-fail classification of errors |
| `pubsub/backoff.py` | Full-jitter backoff that cannot overflow |
| `pubsub/dedupe.py` | Bounded record of recently processed ids |
| `pubsub/shutdown.py` | Signal-safe stop flag; a second signal forces exit |
| `pubsub/publisher.py` | Publishes one message at a time and waits for each confirm |
| `pubsub/consumer.py` | Decides each delivery's fate: ack, dead-letter, retry, or hand back |
| `pubsub/demo.py` | The end-to-end run behind `make run` |

**Ownership.** pika's `BlockingConnection` is not thread-safe. Each process
owns exactly one connection and one channel, and uses them only from its main
thread. Supervision (restarting a crashed process) belongs to the OS, a
container runtime or systemd, not to threads inside the app.

**Each delivery ends exactly one way:**

| Outcome | When |
|---|---|
| `ack` | processed, or a duplicate of an already processed message |
| `nack`, `requeue=False` | malformed; goes to the DLQ now |
| `nack`, `requeue=True` | the handler raised; retried until the delivery limit dead-letters it |
| `nack`, `requeue=True` | delivered after shutdown began; never started |

The consumer reports a message (stdout, the dedupe record) *before* acking it.
If the ack is lost with the connection, the redelivery is recognised as a
duplicate, so each id appears on stdout exactly once.

**Shutdown.** SIGINT and SIGTERM only set a flag. The signal handler never
calls into pika, because it can interrupt the main thread anywhere, including
inside pika's I/O loop.
- **Consumer:** runs its own `process_data_events(time_limit=0.25)` loop
  instead of `start_consuming()`, so it notices the flag within 0.25 s. It
  finishes and acks the message in progress, then cancels the consumer. pika
  hands back prefetched deliveries it had not yet dispatched. Then it closes
  the connection.
- **Publisher:** finishes the publish and confirm in progress, then stops.
- **Second signal:** exits at once with `os._exit`. The broker requeues
  anything unacked when the socket closes.

Exit codes: 0 when the work finished, 1 on a runtime failure, 2 on bad
configuration or arguments, 130/143 after a clean shutdown caused by
SIGINT/SIGTERM.

**Streams.** stdout carries results only: one line per confirmed message id
(publisher) or processed id (consumer). It is line-buffered, so a pipe or a
test sees each id at once. Logs go to stderr.

## Run

Requires Docker and [uv](https://docs.astral.sh/uv/). `make run` starts a
local RabbitMQ, if one isn't running, and runs the end-to-end demo. The demo
deletes the demo topology and runs the publisher before any queue exists. It
then adds one malformed message, drains everything with the consumer, stops
the consumer with SIGTERM, and checks the result.

```console
$ make run
uv sync --locked
waiting for psychic-compendium-rabbitmq to become healthy...
uv run --locked python -m pubsub.demo
1. reset: deleted pubsub.events, pubsub.events.worker and their dead-letter exchange and queue
2. publisher, started before any consumer or queue existed: 100 confirmed
3. published 1 malformed message
4. consumer: processed 100, then stopped by SIGTERM (exit 143)
checks:
  ok   every confirmed message was processed (100/100)
  ok   no message was processed twice
  ok   main queue is empty (0)
  ok   the malformed message is in the dead-letter queue (1)
  ok   the consumer shut down cleanly on SIGTERM
```

To drive it by hand, use two terminals:

```console
$ make consume                              # terminal 1; Ctrl+C to stop
$ PUBSUB_MESSAGE_COUNT=5 make publish       # terminal 2
2026-10-02 15:17:53,004 INFO    pubsub.broker: publisher: connected to amqp://guest:***@localhost:5679/%2F
b4fde29f-c6e2-41d4-9946-9e12ce0a83c0
...
2026-10-02 15:17:53,011 INFO    pubsub.publisher: confirmed 5 of 5 messages; 0 republished after a lost connection
```

The management UI is at <http://localhost:15679> (guest / guest).

`make broker-down` removes the container.

### Configuration

Environment variables. Run any entry point with `--help` to see this list. An
empty value counts as unset.

| Variable | Default | Meaning |
|---|---|---|
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5679/%2F` | Broker URL (`amqp` or `amqps`) |
| `PUBSUB_EXCHANGE` | `pubsub.events` | Main exchange (direct) |
| `PUBSUB_QUEUE` | `pubsub.events.worker` | Main queue (quorum) |
| `PUBSUB_ROUTING_KEY` | `user.created` | Binding key |
| `PUBSUB_DLX` | `pubsub.events.dlx` | Dead-letter exchange (fanout) |
| `PUBSUB_DLQ` | `pubsub.events.worker.dlq` | Dead-letter queue |
| `PUBSUB_DELIVERY_LIMIT` | `20` | Returns a message may have before it is dead-lettered |
| `PUBSUB_PREFETCH` | `10` | Unacked deliveries in flight per consumer |
| `PUBSUB_MESSAGE_COUNT` | `100` | Messages the publisher sends |
| `PUBSUB_MAX_MESSAGES` | `0` | Consumer exits after N processed; 0 = until signalled |
| `PUBSUB_PUBLISH_INTERVAL_MS` | `0` | Pause between publishes (heartbeats keep flowing) |
| `PUBSUB_WORK_MS` | `0` | Simulated work per message; must be under half the heartbeat |
| `PUBSUB_HEARTBEAT_S` | `30` | AMQP heartbeat timeout |
| `PUBSUB_RECONNECT_BASE_S` | `0.5` | First backoff ceiling |
| `PUBSUB_RECONNECT_CAP_S` | `15` | Largest backoff ceiling |
| `PUBSUB_LOG_LEVEL` | `INFO` | `DEBUG` also shows pika's own logs |

Makefile variables:

| Variable | Default |
|---|---|
| `PYTHON` | `python3` (the virtualenv is built on this interpreter) |
| `UV` | `uv` |
| `RUFF` | `uvx ruff@0.16.10` |
| `MYPY` | `uvx mypy@2.4.0` |
| `BROKER_CONTAINER` | `psychic-compendium-rabbitmq` |
| `BROKER_IMAGE` | `rabbitmq:4.1-management` |
| `AMQP_PORT` | `5679` |
| `MANAGEMENT_PORT` | `15679` |
| `BROKER_WAIT_S` | `90` |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:$(AMQP_PORT)/%2F` |

The ports are not RabbitMQ's defaults, so this broker never clashes with one
already running. They are bound to 127.0.0.1, because the broker uses the
well-known guest account.

### Broker administration

```console
$ docker exec psychic-compendium-rabbitmq rabbitmqctl list_queues name type messages messages_unacknowledged
pubsub.events.worker.dlq  quorum  1  0
pubsub.events.worker      quorum  0  0

$ docker exec psychic-compendium-rabbitmq rabbitmqctl add_user <user> <password>
$ docker exec psychic-compendium-rabbitmq rabbitmqctl set_user_tags <user> administrator
$ docker exec psychic-compendium-rabbitmq rabbitmqctl set_permissions -p <vhost> <user> ".*" ".*" ".*"
```

`set_permissions` takes three regexes: configure, write and read. See the
[rabbitmqctl reference](https://www.rabbitmq.com/docs/man/rabbitmqctl.8).

## Test

`make check` runs ruff (lint and format), `mypy --strict`, and the tests that
need no broker. Those cover config validation, message validation, backoff,
dedupe, signal handling, the consumer's and the publisher's decisions against
in-memory fakes, and real processes for exit codes and for shutdown while the
broker is unreachable.

```console
$ make check
...
Success: no issues found in 23 source files
...
Ran 78 tests in 1.937s

OK (skipped=1)
```

The same suite passes on Python 3.11 (the oldest supported version):
`UV_PYTHON=3.11 uv run --isolated --locked --python 3.11 python -m unittest
discover -s tests`.

The skipped test is the integration module. `make test-integration` starts
the broker and runs it. Every test uses its own uniquely named topology. One
test restarts the broker container mid-stream.

```console
$ make test-integration
test_broker_restart_mid_stream_loses_nothing_confirmed ... ok
test_consumer_killed_mid_message_gets_it_redelivered ... ok
test_publisher_sigint_stops_after_a_confirmed_message ... ok
test_second_sigint_forces_an_immediate_exit ... ok
test_sigint_finishes_the_message_in_flight_then_exits ... ok
test_consumer_started_first_receives_everything ... ok
test_demo_passes_end_to_end ... ok
test_publisher_started_first_loses_nothing ... ok
test_queue_declared_with_other_arguments_is_reported ... ok
test_wrong_password_fails_fast_instead_of_retrying ... ok
test_a_message_that_keeps_failing_hits_the_delivery_limit ... ok
test_malformed_messages_are_dead_lettered ... ok
Ran 12 tests in 16.533s
OK
```

What a broker restart looks like from the clients (trimmed; 200 messages,
none lost):

```text
WARNING pubsub.publisher: connection lost while idle (ConnectionClosedByBroker: (320, "CONNECTION_FORCED - broker forced connection closure with reason 'shutdown'"))
WARNING pubsub.broker: publisher: cannot reach broker at amqp://guest:***@localhost:5679/%2F (IncompatibleProtocolError: StreamLostError: ('Transport indicated EOF',)); retry 1 in 0.33s
...
INFO    pubsub.broker: publisher: connected to amqp://guest:***@localhost:5679/%2F
INFO    pubsub.publisher: confirmed 200 of 200 messages; 0 republished after a lost connection
INFO    pubsub.consumer: processed 200, duplicates 0, dead-lettered 0, retried 0, redelivered 0
```

## Failure modes

| Failure mode | What happens | How it's handled | Test |
|---|---|---|---|
| Publisher starts before any consumer or queue | Messages would match no binding and be dropped | Publisher declares the topology and publishes with `mandatory=True` | `test_publisher_started_first_loses_nothing` |
| Queue deleted while the publisher runs | Broker returns the message as unroutable | Reconnect, which re-declares the topology; give up after 5 refusals | `test_unroutable_message_triggers_a_topology_redeclare` |
| Broker nacks a publish | The broker could not store it | Back off and retry; give up after 5 refusals | `test_nack_is_retried_on_the_same_connection`, `test_gives_up_when_the_broker_keeps_refusing` |
| Connection drops before a confirm | Outcome unknown | Republish with the same `message_id`; the consumer deduplicates | `test_republishes_the_same_message_after_a_lost_confirm` |
| Broker restarts mid-stream | Both connections close with 320 CONNECTION_FORCED | Both reconnect with jittered backoff; nothing confirmed is lost | `test_broker_restart_mid_stream_loses_nothing_confirmed` |
| Broker down at startup | Connection refused | Retry with backoff until it is up or a signal arrives | `test_consumer_exits_143_on_sigterm` (and 3 siblings) |
| Broker accepts connections, then drops them | Could become a hot reconnect loop | The consumer backs off before every reconnect; only processing a message resets the backoff | `test_reconnects_after_the_connection_drops` |
| Many clients reconnect at once | Synchronised reconnect storm | Full jitter | `test_jitter_spreads_clients_apart` |
| Thousands of failed attempts | `base * 2**n` overflows a float | Exponent is clamped; the cap wins | `test_does_not_overflow_after_many_attempts` |
| Consumer killed mid-message | Message was never acked | Broker redelivers it to the next consumer | `test_consumer_killed_mid_message_gets_it_redelivered` |
| Ack lost with the connection | Message is redelivered after processing | Recorded before the ack, so the redelivery is acked as a duplicate | `test_lost_ack_does_not_double_count_the_redelivery` |
| Duplicate delivery | Would be processed twice | Acked without reprocessing (bounded in-memory record) | `test_duplicate_is_acked_without_reprocessing` |
| Malformed message | Can never be processed | Dead-lettered at once; `x-death` reason `rejected` | `test_malformed_messages_are_dead_lettered` |
| Handler keeps raising | Would loop forever | Requeued until the delivery limit, then dead-lettered with reason `delivery_limit` | `test_a_message_that_keeps_failing_hits_the_delivery_limit` |
| Ids that differ only in spelling (`{ABC…}` vs `abc…`) | Would slip past dedupe | Ids are normalised to the canonical UUID form | `test_normalises_the_id_so_dedupe_sees_one_message` |
| Huge, deeply nested, NaN-laden or non-UTF-8 body | Memory blow-up or a crash in the parser | Size cap and strict parsing; dead-lettered | `test_rejects_malformed_deliveries` |
| Broker cancels the consumer (queue deleted, leader moved) | No more deliveries, silently | Detected via the cancel callback; reconnect and resubscribe | `test_resubscribes_when_the_broker_cancels_the_consumer` |
| SIGINT/SIGTERM during processing | — | Finish and ack the current message, requeue the prefetched ones, exit 130/143 | `test_sigint_finishes_the_message_in_flight_then_exits` |
| SIGINT/SIGTERM to the publisher | — | Finish the current publish and confirm, exit 130/143; every printed id is in the queue | `test_publisher_sigint_stops_after_a_confirmed_message` |
| Second Ctrl+C | Operator wants out now | Immediate `os._exit`; the broker requeues unacked work | `test_second_sigint_forces_an_immediate_exit` |
| Started with SIGINT ignored (`cmd &` in a script, `nohup`) | Python installs no Ctrl+C handler, so `kill -INT` would do nothing | `Shutdown.install()` sets its handler explicitly | `test_sigint_works_even_when_inherited_as_ignored` |
| stdout closed (`make consume \| head -1`) | Writes fail with `BrokenPipeError` | Exit 1, instead of being mistaken for a broker outage and reconnecting forever; the unacked message is redelivered | `test_closed_stdout_is_not_mistaken_for_a_broker_outage` |
| Producer sends `application/json; charset=utf-8` | A strict string comparison would dead-letter valid JSON | The media type is compared without parameters, case-insensitively | `test_accepts_content_type_parameters` |
| Handler slower than the heartbeat timeout | Broker drops the connection mid-message; redelivery loops forever | Rejected at startup: `PUBSUB_WORK_MS` must be under half of `PUBSUB_HEARTBEAT_S` | `test_rejects_invalid_values` |
| Long pause between publishes | Idle connection misses heartbeats | The pause runs the I/O loop | `test_interval_keeps_servicing_the_connection` |
| Broker memory or disk alarm | Broker stops reading publishes | `blocked_connection_timeout` turns the hang into a reconnect | `test_retries_are_left_to_us_and_heartbeats_are_on` |
| Wrong password or vhost | Retrying cannot help | Exit 1 at once, without retrying | `test_wrong_password_fails_fast_instead_of_retrying` |
| Queue exists with different arguments | 406 PRECONDITION_FAILED | Exit 1 with a hint to delete the queue or use a policy | `test_queue_declared_with_other_arguments_is_reported` |
| Invalid configuration | — | Every problem listed at once, exit 2 | `test_bad_configuration_exits_2_listing_every_problem` |
| Password in logs | Credential leak | URLs are logged with the password redacted | `test_hides_the_password` |

## What the first version got wrong

1. **The thread supervisor restarted the healthy threads.** `Manager.run`
   checked `if thread.is_alive():` where it meant `if not`. Every second it
   started a second publisher, which re-sent all 400 messages, and a second
   consumer. Dead threads were never restarted. A stub test showed 5 copies
   of a healthy job after 4.5 s, and 0 restarts of a dead one. *Lesson:*
   process supervision is a solved problem (systemd, Kubernetes, Docker
   restart policies). An app should do one job per process and exit
   non-zero when it can't.
2. **One `BlockingConnection` was shared across threads.** pika's blocking
   adapter is not thread-safe, and the restart bug put several threads on the
   same channel at once.
3. **Messages published before the consumer started were silently dropped.**
   Only the consumer declared and bound the queue, and an exchange drops what
   it cannot route. `mandatory` was off and there were no confirms, so nothing
   reported the loss. *Lesson:* topology is shared state, and both sides must
   declare it.
4. **Nothing survived a broker restart.** The queue was not durable, the
   messages were not persistent, and the publisher never waited for confirms.
5. **`auto_ack=True`.** A consumer crash lost whatever it had received but not
   finished.
6. **`pika.BasicProperties(method)` set the content type.** The first
   positional argument of `BasicProperties` is `content_type`, so every message
   claimed to be of type `"User_Publisher"`. The fields that were meant are
   `type` and `message_id`.
7. **The standalone consumer could not start.** It called `Consumer(url)`
   without the required `server_name`, and then `.consume()`, a method that
   didn't exist (it was `kimbia`).
8. **Ctrl+C hung.** The manager `join()`ed a consumer thread blocked forever
   in `start_consuming()`.
9. **Idle connections starved their heartbeats.** The publisher finished its
   batch and left an open `BlockingConnection` that nothing serviced. pika's
   blocking adapter answers heartbeats only while you are inside one of its
   calls.
10. **One env var named everything.** `SERVER_NAME`, default `"localhost"`,
    named the exchange, the queue and the routing key, mixing up a host name
    with topology names.
11. **Unset config crashed obscurely.** A missing `RABBITMQ_CONN_STRING` passed
    `None` into pika. There was no requirements file. `notes.md` had
    `set_permission` for `set_permissions`.

The rebuild turned up four more surprises, each found by a failing test:

- **Every return counts toward the delivery limit.** On RabbitMQ 4.1.8 the
  count goes up for a message handed back because a channel closed, as well
  as for an explicit nack with requeue (measured: a limit of 2 dead-letters
  on the third delivery either way). A consumer that restarts while holding
  prefetched messages pushes healthy messages toward the DLQ. That is why the
  limit defaults to 20 and prefetch to 10.
- **pika names handshake failures after the stage, not the cause** (1.3
  and 1.4 alike). When a restarting broker drops the socket, the error is
  `IncompatibleProtocolError` or `ProbableAuthenticationError`. The first
  classifier treated those as fatal, and the restart test failed. Only an
  explicit 403 or 530 in the message is a real refusal. Docker adds to the
  confusion: its port forwarder accepts the TCP connection while the broker is
  down, then closes it.
- **Two quick signals become one.** POSIX does not queue a second identical
  signal while the first is still pending. The force-quit test sent two
  SIGINTs back to back, and the process saw one. The handler now writes a
  notice line, and the test waits for it before sending the second.
- **Quorum-queue publishes are asynchronous.** Without confirms, a
  `basic_get` straight after a `basic_publish` can miss the message. Confirms
  are not optional for correctness reasoning, even in tests.

## Trade-offs and limits

- **At-least-once, not exactly-once.** Deduplication here is per process and
  in memory, so it forgets on restart and cannot see what other consumers
  processed. A real consumer records the `message_id` in the same
  transaction as its side effect, for example under a unique constraint.
  "Exactly-once" is at-least-once plus an idempotent effect.
- **One confirm round trip per message.** The publisher is simple and its
  guarantee is easy to reason about, but throughput is bounded by latency. For
  volume, use asynchronous confirms (pika's `SelectConnection`, tracking
  delivery tags) or publish in batches and wait for all their confirms.
- **The handler runs on the I/O thread.** Work must finish well within the
  heartbeat timeout; config enforces half. Longer work belongs on a worker
  thread that acks through `connection.add_callback_threadsafe`.
- **Retries are immediate.** A requeued message comes straight back, so an
  outage downstream burns through the delivery limit in seconds. Delayed retry
  needs a retry queue with a message TTL that dead-letters back to the main
  exchange.
- **Shutdown returns count as failed deliveries** (see above). Keep prefetch
  small compared with the delivery limit.
- **Ordering is not preserved** across redeliveries or multiple consumers.
- **Topology lives in client code, through x-arguments.** Changing
  `PUBSUB_DELIVERY_LIMIT` on an existing queue fails with 406 until the queue
  is deleted. In production, put dead-lettering and limits in a broker policy
  (it can change at runtime), and own topology in infrastructure code, with
  clients declaring passively.
- **A single node proves durability, not availability.** A quorum queue on
  one node survives a restart but not the loss of the disk. Fault tolerance
  needs three nodes.
- **Local-only security.** guest/guest on 127.0.0.1, no TLS. `amqps://` URLs
  are accepted but untested here.
- **The dead-letter queue has no bound.** Alert on its depth, and give it a
  length limit or TTL by policy, so poison messages cannot pile up unnoticed.
- **During an outage the publisher blocks; it does not buffer.** Memory stays
  bounded, and whatever feeds the publisher has to tolerate waiting.
