# 0009 — Celery Beat for scheduling, not for the bus

## Status

Accepted — 2026-07-28. Amends [0003](0003-transactional-outbox-with-redis-streams.md): the
`transaction.on_commit` and Celery entries in its "Alternatives considered" are corrected there,
and its rejection of Celery is now scoped to the bus rather than to the whole system.

## Context

The clock is a producer on this bus. Two prioritization signals, `deadline_pressure` and
`staleness`, are functions of *now* rather than of any mutation, so `clock.ticked` has to be
emitted on a schedule: one tick every `TICKER_INTERVAL_SECONDS`, and one at local midnight when
overdue and days-open change.

The first design hand-rolled that. `apps/events/ticker.py` held a `run_forever()` loop, a
`tick()` method, and a `manage.py run_ticker` command running as its own compose service. The
loop slept for the interval, and to fire the day-boundary tick it compared the current local date
against the last one it had seen and kept that date in memory.

That last part is the problem, and it is not a style objection. The remembered date is process
state that a scheduling decision depends on:

- It is wrong after every restart. The process comes up with no memory of the previous date and
  either fires a spurious day-boundary tick or skips a real one, depending on when it restarted.
- It is duplicated the moment a second replica exists. Two processes each remember their own last
  date and each fire their own midnight tick.
- It is untestable without either freezing time or reaching into the instance.

The review question was "why not just Celery?", and it deserved separate answers for two
different things Celery could have taken over: the scheduling, and the event path itself.

## Decision

Celery Beat schedules. Celery does not carry domain events.

- `backend/config/celery.py` defines the app, reads config with
  `config_from_object("django.conf:settings", namespace="CELERY")`, and calls
  `autodiscover_tasks()`. `backend/config/__init__.py` exports `celery_app`.
- `backend/config/settings.py` takes `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` from the
  same `REDIS_URL` the rest of the system uses, JSON serialization only, `CELERY_TIMEZONE` from
  `TIME_ZONE`, and `CELERY_TASK_ACKS_LATE = False` — a tick is worthless once the next one is
  due, so redelivering an unacknowledged one buys nothing and risks a burst after an outage.
- `CELERY_BEAT_SCHEDULE` has exactly two entries: `emit-interval-tick`
  (`events.emit_interval_tick`, every `TICKER_INTERVAL_SECONDS`) and `emit-day-boundary-tick`
  (`events.emit_day_boundary_tick`, `crontab(hour=0, minute=0)`).
- `backend/apps/events/tasks.py` holds the two `@shared_task` functions. Each calls
  `Ticker().emit(...)` and returns the envelope id as a string. They are the only tasks in the
  system.
- `Ticker` in `backend/apps/events/ticker.py` is stateless. It knows how to write one tick to the
  outbox and nothing else. `run_forever()`, `tick()` and the remembered date are gone, and
  `backend/apps/events/management/commands/run_ticker.py` is deleted.
- A task writes an `OutboxEvent` and returns. Rule 4 is unchanged: the relay still publishes, and
  a task that imported the Redis client would be the same bug as a service that did.

The day boundary is a second schedule entry rather than a branch inside the interval task
precisely because that is where the state went. `crontab(hour=0, minute=0)` states the intent and
remembers nothing across restarts or replicas.

Two compose services replace the one `ticker` service:

```
beat            celery -A config beat        decides when
celery-worker   celery -A config worker      executes the scheduled tasks
```

**`celery-worker` is not `worker`.** The pre-existing `worker` service runs `manage.py
run_consumer` — the Redis Streams consumer groups from ADR 0003 (`priority-recalculator`,
`risk-evaluator`, `sse-fanout`). They are different processes with different failure modes:
`celery-worker` down means no ticks are produced; `worker` down means events are produced and
nobody reacts. Anywhere both appear — compose, the runbook, an incident — say which one.

### Why Celery is not the bus

A task queue names its consumers. `recalculate_priority.delay(...)` is a producer stating who
runs; adding a second reaction means editing the producer to add a second `.delay()`. That is the
same shape CLAUDE.md rule 8 rules out for signals — extension by editing the existing code path.
Consumer groups invert it: the producer writes one `clock.ticked`, and N groups subscribe without
the producer knowing they exist. Adding a reactor is a new consumer group and zero edits upstream.

### Why Celery would not have removed the outbox

`transaction.on_commit(task.delay)` is the closest Celery gets, and it does close the
publish-without-commit window — the hook does not run if the transaction rolls back. It does not
close the other one: the transaction commits, the hook runs, the broker is unreachable in that
instant, and the work is lost with nothing recording it was ever meant to happen. That is the
failure ADR 0003 exists to prevent, and it is unaffected by which library enqueues.

## Consequences

Good:

- No scheduling loop to own. Beat is the scheduler; the interval and the crontab are two lines of
  settings and can be read without reading any code.
- No in-process date state. Restarting `beat`, or running a second replica, changes nothing about
  when the day-boundary tick fires.
- Retries and backoff come with the library instead of being written into the loop.
- `Ticker` became testable as a plain function of its arguments — call `emit()`, assert one
  `OutboxEvent`. No clock to freeze, no loop to break out of.
- The scheduler is one any Django developer has already operated, and `celery -A config inspect`
  answers "is it scheduling?" without adding instrumentation.

Cost we accepted:

- One process became two. `beat` and `celery-worker` both have to be up for a tick to exist, and
  `beat` failing silently produces no error anywhere — the symptom is scores that stop aging.
- Three more transitive dependencies: `kombu`, `billiard` and a result backend, plus their
  upgrade surface, to schedule two tasks.
- One more thing to run in development. `make up` now starts `beat` and `celery-worker` alongside
  `relay` and `worker`, and the set of processes a newcomer must understand before "why is the UI
  stale?" has a fourth member.
- Redis now has a second, unrelated job. It is the Streams bus *and* the Celery broker and result
  backend, so a Redis outage takes out event delivery and scheduling together, and `redis-cli`
  output now mixes both.
- Two services in compose have names one letter apart in meaning. That is the failure mode this
  ADR spends a paragraph on because a reader who conflates them will debug the wrong process.

## Alternatives considered

- **Keeping the hand-rolled ticker.** Rejected. It works, and the reason to drop it is not the
  loop but the remembered date: correctness of the midnight tick depended on process memory that
  no restart and no second replica preserves.
- **Keeping the loop but moving the date into the database.** Rejected. That is building a
  scheduler — a locked row, a claim, a lease for the replica case — to avoid a dependency that
  already implements it.
- **`cron` in the container, calling a management command.** Rejected. It moves the schedule out
  of the repository into image or host configuration, has no visibility into whether a run
  happened, and gives an interval tick nothing better than the loop it replaces.
- **Celery for the whole event path, dropping the outbox and the consumer groups.** Rejected for
  the two reasons above: it makes producers name their consumers, and `on_commit` leaves the
  commit-without-publish window open.
- **APScheduler in the API process.** Rejected. It puts scheduling inside a process that is
  scaled for request handling, so every extra API replica is another midnight tick.
