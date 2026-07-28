# Runbook — Aztec Ops

How to run the system and what to do when it breaks. Normative spec is `docs/ARCHITECTURE.md`;
this file is the operational companion to it and to the `aztec-local-dev` skill
(`.agents/skills/aztec-local-dev/SKILL.md`).

Everything runs on Docker Compose. Management commands run **inside the `api` container**, so they
resolve `postgres:5432` and `redis:6379` on the compose network. `python manage.py ...` on the host
fails with connection refused and is not worth debugging.

## 1. Compose topology

Eight services in `docker-compose.yml`. `api`, `relay`, `worker`, `beat` and `celery-worker` share
one image and differ only in `command` — one build, five processes.

`worker` and `celery-worker` are different processes and are never interchangeable:

| Service | What it is |
|---|---|
| `worker` | Redis Streams consumer groups (`manage.py run_consumer`). The event bus. |
| `celery-worker` | Executes scheduled Celery tasks. Only ever runs the clock ticks. |
| `beat` | Celery Beat. Holds the schedule, executes nothing. |

| Service | Responsibility | Depends on | Port |
|---|---|---|---|
| `postgres` | PostgreSQL 16. Aggregates, `OutboxEvent`, `ProcessedEvent`, `ActivityRecord`, `PriorityScore`, `ProjectSnapshot` | — | 5432 |
| `redis` | Redis 7. Stream `aztec.events`, dead letter stream `aztec.events.dlq`, pub/sub channel `aztec.sse` | — | 6379 |
| `api` | Django 6 + django-ninja under uvicorn (ASGI). REST API, `GET /api/stream` (SSE), Django admin. Applies migrations on start, then serves | `postgres` healthy, `redis` healthy | 8000 |
| `relay` | Outbox relay. The **only** process allowed to `XADD`. Polls `OutboxEvent` where `published_at IS NULL` with `SELECT ... FOR UPDATE SKIP LOCKED`, publishes to `aztec.events`, stamps `published_at` | `postgres` healthy, `redis` healthy | — |
| `worker` | Consumer groups on `aztec.events`: `priority-recalculator`, `risk-evaluator`, `sse-fanout`, plus the `ProjectSnapshot` rebuild. Creates the groups with `XGROUP CREATE ... MKSTREAM` on start. Moves an entry to `aztec.events.dlq` after N failed retries | `postgres` healthy, `redis` healthy | — |
| `beat` | `celery -A config beat`. Holds `CELERY_BEAT_SCHEDULE`: `emit-interval-tick` every `TICKER_INTERVAL_SECONDS`, `emit-day-boundary-tick` at `crontab(hour=0, minute=0)`. Enqueues, executes nothing, keeps no domain state | `redis` healthy | — |
| `celery-worker` | `celery -A config worker`. Executes the scheduled tasks in `apps/events/tasks.py`; each one calls `Ticker().emit(...)` and writes an `OutboxEvent`. Not the stream consumer — it never reads `aztec.events` | `postgres` healthy, `redis` healthy | — |
| `web` | Astro 7 (Node >= 22.12) serving `/` and `/projects/{code}`. SSR fetches the API by compose service name; the browser reaches the API on `localhost:8000` | `api` healthy | 4321 |

Startup ordering is `depends_on: condition: service_healthy` throughout: `pg_isready` for
`postgres`, `redis-cli ping` for `redis`, `GET /api/health` for `api`. There are no sleeps.

Entry points: app http://localhost:4321 · API docs http://localhost:8000/api/docs · admin
http://localhost:8000/admin/.

## 2. Command table

| Command | What it runs |
|---|---|
| `make up` | `docker compose up -d --build`, waits on healthchecks; `api` migrates on start |
| `make down` | `docker compose down` — stops everything, keeps volumes |
| `make logs` | `docker compose logs -f api relay worker` |
| `make migrate` | `docker compose exec api python manage.py migrate` |
| `make seed` | `loaddata catalog workflows portfolio work activity`, then `make recompute` |
| `make recompute` | recomputes `PriorityScore`, risk flags and `ProjectSnapshot` for every project |
| `make test` | `docker compose exec api pytest` |
| `make lint` | `ruff check .`, `ruff format --check .`, `mypy` |
| `make relay` | runs the outbox relay in the foreground with `--verbosity 2` (debugging) |
| `make shell` | `docker compose exec api python manage.py shell` |
| `make reset` | **destructive** — `docker compose down -v`, then up, migrate, seed |

