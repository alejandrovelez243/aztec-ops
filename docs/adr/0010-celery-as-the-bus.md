# 0010 — Celery as the bus, Redis Streams removed

## Status

Accepted — 2026-07-28. Amends [0003](0003-transactional-outbox-with-redis-streams.md): the outbox
stands exactly as written there, the Redis Streams transport below it does not. Extends
[0009](0009-celery-beat-for-scheduling.md): Celery arrived for scheduling only, and now absorbs the
bus that ADR argued it should not take.

## Context

The two decisions were each defensible on the day they were made, and the system that resulted from
holding both was not.

ADR 0003 chose Redis Streams because the thing it needed was consumer-group fan-out: a producer
writes one event and N reactors subscribe without the producer knowing they exist. That is CLAUDE.md
rule 8 applied to the bus, and a task queue looked like the opposite — `recalculate_priority.delay()`
is a producer naming a consumer.

ADR 0009 then brought Celery in for the clock, because the hand-rolled ticker kept the last local
date in process memory. It scoped itself carefully: Beat schedules, Celery does not carry domain
events. It also predicted the cost it was accepting — "Redis now has a second, unrelated job" and
"two services in compose have names one letter apart in meaning".

What it did not predict is what those two decisions summed to. The running system was:

```
api  relay  worker  worker-risk  worker-sse  celery-worker  beat
```

Seven application processes and **two job systems doing one job**. Both had a broker, both had
workers, both had retries, both had a backoff policy, both had a dead-letter story, both had a
"which process is down?" failure mode. Every operational question had to be asked twice, and the
runbook had a table whose entire purpose was explaining that `worker` and `celery-worker` are not
the same word.

The duplication was the problem; the process count was the symptom that made it visible. The load
this was built for is 22 projects and 82 tasks — a bus that moves a few dozen events an hour. Two
transports, two mental models and seven containers for that is YAGNI in the plainest sense: we paid
the complexity in full and never drew on the throughput it buys.

The premise ADR 0009 rejected Celery on also turned out to be avoidable rather than intrinsic. "A
task queue makes the producer name its consumer" is true of `task.delay()` called from a service.
It is not true of a task that reads the outbox and looks the subscribers up in a registry.

## Decision

**Celery is the bus. Redis Streams is removed.** The outbox is untouched.

The transport is four tasks and a registry:

- `apps/events/registry.py` maps a topic to the handlers subscribed to it. A handler is a function
  in `apps/<context>/handlers.py` decorated with
  `@register_handler(name=..., topics={...})`; app-ready calls `autodiscover_modules("handlers")`.
  Name and topics are validated at import, so a typo is a failed boot rather than a reactor that
  silently never runs.
- `events.drain_outbox` claims unpublished rows with `SELECT ... FOR UPDATE SKIP LOCKED` — the same
  claim the relay used — and queues one `events.handle_event` per subscribed handler per event, from
  `transaction.on_commit` inside the claiming transaction.
- `events.handle_event(handler_name, event_id)` applies one handler to one event. The
  `ProcessedEvent` insert on `(event_id, handler)` and the handler's writes share one transaction.
- `events.emit_interval_tick` and `events.emit_day_boundary_tick` are unchanged from ADR 0009. They
  are producers like any other: they write to the outbox and return.

The drain runs on two paths that read the same table. `enqueue_event` kicks it with
`transaction.on_commit`, so normal latency is a broker round trip; Celery Beat sweeps it every
`EVENT_DRAIN_INTERVAL_SECONDS` (default 5s), so a broker that was unreachable at commit time delays
an event by one sweep instead of losing it. A double dispatch is possible and harmless — the ledger
absorbs it.

Retries and backoff are Celery's, roughly 1s, 2s, 4s, 8s, 16s with jitter. Past
`EVENT_MAX_ATTEMPTS` the event is **dead-lettered on the outbox row**: `dead_lettered_at` and
`last_error` are set on the row that already exists. Nothing is copied to a second queue and nothing
is deleted. The admin filters on it and offers a "Re-queue selected dead-lettered events" action.

`CELERY_TASK_ACKS_LATE` flips from `False` to `True` (with `REJECT_ON_WORKER_LOST`). ADR 0009 could
set it `False` because a stale tick is worthless; it is load-bearing now, because `drain_outbox`
marks the row published before the handler runs, so an early ack plus a worker kill would drop an
event the outbox believes was delivered.

The process list becomes:

```
postgres  redis  api  worker (celery)  beat  frontend
```

Three application processes and one mental model. Redis keeps exactly two jobs: the Celery broker,
and the `aztec.sse` pub/sub channel `GET /api/stream` reads. It holds no durable event state.

### What did not change, and why that is the point

**The outbox.** It is the durability guarantee and it was never a property of the transport. The
event row commits in the same transaction as the aggregate mutation and the `ActivityRecord`; either
all three exist or none do. There is still no dual write anywhere in the system, and the
commit-without-publish window ADR 0003 exists to close is still closed. A service that imports the
Redis client is still a bug. Everything ADR 0003's Decision section says about the *outbox* stands
verbatim; only the paragraph below the commit line changed.

