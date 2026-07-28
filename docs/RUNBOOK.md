# Runbook — Aztec Ops

How to run the system and what to do when it breaks. Normative spec is `docs/ARCHITECTURE.md`;
this file is the operational companion to it and to the `aztec-local-dev` skill
(`.agents/skills/aztec-local-dev/SKILL.md`).

Everything runs on Docker Compose. Management commands run **inside the `api` container**, so they
resolve `postgres:5432` and `redis:6379` on the compose network. `python manage.py ...` on the host
fails with connection refused and is not worth debugging.

## 1. Compose topology

Six services in `docker-compose.yml`. `api`, `worker` and `beat` share one image and differ only in
`command` — one build, **three application processes**.

There is exactly one worker. It is the whole bus: it drains the outbox, it runs every handler, and
it runs the clock ticks. ([ADR 0010](adr/0010-celery-as-the-bus.md) explains why there used to be
four workers and a relay, and why there is now one.)

| Service | Responsibility | Depends on | Host port |
|---|---|---|---|
| `postgres` | PostgreSQL 16. Aggregates, `OutboxEvent`, `ProcessedEvent`, `ActivityRecord`, `PriorityScore`, `ProjectSnapshot` | — | `${POSTGRES_PORT:-55433}` |
| `redis` | Redis 7. Two jobs and no durable event state: the **Celery broker/result backend**, and the pub/sub channel `aztec.sse` | — | `${REDIS_PORT:-56379}` |
| `api` | Django 6 + django-ninja under uvicorn (ASGI). REST API, `GET /api/stream` (SSE), Django admin. Applies migrations on start, then serves | `postgres` healthy, `redis` healthy | `${API_PORT:-8000}` |
| `worker` | `celery -A config worker --concurrency 2`. **The bus.** Runs `events.drain_outbox` (claims unpublished `OutboxEvent` rows with `SELECT ... FOR UPDATE SKIP LOCKED`, marks them published, fans one `events.handle_event` out per registered handler), the handler tasks themselves, and the two clock ticks | `postgres` healthy, `redis` healthy | — |
| `beat` | `celery -A config beat`. Holds `CELERY_BEAT_SCHEDULE`: `emit-interval-tick` every `TICKER_INTERVAL_SECONDS`, `emit-day-boundary-tick` at `crontab(hour=0, minute=0)`, and `drain-outbox` every `EVENT_DRAIN_INTERVAL_SECONDS`. Enqueues, executes nothing, keeps no domain state | `redis` healthy | — |
| `frontend` | Astro 7 (Node >= 22.12) serving `/` and `/projects/{code}`. SSR fetches the API by compose service name; the browser reaches the API on `localhost:8000` | `api` healthy | `${FRONTEND_PORT:-4321}` |

Two things about the topology are worth internalising before debugging anything:

- **`beat` down is not `worker` down.** `beat` down means no clock ticks and no sweep, so
  time-derived scores stop aging but data changes still propagate — `enqueue_event` kicks the drain
  on commit. `worker` down means events accumulate in the outbox, unpublished, and nothing reacts to
  anything. In both cases the events are safe; the outbox is the durability guarantee.
- **Redis holds nothing you need to keep.** Losing the append-only file loses queued task messages,
  which delays events by one Beat sweep. It cannot lose an event: the row is in PostgreSQL.

Startup ordering gates on the backing services: `pg_isready` for `postgres`, `redis-cli ping` for
`redis`, both `condition: service_healthy`. There are no sleeps. The application services carry no
container healthcheck — `worker` and `frontend` wait on `api` with `condition: service_started`, so
a worker that starts before `api` finishes migrating dies on a ProgrammingError and is recovered by
`restart: on-failure:5`.

The health endpoints remain, for orchestrators and load balancers rather than for Compose.
`GET /api/v1/health/live` is **liveness**: it touches neither PostgreSQL nor Redis, because a probe
that fails when a dependency blips turns an outage into a restart loop and kills every open SSE
connection. `GET /api/v1/health/ready` is the dependency-checking probe, for load balancers that
drain rather than kill. Never point a liveness probe at `/api/v1/health/pipeline`.