## 3. From a clean clone

```bash
git clone <repo> aztec-challenge && cd aztec-challenge
cp .env.example .env && make up && make seed
```

`make up` builds the images, starts the eight services, waits for health, and the `api` container
applies migrations before uvicorn binds. `make seed` loads the fixtures and recomputes scores.
There is no third step; if you need one, the setup is broken and belongs to the `devops-engineer`
agent.

Optional, for the admin:

```bash
docker compose exec api python manage.py createsuperuser
```

Verify before doing anything else:

```bash
docker compose ps
docker compose exec postgres pg_isready -U aztec
docker compose exec redis redis-cli PING
curl -s localhost:8000/api/health
curl -s localhost:8000/api/projects | head -c 400
```

Python dependencies are only ever added with `uv add` / `uv add --dev` / `uv remove`, frontend
dependencies with `npm install` or `npx astro add`. Hand-editing `pyproject.toml`,
`package.json` or a lockfile is forbidden — `pre-commit` runs `uv lock --check` and will fail.
Install the hooks once with `uv run pre-commit install`; never commit with `--no-verify`.

## 4. Seeding and reseeding

```bash
make seed
```

which is:

```bash
docker compose exec api python manage.py loaddata catalog workflows portfolio work activity
docker compose exec api python manage.py recompute_scores      # make recompute
```

Order matters: `catalog` and `workflows` carry the rows every later fixture points at. Never load
with a glob.

Reseeding is just running it again. Fixtures use explicit stable primary keys, so `loaddata` is an
upsert: after a second `make seed` the row counts and the primary keys are identical. That is a
tested property:

```bash
docker compose exec api pytest -k seed_idempotency
```

Expected after a clean seed: 22 projects, 82 tasks, 5 people (`accounts.User`), 16 clients.

```bash
docker compose exec api python manage.py shell -c \
  "from apps.accounts.models import User; from apps.portfolio.models import Project, Client; from apps.work.models import Task; \
   print(Project.objects.count(), Task.objects.count(), User.objects.count(), Client.objects.count())"
```

Fixtures are generated, not hand-edited. If the source data changes, regenerate them with
`backend/scripts/xlsx_to_fixtures.py` (developer-only, never in the runtime path) and review the diff.
A hand edit is silently overwritten on the next regeneration.

## 5. Resetting

Full reset, database and Redis:

```bash
make reset
# equivalent to:
docker compose down -v
make up
make seed
```

Redis only, keeping the seeded database — clears the stream, the consumer groups, the DLQ and the
dedup table so the next event is processed as if new:

```bash
docker compose exec redis redis-cli DEL aztec.events aztec.events.dlq
docker compose exec api python manage.py shell -c \
  "from apps.bus.models import ProcessedEvent; ProcessedEvent.objects.all().delete()"
docker compose restart worker
```

Deleting `aztec.events` destroys its consumer groups; the `worker` recreates them with
`XGROUP CREATE ... MKSTREAM` on start, which is why the restart is part of the sequence.

Do not reach for `docker compose down -v` when only Redis needed clearing — you lose the seeded
database for nothing.

## 6. Redis inspection reference

Stream:

```bash
docker compose exec redis redis-cli XLEN aztec.events
docker compose exec redis redis-cli XINFO STREAM aztec.events
docker compose exec redis redis-cli XRANGE aztec.events - + COUNT 5
docker compose exec redis redis-cli XREVRANGE aztec.events + - COUNT 5      # newest first
```

Consumer groups:

```bash
docker compose exec redis redis-cli XINFO GROUPS aztec.events
docker compose exec redis redis-cli XINFO CONSUMERS aztec.events risk-evaluator
```

`XINFO GROUPS` gives `pending` and `lag` per group. `lag` is entries never delivered to the group;
`pending` is entries delivered and never acked. They are different failures.

Pending entries:

```bash
docker compose exec redis redis-cli XPENDING aztec.events risk-evaluator
docker compose exec redis redis-cli XPENDING aztec.events risk-evaluator - + 10
docker compose exec redis redis-cli XPENDING aztec.events risk-evaluator IDLE 60000 - + 10
docker compose exec redis redis-cli XAUTOCLAIM aztec.events risk-evaluator recovery-1 60000 0
docker compose exec redis redis-cli XACK aztec.events risk-evaluator <entry-id>
```