**The registry, so a producer still never names a consumer.** This is the objection ADR 0009 raised
against Celery, and it is answered rather than waived. The producing service writes
`OutboxEvent(topic=...)` and stops. It does not know that `snapshot-builder` exists. Adding a reactor
is one decorated function in one context's `handlers.py` — no producer edit, no `if`, no new compose
service. CLAUDE.md rule 8 survives intact, which is the whole reason this rewrite was allowed to
happen.

**Idempotency.** At-least-once is still the delivery model and `ProcessedEvent` is still how a
handler survives it. The key's second column is now the registered handler *name* rather than a
consumer group; the semantics are identical.

**The envelope and the topic catalog.** `docs/EVENTS.md` §4 is unchanged. Nothing on the wire moved.

## Consequences

Good:

- One job system. One broker, one worker command, one retry policy, one place to look when
  something did not happen. The runbook lost the table that existed to disambiguate two workers.
- Seven application processes became three. `worker` is now unambiguous — there is only one.
- Retries, backoff, jitter, scheduling and `celery -A config inspect` come from a library that is
  already a dependency, instead of from `ConsumerRunner` and its `XREADGROUP`/`XACK` loop, which is
  roughly 300 lines of infrastructure we no longer maintain or test.
- Dead-lettering became a column instead of a second stream. The failed event is the same row that
  carries its topic, payload, correlation id, `attempts` and `last_error`, in a table that is
  already joined, already indexed and already rendered in the admin.
- The `.delay()`-shaped latency path is better than the relay's. The relay polled; the drain is
  kicked on commit and swept on an interval.
- Redis no longer holds durable state. Losing the append-only file now delays events by one sweep
  rather than losing a stream.

Cost we accepted, written down rather than hidden:

- **`XPENDING` and `XINFO GROUPS` are gone.** There is no per-group pending-entry list, no consumer
  lag figure, no "which consumer has been holding this message for 40 seconds".
- **Stream replay is gone.** There is no `XRANGE` over a retained window, so "replay the last hour
  into a new consumer" is no longer a one-liner.

  The replacement for both is the outbox table, and it is arguably the better instrument. It lives
  in PostgreSQL, so it is queryable with `WHERE` and `JOIN` rather than with a purpose-built
  inspection command; it carries `published_at`, `attempts`, `last_error` and `dead_lettered_at`; it
  is retained by policy rather than by a stream `MAXLEN`; and the admin already renders it with a
  re-queue action. `SELECT count(*) FILTER (WHERE published_at IS NULL) FROM events_outboxevent` is
  the backlog, and `make outbox` prints it. What is genuinely lost is visibility *between* dispatch
  and handling — a handler task in flight is Celery's business, and `celery -A config inspect
  active` is a weaker answer than `XPENDING` was.
- A per-handler failure no longer isolates by process. Under Streams, `worker-risk` could be down
  while `worker-sse` ran. Now one worker runs every handler, so a worker outage stops all four. The
  outbox holds the backlog either way, so this costs latency and not events — and the honest fix, if
  it is ever needed, is `--queues` plus a routing rule, decided from a measurement.
- Two ADRs now have superseded transport sections. A reader of 0003 or 0009 alone gets the wrong
  picture, which is why both carry a pointer to this one.
- `ProcessedEvent` still grows unbounded and still has no pruning. Unchanged, and still owed.

## Alternatives considered

- **Keep both, and just delete the duplicate worker services.** Rejected. Merging `worker`,
  `worker-risk` and `worker-sse` into one process fixes the container count and leaves the actual
  problem — two brokers' worth of concepts, two retry policies, two dead-letter stories — exactly
  where it was.
- **Drop Celery instead, and move the clock back onto Streams.** Rejected. That reintroduces the
  in-process date state ADR 0009 removed, and it means hand-rolling the retry and backoff machinery
  Celery already ships. Of the two systems, the one worth keeping is the one that is a dependency
  rather than a file in this repository.
- **Drop the outbox now that Celery could take `on_commit(task.delay)` directly.** Rejected, for
  precisely the reason ADR 0003 and ADR 0009 both give: the hook runs after the commit, so a broker
  outage in that instant loses the work with no row saying it was meant to happen. The outbox is
  what makes the kick an optimization rather than the delivery guarantee, and it is why the Beat
  sweeper is a safety net instead of a second chance at the only chance.
- **Postgres `LISTEN/NOTIFY` to wake the drain instead of a broker kick.** Rejected as unnecessary.
  It would remove the broker from the latency path, but the drain already has a correct fallback in
  the sweeper, and adding a third notification mechanism to a decision whose entire purpose is
  removing one is the wrong direction.
- **Waiting until the load justified a choice.** Rejected as the thing that got us here. Seven
  processes was not a load-driven design either; it was two reasonable decisions never reconciled.
  At 22 projects the reconciliation is cheap, and the time to collapse a duplicated subsystem is
  before anything else is built on top of both halves.