Entry points: app http://localhost:4321 · API docs http://localhost:8000/api/docs · admin
http://localhost:8000/admin/ · outbox http://localhost:8000/admin/events/outboxevent/.

## 2. Command table

| Command | What it runs |
|---|---|
| `make up` | `docker compose up -d --build`, waits on healthchecks; `api` migrates on start |
| `make down` | `docker compose down` — stops everything, keeps volumes |
| `make logs` | `docker compose logs -f` |
| `make logs-api` | follow just the API |
| `make logs-worker` | `docker compose logs -f worker beat` — the whole event path |
| `make outbox` | pending / dispatched / dead-lettered counts straight out of `events_outboxevent` |
| `make migrate` | `docker compose exec api python manage.py migrate` |
| `make seed` | `manage.py seed`: `loaddata`, sync the code sequences, recompute the portfolio |
| `make test` | `pytest -q` inside the `api` container |
| `make test-local` | `pytest -q` on the host, against the published PostgreSQL port |
| `make lint` / `make typecheck` / `make check` | ruff, mypy, and the full pre-push gate |
| `make shell` / `make dbshell` / `make superuser` | Django shell, `psql`, admin user |
| `make reset` | **destructive** — `docker compose down -v`, then up, migrate, seed |

`seed` is the **only** management command in the system, and that is deliberate: a command someone
runs from a laptop against a production database is not an operation, it is an accident. Seeding is
bootstrap, it runs inside the container at deploy time, and it is an upsert. Everything that used to
be a command is now either automatic or an operator action with an audit trail:

| Gone | Use instead |
|---|---|
| `manage.py recompute` / `make recompute` | the **"Recompute priority for selected projects"** admin action, or `POST /api/v1/projects/{code}/recompute` / `POST /api/v1/recompute` |
| `manage.py sync_code_sequences` | runs automatically as a step inside `seed`, where nobody can forget it |
| `manage.py run_relay` / `make relay` | deleted with the Streams machinery — the drain is a Celery task |
| `manage.py run_consumer` | deleted with the Streams machinery — handlers are Celery tasks |
| `make events` / `make dlq` / `make logs-bus` | `make outbox`, `GET /api/v1/health/pipeline`, `make logs-worker` |

## 3. From a clean clone

```bash
git clone <repo> aztec-challenge && cd aztec-challenge
cp .env.example .env && make up && make seed
```

`make up` builds the images, starts the six services, waits for health, and the `api` container
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
curl -s localhost:8000/api/v1/health/live       # process only
curl -s localhost:8000/api/v1/health/ready      # PostgreSQL + Redis, 200 or 503
curl -s localhost:8000/api/v1/health/pipeline   # outbox backlog, dead letters, last tick
curl -s localhost:8000/api/v1/projects | head -c 400
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
docker compose exec api python manage.py seed
```

which loads the fixtures in foreign-key dependency order, realigns the business-code sequences and
rebuilds every score. All three steps live in `apps/portfolio/management/commands/seed.py`; the
sequence sync is *inside* the command rather than beside it, because a step an operator can forget
is a step that will be forgotten.

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

Redis only, keeping the seeded database. There is no stream and no consumer group to clear any
more, so the only reason to touch Redis is to drop queued task messages; the dedup table is what
actually makes an already-processed event process again:

```bash
docker compose exec redis redis-cli FLUSHALL          # queued Celery messages only
docker compose exec api python manage.py shell -c \
  "from apps.events.models import ProcessedEvent; ProcessedEvent.objects.all().delete()"
docker compose restart worker
```

To make the outbox re-deliver events it already dispatched, clear the claim *and* the dispatch mark:

```bash
docker compose exec api python manage.py shell -c \
  "from apps.events.models import OutboxEvent, ProcessedEvent; \
   ProcessedEvent.objects.all().delete(); \
   OutboxEvent.objects.update(published_at=None, dead_lettered_at=None, attempts=0)"