Dead letter stream:

```bash
docker compose exec redis redis-cli XLEN aztec.events.dlq
docker compose exec redis redis-cli XRANGE aztec.events.dlq - + COUNT 10
```

SSE fan-out channel (leave it open in a second shell while you trigger a change):

```bash
docker compose exec redis redis-cli SUBSCRIBE aztec.sse
```

## 7. Running the relay in the foreground

The composed `relay` and a foreground relay compete for the same outbox rows. Stop the container
first.

```bash
docker compose stop relay
make relay
# equivalent to:
docker compose run --rm relay python manage.py run_outbox_relay --verbosity 2
```

In another shell, cause an event and watch it drain:

```bash
curl -s -X POST localhost:8000/api/projects/PRJ-01/transition \
  -H 'Content-Type: application/json' -H 'X-Actor: camila' \
  -d '{"to_state": "blocked", "reason": "waiting for client access"}'
```

When you are done: `docker compose start relay`.

### Driving the clock by hand

There is no foreground ticker any more — `manage.py run_ticker` is gone and `Ticker` is stateless.
The schedule lives in `beat`, the execution in `celery-worker`. To do by hand what they do:

```bash
docker compose exec api celery -A config call events.emit_interval_tick     # force one tick
docker compose logs -f beat                                                 # is it scheduling
docker compose exec api celery -A config inspect scheduled                  # what is queued
```

`celery ... call` enqueues the task; `celery-worker` is what runs it. If the call returns an id and
nothing happens, `celery-worker` is down — that is the point of the two being separate services.

---

# Diagnostics

## 8. The relay is not publishing

**Symptom.** A transition returns 200, the state changed in the API, but `XLEN aztec.events` does
not grow and nothing reaches the UI.

**Checks.**

```bash
docker compose ps relay
docker compose logs --tail=100 relay
docker compose exec api python manage.py shell -c \
  "from apps.bus.models import OutboxEvent; \
   print('unpublished:', OutboxEvent.objects.filter(published_at__isnull=True).count(), \
         'total:', OutboxEvent.objects.count())"
docker compose exec redis redis-cli XLEN aztec.events
docker compose exec api python -c "import redis, os; print(redis.from_url(os.environ['REDIS_URL']).ping())"
```

**Fix**, by what the counts say:

- `total == 0` after a transition: the service never wrote to the outbox. This is a service bug,
  not a relay bug — the service must write `OutboxEvent` inside the same `transaction.atomic()`
  as the mutation (CLAUDE.md rule 4). Check whether the service imports the Redis client and
  publishes directly; that path bypasses the relay entirely and is the bug.
- `unpublished > 0` and growing: the relay is down or crash-looping. Read `docker compose logs
  relay`, fix the cause, `docker compose up -d relay`. Rows are picked up where they were left;
  nothing is lost.
- `unpublished > 0`, relay alive, no errors: the relay cannot reach Redis. The `ping` above fails
  or `REDIS_URL` points at `localhost` instead of `redis`. Fix `.env`, `docker compose up -d relay`.
- `unpublished == 0` and `XLEN` still flat: two relays are running and the other one published.
  `docker compose ps` plus any foreground `make relay` you forgot to stop.

Run the relay in the foreground (§7) to watch a single event go through end to end.

## 9. A consumer group has growing pending entries

**Symptom.** `XLEN aztec.events` grows, and `pending` for one group grows with it. Scores or risk
flags stop updating while the other groups keep working.

**Checks.**

```bash
docker compose exec redis redis-cli XINFO GROUPS aztec.events
docker compose exec redis redis-cli XPENDING aztec.events risk-evaluator - + 10
docker compose logs --tail=200 worker | grep -i -A 20 traceback
```

`XPENDING` gives the entry id, the consumer holding it, and its idle time. Read the payload before
touching anything:

```bash
docker compose exec redis redis-cli XRANGE aztec.events <entry-id> <entry-id>
```

**Fix.**

1. Read the worker traceback first. A pending pile is almost always a handler that raises, or one
   that returns without `XACK` on the duplicate path.
2. Fix the handler, rebuild and restart: `docker compose up -d --build worker`.
3. Reclaim the stranded entries so a live consumer retries them:

```bash
docker compose exec redis redis-cli XAUTOCLAIM aztec.events risk-evaluator recovery-1 60000 0
```

Reprocessing is safe — consumers deduplicate on `(event_id, consumer_group)` via `ProcessedEvent`.

