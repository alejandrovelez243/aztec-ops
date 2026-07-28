# 0003 — Transactional outbox relayed to Redis Streams

## Status

Accepted — 2026-07-28.

## Context

A state change in this system fans out to three consequences that are not part of the request:
the priority score is recomputed, the risk flags are re-evaluated, and the `ProjectSnapshot`
read model is rebuilt and pushed to any open SSE connection. None of them belongs inside the
transition request, because none of them should be able to fail the transition.

The naive shape is a service that calls `save()` and then `redis.xadd()`. It has two failure
modes, and both are silent:

- **Commit without publish.** The transaction commits, then the process is killed, Redis is
  restarting, or the network drops between the two calls. The state change is durable, the event
  is gone forever, and the score, the risk flags and the snapshot stay stale with nothing
  anywhere indicating that they are wrong. The command center then confidently ranks a project
  using inputs that no longer exist.
- **Publish without commit.** The publish succeeds, then the transaction rolls back. Consumers
  react to a change that never happened and write derived state for it.

This system's entire value proposition is that the ranking is defensible and that decisions
leave a trace. A dropped event breaks both at once, and it breaks them invisibly — the number is
still there, still confident, just wrong. That is the pressure: not throughput, but the fact
that a lost event is indistinguishable from a correct one.

## Decision

Application services never publish. They write an `OutboxEvent` row in the same PostgreSQL
transaction as the aggregate mutation and the `ActivityRecord`. Either all three exist or none
do. A separate relay process claims unpublished rows with `SELECT ... FOR UPDATE SKIP LOCKED`,
`XADD`s them to the Redis stream `aztec.events`, and marks the row published only after `XADD`
returns.

- Three consumer groups on that stream, one per concern: `priority-recalculator`,
  `risk-evaluator`, `sse-fanout`. A failure in one never blocks another.
- Every consumer deduplicates on `event.id` through `ProcessedEvent`; the `ProcessedEvent`
  insert and the handler's own writes share one transaction, and a duplicate hits the unique
  constraint and is acknowledged without reprocessing.
- `XACK` happens only after the handler has committed.
- Failures retry with backoff. After N attempts the event goes to `aztec.events.dlq` with the
  error, the attempt count and the consumer group, then is acked on the main stream. The DLQ is
  visible in the admin.
- The envelope shape is fixed (`docs/ARCHITECTURE.md` §6). New fields go inside `payload`;
  changing the top level bumps `version` and consumers handle both.
- A module under `services/` that imports the Redis client is a bug, not a shortcut.

## Consequences

Good:

- There is no dual write anywhere in the system. Durability of the event equals durability of
  the state change, by definition rather than by timing.
- A relay crash mid-publish republishes rather than loses, which is why delivery is
  at-least-once and why idempotency is a hard rule rather than a nicety.
- The outbox table is a queryable log of everything the system intended to publish. When the UI
  is stale, the first diagnostic is one `SELECT` on unpublished rows, and it distinguishes
  "never published" from "published, consumer failed" without guessing.
- Adding a reaction to an existing change is a new consumer group. External notifications, which
  §12 lists as out of scope, would be exactly that and nothing else.

Cost we accepted:

- Two extra processes in `docker-compose.yml` (`relay`, `worker`) that must be running for the
  UI to update. When SSE looks broken the cause is usually one of them being down, which is a
  class of confusion a direct publish would not have.
- End-to-end latency is now commit, plus relay poll interval, plus consumer handling. It is
  sub-second in practice and irrelevant for morning triage, but it is not zero and the UI has to
  tolerate the gap rather than assume a response reflects post-event state.
- Every consumer carries idempotency machinery it would not need under exactly-once delivery.
  `ProcessedEvent` grows unbounded and will eventually need pruning that is not built yet.
- Correctness now depends on a rule that only review enforces: services must not import Redis,
  and the `OutboxEvent` write must not drift into an `on_commit` hook, which would quietly
  reintroduce the dual write with the same silent failure mode.
- The DLQ is a place where events go to be forgotten unless someone looks at the admin. Nothing
  pages anyone.

## Alternatives considered

- **Publishing straight from the service after commit.** Rejected. This is precisely the
  commit-without-publish failure, and it fails silently in the one part of the system whose
  value is being trustworthy.
- **`transaction.on_commit()` to publish.** Rejected. It fixes publish-without-commit but not
  commit-without-publish: the hook runs in the same process after the commit, so a crash or a
  Redis outage in that window still loses the event permanently, and now there is no row
  recording that it was ever meant to exist.
- **Celery with a broker.** Rejected. It moves the dual write rather than removing it — enqueuing
  a task from inside a transaction has the same failure window — and it adds a worker model,
  result backend and serialization layer heavier than the three consumer groups we need.
- **Postgres `LISTEN/NOTIFY` as the bus.** Rejected. Notifications are fire-and-forget with no
  persistence, no consumer groups and no replay: a consumer that is down during a notification
  never learns it happened. That is the same lost-event problem in a different place.
- **Kafka.** Rejected. It is the right shape and the wrong size. Redis is already in the compose
  file for other reasons, and Streams give consumer groups, acks and pending-entry inspection,
  which is the full feature set this system uses.