```

The next drain — within `EVENT_DRAIN_INTERVAL_SECONDS` — re-dispatches everything. This is safe by
construction: handlers are idempotent, so the worst case is that they recompute what they already
computed.

Do not reach for `docker compose down -v` when only Redis needed clearing — you lose the seeded
database for nothing.

## 6. Bus inspection reference

There is no stream and no consumer group to inspect. **The outbox table is the instrument**, and
that is the deliberate trade in [ADR 0010](adr/0010-celery-as-the-bus.md): we gave up `XPENDING`
and `XINFO GROUPS` and got a queryable, durable, admin-rendered table in exchange.

Backlog and dead letters — the first command in almost every investigation:

```bash
make outbox
```

```
 pending | dispatched | dead_lettered
---------+------------+---------------
       0 |       1284 |             0
```

The same numbers over HTTP, plus the age of the oldest unpublished row and the last clock tick.
This endpoint always returns 200 — it reports, it does not judge, which is why nothing should ever
point a container healthcheck at it:

```bash
curl -s localhost:8000/api/v1/health/pipeline
```

```json
{"unpublished": 0, "oldest_unpublished_age_seconds": null, "dead_lettered": 0,
 "last_tick_at": "2026-07-28T09:15:00Z", "last_tick_age_seconds": 42.1}
```

Anything more specific is SQL:

```bash
make dbshell
```

```sql
-- the ten most recent events and whether they were dispatched
SELECT id, topic, occurred_at, published_at, attempts, dead_lettered_at
FROM events_outboxevent ORDER BY occurred_at DESC LIMIT 10;

-- what is stuck, oldest first
SELECT id, topic, entity_id, occurred_at, attempts, last_error
FROM events_outboxevent
WHERE published_at IS NULL AND dead_lettered_at IS NULL
ORDER BY occurred_at LIMIT 20;

-- what died, and why
SELECT id, topic, entity_id, attempts, dead_lettered_at, last_error
FROM events_outboxevent WHERE dead_lettered_at IS NOT NULL ORDER BY dead_lettered_at DESC;

-- which handlers have applied a given event (the replacement for XINFO CONSUMERS)
SELECT handler, processed_at FROM events_processedevent WHERE event_id = '<uuid>';
```

Or in the admin, which filters on exactly those three states and carries the re-queue action:
http://localhost:8000/admin/events/outboxevent/.

Celery, for the half of the path that is in flight rather than in the table:

```bash
docker compose exec api celery -A config inspect ping         # is the worker alive
docker compose exec api celery -A config inspect active       # tasks running right now
docker compose exec api celery -A config inspect scheduled    # retries waiting on their countdown
docker compose exec api celery -A config inspect registered   # tasks the worker knows about
docker compose exec api celery -A config inspect stats
```

`inspect scheduled` is where a retrying `events.handle_event` sits between attempts, and
`inspect registered` is the fastest way to confirm a deploy actually shipped the tasks.

Which handlers are registered at all — the check that catches a reactor in a module that is not
called `handlers.py`, the one failure mode that produces no error anywhere:

```bash
docker compose exec api python manage.py shell -c \
  "from apps.events.registry import registered_handlers, handlers_for; \
   print(registered_handlers()); \
   print([r.name for r in handlers_for('project.state_changed')])"
```

SSE fan-out channel (leave it open in a second shell while you trigger a change):

```bash
docker compose exec redis redis-cli SUBSCRIBE aztec.sse
```

## 7. Driving the bus by hand

There is no relay to run in the foreground any more; the drain is a Celery task, so you call it
like one. Nothing competes for rows — the claim is `SELECT ... FOR UPDATE SKIP LOCKED`, so a
hand-run drain and the worker's own take disjoint batches.

```bash
# force one drain pass, synchronously, and see how many events it dispatched
docker compose exec api python manage.py shell -c \
  "from apps.events.tasks import drain_outbox; print(drain_outbox(batch_size=50))"

# or enqueue it for the worker to run
docker compose exec api celery -A config call events.drain_outbox
```

In another shell, cause an event and watch it go:

```bash
curl -s -X POST localhost:8000/api/v1/projects/PRJ-01/transition \
  -H 'Content-Type: application/json' -H 'X-Actor: camila' \
  -d '{"to_state": "blocked", "reason": "waiting for client access"}'