4. Only ack manually when you have read the payload and decided the entry must be abandoned:

```bash
docker compose exec redis redis-cli XACK aztec.events risk-evaluator <entry-id>
```

Never ack to make the number go down, and never `XGROUP DESTROY` to clear it. Both hide a consumer
bug that returns on the next event.

If `lag` is high but `pending` is 0, the group is not consuming at all: the `worker` process for
that group is not running, or it was never registered in the runner.

## 10. An event landed in `aztec.events.dlq`

**Symptom.** `XLEN aztec.events.dlq` is non-zero. The admin shows the entry. `pending` on the
originating group is flat — the runner exhausted its retries and moved on.

**Checks.**

```bash
docker compose exec redis redis-cli XLEN aztec.events.dlq
docker compose exec redis redis-cli XRANGE aztec.events.dlq - + COUNT 10
docker compose logs worker | grep -i "<event-id>"
```

The DLQ entry carries the original envelope plus the failure reason. Read `topic`, `version`,
`entity.id` and the traceback: a DLQ event means the handler failed **deterministically** on that
payload, so replaying it unchanged will fail again.

**Fix.**

1. Reproduce the handler against that payload in a shell before changing code:

```bash
docker compose exec api python manage.py shell
```

2. Fix the handler (or the emitting service, if the payload is malformed for its declared
   `version`), add the regression test, rebuild: `docker compose up -d --build worker`.
3. Drain the DLQ by replaying it onto the main stream, then trim it:

```bash
docker compose exec api python manage.py replay_dlq --limit 100
# check it emptied, then:
docker compose exec redis redis-cli XTRIM aztec.events.dlq MAXLEN 0
```

4. If the entries are unrecoverable (a topic that no longer exists, a payload from a removed
   version), discard them deliberately and say so in the commit:

```bash
docker compose exec redis redis-cli DEL aztec.events.dlq
```

Discarding without reading the payload is how a silent data-loss bug survives. The read side
(`ProjectSnapshot`, `PriorityScore`) can always be rebuilt afterwards with `make recompute`.

## 11. SSE connects but no events reach the browser

**Symptom.** The connection badge shows connected, the network tab shows `/api/stream` pending,
and nothing ever arrives — or events arrive in a burst only when the request finally closes.

**Checks.** Take the browser out of the equation first:

```bash
curl -N -H 'Accept: text/event-stream' localhost:8000/api/stream
```

Then, in a second shell, confirm the fan-out is publishing at all:

```bash
docker compose exec redis redis-cli SUBSCRIBE aztec.sse
```

and trigger a transition (§7).

**Fix**, by what you observed:

- **`curl` hangs with no output at all, not even a comment line** — buffering. `api` must run
  under uvicorn (ASGI). `runserver` and any sync WSGI worker buffer the response and the stream
  looks broken while the code is fine. Check the compose `command` for `api`. Any proxy in front
  needs `proxy_buffering off`, and the response must carry `X-Accel-Buffering: no`.
- **`curl` streams the initial comment then stops, while `SUBSCRIBE aztec.sse` shows messages** —
  the stream view is not flushing per event, or the topic is not in the `sse-fanout` allowlist for
  what you triggered. The endpoint sends an initial comment line on connect and periodic
  heartbeats; without them an idle connection is indistinguishable from a dead one.
- **`SUBSCRIBE aztec.sse` shows nothing** — the problem is upstream of SSE. Go to §8, then §9.
- **`curl` streams but the browser does not** — CORS. `CORS_ALLOWED_ORIGINS` must include
  `http://localhost:4321`. `EventSource` sends no custom headers, so `/api/stream` must not
  require `X-Actor` or any auth header. The browser console shows the blocked-origin error.
- **Events arrive once and then stop** — several `EventSource` objects were opened and the store
  is only reading one. There is exactly one shared connection, owned by
  `frontend/src/lib/stream/store.ts`; islands subscribe to the store and never construct their own
  (ARCHITECTURE §9).
- **Events arrive on the wire but nothing renders** — the topic reaches the fan-out but is not
  registered in `frontend/src/lib/stream/topics.ts` / the store's subscriptions. Published, received,
  ignored.

## 12. The browser reconnects in a loop

**Symptom.** The connection badge flips `connecting → open → disconnected` every few seconds;
the network tab shows a new `/api/stream` request each time.

**Checks.**