make logs-worker
```

Apply one handler to one event by hand, which is the fastest reproduction of a handler bug — note
that it is `apply_once`, so it is a no-op if that pair is already in `ProcessedEvent`:

```bash
docker compose exec api python manage.py shell -c \
  "from apps.events.models import OutboxEvent; \
   from apps.events.registry import get_handler; \
   from apps.events.tasks import apply_once; \
   row = OutboxEvent.objects.get(id='<uuid>'); \
   print(apply_once(get_handler('risk-evaluator'), row.to_envelope()))"
```

### Driving the clock by hand

`Ticker` is stateless and there is no ticker process. The schedule lives in `beat`, the execution
in `worker` — the same worker that runs everything else.

```bash
docker compose exec api celery -A config call events.emit_interval_tick     # force one tick
docker compose logs -f beat                                                 # is it scheduling
docker compose exec api celery -A config inspect scheduled                  # what is queued
```

`celery ... call` enqueues; `worker` runs it. If the call returns an id and nothing happens, the
worker is down.

---

# Diagnostics

## 8. Events are written but never dispatched

**Symptom.** A transition returns 200, the state changed in the API, but nothing reacts: no score
moves, no snapshot rebuilds, nothing reaches the UI. `make outbox` shows `pending` climbing.

**Checks.**

```bash
make outbox
docker compose ps worker
docker compose logs --tail=100 worker
docker compose exec api celery -A config inspect ping
docker compose exec api python -c "import redis, os; print(redis.from_url(os.environ['REDIS_URL']).ping())"
```

**Fix**, by what the counts say:

- **`pending == 0` and `dispatched == 0` after a transition** — the service never wrote to the
  outbox. This is a service bug, not a transport bug: the service must write `OutboxEvent` inside
  the same `transaction.atomic()` as the mutation (CLAUDE.md rule 4). If the service imports the
  Redis client, or calls `.delay()` itself, that is the bug — it bypasses the outbox entirely.
- **`pending > 0` and growing** — the worker is down or crash-looping. Read `docker compose logs
  worker`, fix the cause, `docker compose up -d worker`. Rows are picked up where they were left;
  nothing is lost, and this is exactly why the outbox exists.
- **`pending > 0`, worker alive, `inspect ping` fails** — the worker cannot reach the broker.
  `REDIS_URL` points at `localhost` instead of `redis`, or Redis is down. Fix `.env`,
  `docker compose up -d worker`.
- **`pending > 0`, worker alive, `inspect ping` fine** — Beat is not sweeping *and* the on-commit
  kick failed. Check `docker compose ps beat` and confirm the `drain-outbox` entry is in
  `CELERY_BEAT_SCHEDULE`. Force a pass by hand (§7): if that drains it, the schedule is the
  problem, not the drain.
- **`dispatched` is climbing but nothing reacts** — the events were dispatched to *nobody*. The
  handler is not registered: check `registered_handlers()` (§6) and confirm the module is named
  `handlers.py`. A reactor in `reactors.py` is never imported and fails silently, which is the one
  failure mode of this design that produces no error line anywhere.

## 9. A handler keeps failing

**Symptom.** `make outbox` shows `dispatched` climbing normally, but one concern stops updating —
scores freeze while snapshots keep rebuilding, or the reverse. The worker log has a repeating
traceback.

**Checks.**

```bash
docker compose logs --tail=200 worker | grep -i -B5 -A20 traceback
docker compose exec api celery -A config inspect scheduled   # retries waiting on backoff
```

```sql
-- which handlers applied a recent event, and which one is missing
SELECT o.id, o.topic, p.handler
FROM events_outboxevent o LEFT JOIN events_processedevent p ON p.event_id = o.id
WHERE o.occurred_at > now() - interval '10 minutes'
ORDER BY o.occurred_at DESC;

-- attempts and the last error on the row itself
SELECT id, topic, attempts, last_error FROM events_outboxevent
WHERE attempts > 0 ORDER BY occurred_at DESC LIMIT 10;
```

A row with no `ProcessedEvent` for one handler and `attempts > 0` is that handler failing. The log
line carries `event_id`, `topic`, `handler` and `attempt` — grep the event id.

**Fix.**

1. Read the traceback first. A handler that raises is behaving correctly; a handler that swallows
   its own exception to "keep things moving" is the actual bug, because the event is then marked
   applied and lost.
2. Reproduce it against that exact payload with `apply_once` (§7) before changing code. If the pair
   is already claimed you will get `False` — delete that one `ProcessedEvent` row to re-run it.
3. Fix the handler, rebuild and restart: `docker compose up -d --build worker`.
4. Retries are automatic with backoff up to `EVENT_MAX_ATTEMPTS` (default 5). Do not clear anything
   to "make the number go down": reprocessing is safe — deduplication is
   `ProcessedEvent(event_id, handler)` — but hiding a failing handler is how stale derived state
   survives a deploy.

If the handler was never called at all rather than failing, it is not subscribed to that topic.
Check its `topics=` set against the §5 table in `docs/EVENTS.md` — a handler subscribed to nothing
relevant produces no error, only silence.

## 10. An event was dead-lettered

**Symptom.** `make outbox` shows a non-zero `dead_lettered`. The admin's delivery-state filter
lists the rows. The worker log carries one ERROR per dead letter, with the event id, topic and
handler.

**Checks.**

```bash
make outbox
docker compose logs worker | grep -i "dead lettered"
```

```sql
SELECT id, topic, entity_id, version, attempts, dead_lettered_at, last_error, payload
FROM events_outboxevent WHERE dead_lettered_at IS NOT NULL ORDER BY dead_lettered_at DESC;
```

Or read it in the admin, which renders the payload: http://localhost:8000/admin/events/outboxevent/.

The row *is* the dead letter. Nothing was copied anywhere and nothing was deleted — it carries its
own topic, payload, correlation id, `attempts` and `last_error`. A dead-lettered event means a
handler failed **deterministically** on that payload five times, so re-queuing it unchanged will
fail again. Fix the code first.

**Fix.**

1. Reproduce the handler against that payload with `apply_once` (§7).
2. Fix the handler — or the emitting service, if the payload is malformed for its declared
   `version` — add the regression test, rebuild: `docker compose up -d --build worker`.
3. Re-queue: select the rows in the admin and run **"Re-queue selected dead-lettered events"**. It
   clears `dead_lettered_at` and resets `published_at`, so the next drain dispatches the same
   envelope with the same `event.id`. Handlers that already applied it dedup on `ProcessedEvent`;
   the one that failed applies it for the first time. This is what replaced stream replay, and it
   is narrower and safer: you re-queue the events you chose, not a time window.
4. If the events are unrecoverable (a topic that no longer exists, a payload from a removed
   version), leave them dead-lettered and say so in the commit. They are a record, and a record
   nobody can act on is still better than a deletion nobody can see. Deleting without reading the
   payload is how a silent data-loss bug survives.

The read side (`ProjectSnapshot`, `PriorityScore`) can always be rebuilt afterwards from current
state with the **"Recompute priority for selected projects"** admin action or
`POST /api/v1/recompute`.

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
  the stream view is not flushing per event, or the topic is not in the `sse-fanout` allowlist
  (`SSE_ALLOWLIST_TOPICS` in `apps/events/domain/envelope.py`) for what you triggered. The endpoint sends an initial comment line on connect and periodic
  heartbeats; without them an idle connection is indistinguishable from a dead one.
- **`SUBSCRIBE aztec.sse` shows nothing** — the problem is upstream of SSE: either the event was
  never dispatched (§8) or `sse-fanout` is failing or unregistered (§9). `SELECT handler FROM
  events_processedevent WHERE event_id = '<uuid>'` tells you which.
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
  "from apps.events.models import OutboxEvent; print(OutboxEvent.objects.order_by('-occurred_at')[:3].values('topic','published_at','dead_lettered_at'))"

# 2. did the drain dispatch it?
make outbox

# 3. did the handlers apply it?
docker compose exec api python manage.py shell -c \
  "from apps.events.models import ProcessedEvent; print(ProcessedEvent.objects.order_by('-processed_at')[:5].values('event_id','handler'))"

# 4. is the score itself fresh?
docker compose exec api python manage.py shell -c \
  "from apps.prioritization.models import PriorityScore; \
   s = PriorityScore.objects.order_by('-computed_at').first(); \
   print(s.project_id, s.value, s.policy_version, s.computed_at)"
```