```bash
docker compose logs --tail=100 api | grep -i stream
curl -N -sD - -H 'Accept: text/event-stream' localhost:8000/api/stream | head -40
docker compose exec redis redis-cli CLIENT LIST | wc -l
```

Watch how long `curl` survives. If it is killed at a fixed interval, something is closing it on a
timer.

**Fix.**

- A fixed-interval drop is a proxy or server read timeout shorter than the heartbeat interval.
  Shorten the heartbeat or raise the timeout so heartbeats keep the connection warm.
- If the server closes the response after the first event, the view is returning instead of
  yielding — the SSE handler must be an async generator that never completes on its own.
- If the reconnect storm has no backoff, the store is reconnecting immediately. The store owns the
  retry: `min(1000 * 2 ** attempt, 30000)` with jitter, `attempt` reset to 0 on `open`, status set
  to `disconnected` at failure time rather than after the retry fails.
- If every reconnect replays the whole history, the client is not sending its last id.
  Reconnects go to `/api/stream?last_event_id=<id>`; the store keeps its own copy of the last id
  because a freshly constructed `EventSource` does not carry `Last-Event-ID`.
- If the loop only happens with the app open in several tabs, check the per-origin connection cap:
  one connection per tab is expected, one per island is a bug.

## 13. `loaddata` fails on a foreign key, or appears to duplicate rows

**Symptom A.** `DeserializationError: Problem installing fixture ... matching query does not exist`.

**Checks.**

```bash
docker compose exec api python manage.py loaddata catalog workflows portfolio work activity -v 3
docker compose exec api python manage.py showmigrations
```

**Fix.** Load order is `catalog workflows portfolio work activity` and nothing else — `catalog`
and `workflows` define the taxonomy and workflow-state rows every later fixture references. Never
`loaddata backend/apps/*/fixtures/*.json`. If the order is right and it still fails, a fixture references
a primary key that its dependency fixture does not define: fix the fixture (regenerate with
`backend/scripts/xlsx_to_fixtures.py`), never loosen the FK or `--ignorenonexistent` past it.

**Symptom B.** `make seed` twice gives doubled projects, tasks, blockers or activity records.

**Checks.**

```bash
grep -c '"pk": null' backend/apps/*/fixtures/*.json
docker compose exec api python manage.py shell -c \
  "from apps.portfolio.models import Project; from apps.work.models import Task; \
   print(Project.objects.count(), Task.objects.count())"
```

**Fix.** Every fixture object must carry an explicit, stable primary key derived from its natural
key (`project_code`, `task_code`, taxonomy `code`). `"pk": null` inserts a fresh row on every run.
Regenerate the fixtures, then confirm with the test that owns this property:

```bash
docker compose exec api pytest -k seed_idempotency
```

To recover a doubled database, reset rather than deleting rows by hand: `make reset`.

## 14. Migrations conflict

**Symptom.** `Conflicting migrations detected; multiple leaf nodes in the migration graph`,
usually after merging a branch that added a migration to the same app.

**Checks.**

```bash
docker compose exec api python manage.py showmigrations workflow
docker compose exec api python manage.py makemigrations --check --dry-run
```

**Fix.**

```bash
docker compose exec api python manage.py makemigrations --merge
docker compose exec api python manage.py migrate
```

Do not delete someone else's migration to make the graph linear. `--check --dry-run` before
writing a new migration tells you whether your model change is already covered by an existing one.

If the failure is instead `InconsistentMigrationHistory` on a local database you do not care
about, `make reset` is faster than untangling it — never on data you need.

Adding a workflow state or a taxonomy entry is **not** a migration. States, priorities, types and
transitions are data, edited in the Django admin (hard rule 1). A migration that inserts a
`WorkflowState` is the bug.

## 15. Scores look stale after a data change

**Symptom.** A blocker was raised, a due date moved, or a task was reprioritized, and the command
center still shows the old score, the old risk flags or the old ordering.

**Checks.** Walk the chain in order and stop at the first break:

```bash
# 1. did the service write the outbox row?
docker compose exec api python manage.py shell -c \
  "from apps.bus.models import OutboxEvent; print(OutboxEvent.objects.order_by('-id')[:3].values('topic','published_at'))"

# 2. did the relay publish it?
docker compose exec redis redis-cli XREVRANGE aztec.events + - COUNT 3

# 3. did the consumers process it?
docker compose exec redis redis-cli XINFO GROUPS aztec.events
docker compose exec api python manage.py shell -c \
  "from apps.bus.models import ProcessedEvent; print(ProcessedEvent.objects.order_by('-id')[:5].values('event_id','consumer_group'))"

# 4. is the score itself fresh?
docker compose exec api python manage.py shell -c \
  "from apps.prioritization.models import PriorityScore; \
   s = PriorityScore.objects.order_by('-computed_at').first(); \
   print(s.project_id, s.value, s.policy_version, s.computed_at)"
```

**Fix**, by where it broke:

- No outbox row → the change did not go through a service, or the service did not emit. See §8.
- Outbox row unpublished → §8.
- Published but no `ProcessedEvent` for `priority-recalculator` → §9.
- `PriorityScore` fresh but the UI stale → the read model did not rebuild. `ProjectSnapshot` is
  what the read API queries (ARCHITECTURE §8); the rebuild consumer reacts to any event with
  `entity.type == "project"`. If your new topic is not reaching it, the snapshot keeps the old
  join and the UI is correct about a stale table.
- Everything fresh in the database but stale on screen → the browser never got the event. §11.

Unconditional repair, safe to run at any time:

```bash
make recompute
```

It recomputes `PriorityScore`, the risk flags and `ProjectSnapshot` for every project from current
state. It is a repair tool, not a workaround: if you need it after every change, the event chain is
broken and one of §8–§10 applies.

If the score changed but the number looks wrong rather than stale, the active `PriorityPolicy`
version may have moved. Every `PriorityScore` persists `policy_version` and its per-signal
`breakdown`; compare the two rows before blaming the engine, and check whether a
`PriorityOverride` is in play — an override is labelled as manual and never rendered as a
computed score.

## 16. Scores are not refreshing on their own

**Symptom.** Nothing is wrong with the data and a manual change still propagates fine, but a score
that should move only because time passed does not: an overdue project stays at its old
`deadline_pressure`, a stale one never picks up `staleness`, and the day boundary comes and goes
with no recompute. Nobody touched anything, so nothing was emitted — that is exactly what the
clock exists to fix.

**Checks.** Walk the chain in order and stop at the first break.

```bash
# 1. is beat running and logging its schedule?
docker compose ps beat
docker compose logs --tail=50 beat

# 2. is celery-worker consuming what beat enqueues?
docker compose ps celery-worker
docker compose logs --tail=50 celery-worker
docker compose exec api celery -A config inspect scheduled
docker compose exec api celery -A config call events.emit_interval_tick

# 3. did the task write an OutboxEvent?
docker compose exec api python manage.py shell -c \
  "from apps.events.models import OutboxEvent; \
   print(OutboxEvent.objects.filter(topic='clock.ticked').order_by('-id')[:3].values('id','published_at'))"

# 4. did the relay publish it?
docker compose exec redis redis-cli XREVRANGE aztec.events + - COUNT 3

# 5. is anything in the DLQ?
docker compose exec redis redis-cli XLEN aztec.events.dlq
```

**Fix**, by where it broke:

- `beat` down or crash-looping → nothing is scheduled at all. Read its log; a bad `crontab` entry
  or an unreachable broker fails at startup. `docker compose up -d beat`.
- `beat` logging sends but no `OutboxEvent` → `celery-worker` is not consuming. Check it is up and
  that both point at the same `REDIS_URL`. `celery -A config call` returning an id while nothing
  runs is the same finding.
- `OutboxEvent` written but `published_at` stays null → this is the relay, not the clock. §8.
- Published but scores unchanged → `priority-recalculator` is not processing `clock.ticked`. §9.
- Entries in `aztec.events.dlq` → the handler fails deterministically on the tick payload. §10.

Two ticks are scheduled and they fail differently: `emit-interval-tick` runs every
`TICKER_INTERVAL_SECONDS`, so its absence shows up within a minute; `emit-day-boundary-tick` runs
at `crontab(hour=0, minute=0)` under `CELERY_TIMEZONE`, so a wrong timezone looks like a working
system that recomputes the day at the wrong hour. Compare `CELERY_TIMEZONE` with `TIME_ZONE`
before blaming the schedule.

`CELERY_TASK_ACKS_LATE = False`, so a tick lost to a `celery-worker` crash is not redelivered. That
is deliberate — a tick is worthless once the next one is due. Do not chase a single missing tick;
chase a pattern of them.