**Fix**, by where it broke:

- No outbox row → the change did not go through a service, or the service did not emit. See §8.
- Outbox row unpublished → §8.
- Dispatched but no `ProcessedEvent` for `priority-recalculator` → §9, or it is dead-lettered, §10.
- `PriorityScore` fresh but the UI stale → the read model did not rebuild. `ProjectSnapshot` is
  what the read API queries (ARCHITECTURE §8); the `snapshot-builder` handler reacts to every
  topic except `clock.ticked`. If your new topic is not reaching it, the snapshot keeps the old
  join and the UI is correct about a stale table.
- Everything fresh in the database but stale on screen → the browser never got the event. §11.

Unconditional repair, safe to run at any time — and deliberately **not** a make target, because a
command that only works from a checkout is not an operation:

```bash
curl -s -X POST localhost:8000/api/v1/recompute -H 'X-Actor: camila'
# or, per project:
curl -s -X POST localhost:8000/api/v1/projects/PRJ-01/recompute -H 'X-Actor: camila'
```

The same thing lives in the admin as **"Recompute priority for selected projects"**. Both recompute
`PriorityScore` and the risk flags from current state and report how many actually changed. It is a
repair tool, not a workaround: if you need it after every change, the event chain is broken and one
of §8-§10 applies. Note that it emits no event, so an open dashboard will not see the new score
until its next legitimate event.

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

# 2. is the worker consuming what beat enqueues?
docker compose ps worker
docker compose logs --tail=50 worker
docker compose exec api celery -A config inspect scheduled
docker compose exec api celery -A config call events.emit_interval_tick

# 3. did the task write an OutboxEvent?
docker compose exec api python manage.py shell -c \
  "from apps.events.models import OutboxEvent; \
   print(OutboxEvent.objects.filter(topic='clock.ticked').order_by('-id')[:3].values('id','published_at'))"

# 4. did the drain dispatch it, and did anything die?
make outbox
curl -s localhost:8000/api/v1/health/pipeline
```

`last_tick_age_seconds` in that response is the single fastest answer to "is the clock alive?" —
if it is much larger than `TICKER_INTERVAL_SECONDS`, stop here and look at `beat`.

**Fix**, by where it broke:

- `beat` down or crash-looping → nothing is scheduled at all. Read its log; a bad `crontab` entry
  or an unreachable broker fails at startup. `docker compose up -d beat`.
- `beat` logging sends but no `OutboxEvent` → the `worker` is not consuming. Check it is up and
  that both point at the same `REDIS_URL`. `celery -A config call` returning an id while nothing
  runs is the same finding.
- `OutboxEvent` written but `published_at` stays null → this is the drain, not the clock. §8.
- Dispatched but scores unchanged → `priority-recalculator` is not processing `clock.ticked`. §9.
- `dead_lettered > 0` → the handler fails deterministically on the tick payload. §10.

Two ticks are scheduled and they fail differently: `emit-interval-tick` runs every
`TICKER_INTERVAL_SECONDS`, so its absence shows up within a minute; `emit-day-boundary-tick` runs
at `crontab(hour=0, minute=0)` under `CELERY_TIMEZONE`, so a wrong timezone looks like a working
system that recomputes the day at the wrong hour. Compare `CELERY_TIMEZONE` with `TIME_ZONE`
before blaming the schedule.

`CELERY_TASK_ACKS_LATE` is `True` now that Celery is the bus (a delivery task must survive a worker
kill), so a tick lost mid-flight *is* redelivered. It costs nothing: a tick's whole effect is an
outbox row, and a duplicated tick recomputes the same projects to the same numbers. Do not chase a
single missing tick; chase a pattern of them.
