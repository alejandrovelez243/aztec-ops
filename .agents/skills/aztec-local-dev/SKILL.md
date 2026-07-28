---
name: aztec-local-dev
description: Operating Aztec Ops locally — docker compose up/down, migrations, make seed with Django fixtures, tests and lint, Django admin, inspecting the outbox backlog and dead-lettered events, forcing a drain, celery inspect, resetting the database. Load it when a command fails, when the outbox is not draining, when a handler keeps failing, when SSE never reaches the browser, or when loaddata breaks.
---

# Operating Aztec Ops locally

Six services in `docker-compose.yml`, three of them application processes: `postgres` (16),
`redis` (7 — Celery broker plus the `aztec.sse` pub/sub channel, no durable event state), `api`
(Django/ASGI on 8000), `worker` (the single Celery worker: it drains the outbox, runs every handler
— priority-recalculator, risk-evaluator, snapshot-builder, sse-fanout — and executes the clock
ticks), `beat` (Celery Beat: the schedule and the outbox sweep, executes nothing), `frontend`
(Astro on 4321). Python deps are managed with `uv`.

**Celery is the bus** ([ADR 0010](../../../docs/adr/0010-celery-as-the-bus.md)). There is no relay,
no Redis stream, no consumer group and no DLQ stream. `XADD`, `XPENDING` and `XINFO GROUPS` do not
apply here — the outbox table is the instrument.

## Command table

| Command | What it does |
|---|---|
| `make up` | `docker compose up -d` for postgres, redis, api, worker, beat, frontend |
| `make down` | `docker compose down` (keeps volumes) |
| `make logs` | `docker compose logs -f` |
| `make logs-worker` | `docker compose logs -f worker beat` — the whole event path |
| `make outbox` | pending / dispatched / dead-lettered counts from `events_outboxevent` |
| `make migrate` | `docker compose exec api python manage.py migrate` |
| `make seed` | the **only** management command: fixtures + code sequences + recompute |
| `make test` | `docker compose exec api pytest` — the runner is pytest via pytest-django, collecting Django `TestCase` classes natively |
| `make lint` | `ruff check`, `ruff format --check`, and `make typecheck` for mypy |
| `make shell` | `docker compose exec api python manage.py shell` |
| `make reset` | drops volumes, migrates, seeds from scratch |

There is deliberately **no `make recompute` and no `make relay`**. Recompute is the
"Recompute priority for selected projects" admin action or `POST /api/v1/recompute`; a command that
only works from a checkout is not an operation.

Always run management commands inside the `api` container so they see the compose network
(`postgres:5432`, `redis:6379`), not localhost.

## Bring-up from a clean checkout

```bash
cd /Users/alejandrovelezp/aztec-challenge
cp .env.example .env
make up
docker compose exec api python manage.py migrate
make seed
docker compose exec api python manage.py createsuperuser
```

Admin: http://localhost:8000/admin/ — taxonomies (`catalog`), workflows and transitions
(`workflow`), `ProcessedEvent`, and `OutboxEvent` with its pending / dispatched / dead-lettered
filter and the **"Re-queue selected dead-lettered events"** action (which replaced stream replay).
API docs: http://localhost:8000/api/docs. Frontend: http://localhost:4321.

Health check before debugging anything else:

```bash
docker compose ps
docker compose exec postgres pg_isready -U aztec
docker compose exec redis redis-cli PING
curl -s localhost:8000/api/v1/health/live       # process only
curl -s localhost:8000/api/v1/health/ready      # PostgreSQL + Redis, 200 or 503
curl -s localhost:8000/api/v1/health/pipeline   # outbox backlog, dead letters, last tick
```

## Driving the bus by hand

There is no relay to run in the foreground; the drain is a Celery task, so you call it like one.
Nothing competes for rows — the claim is `SELECT ... FOR UPDATE SKIP LOCKED`, so a hand-run drain
and the worker's own take disjoint batches.

```bash
# force one drain pass synchronously and see how many events it dispatched
docker compose exec api python manage.py shell -c \
  "from apps.events.tasks import drain_outbox; print(drain_outbox(batch_size=50))"

# or enqueue it for the worker
docker compose exec api celery -A config call events.drain_outbox

# force a clock tick
docker compose exec api celery -A config call events.emit_interval_tick
```

In another shell, cause an event and watch it go:

```bash
curl -s -X POST localhost:8000/api/v1/projects/PRJ-01/transition \
  -H 'Content-Type: application/json' -H 'X-Actor: camila' \
  -d '{"to_state": "blocked", "reason": "waiting for client access"}'

make logs-worker
```

## Inspecting the bus

The outbox table is the instrument. It replaced `XPENDING` and `XINFO GROUPS`, and it is better:
it lives in PostgreSQL, it survives a Redis restart, and the admin renders it.

```bash
make outbox                                      # pending / dispatched / dead_lettered
curl -s localhost:8000/api/v1/health/pipeline    # the same, plus oldest backlog age and last tick

docker compose exec api celery -A config inspect ping        # is the worker alive
docker compose exec api celery -A config inspect active      # tasks running now
docker compose exec api celery -A config inspect scheduled   # retries waiting on backoff
docker compose exec api celery -A config inspect registered  # did the deploy ship the tasks

docker compose exec redis redis-cli SUBSCRIBE aztec.sse      # fan-out channel
```

Which handlers are registered at all — this catches the one silent failure mode of the design, a
reactor in a module not called `handlers.py`:

```bash
docker compose exec api python manage.py shell -c \
  "from apps.events.registry import registered_handlers; print(registered_handlers())"
```

Anything more specific is SQL (`make dbshell`):

```sql
SELECT id, topic, occurred_at, published_at, attempts, dead_lettered_at
FROM events_outboxevent ORDER BY occurred_at DESC LIMIT 10;

SELECT id, topic, attempts, last_error FROM events_outboxevent
WHERE dead_lettered_at IS NOT NULL ORDER BY dead_lettered_at DESC;

-- which handlers applied a given event: the replacement for XINFO CONSUMERS
SELECT handler, processed_at FROM events_processedevent WHERE event_id = '<uuid>';
```

## Resetting the database

```bash
make down
docker compose down -v            # drops the postgres and redis volumes
make up
docker compose exec api python manage.py migrate
make seed
```

Redis only, keeping the database. There is no stream or consumer group to clear; the dedup table is
what actually makes an already-processed event process again:

```bash
docker compose exec redis redis-cli FLUSHALL          # queued Celery messages only
docker compose exec api python manage.py shell -c \
  "from apps.events.models import ProcessedEvent; ProcessedEvent.objects.all().delete()"
docker compose restart worker
```

To make the outbox re-deliver everything it already dispatched, clear the claim *and* the mark:

```bash
docker compose exec api python manage.py shell -c \
  "from apps.events.models import OutboxEvent, ProcessedEvent; \
   ProcessedEvent.objects.all().delete(); \
   OutboxEvent.objects.update(published_at=None, dead_lettered_at=None, attempts=0)"
```

The next drain re-dispatches. Safe by construction: handlers are idempotent, so the worst case is
recomputing what was already computed.

## Usual failures

**Events written but never dispatched.** `make outbox` first. If `pending` is climbing, the
`worker` is down or cannot reach the broker — `docker compose ps worker`,
`docker compose logs worker`, `celery -A config inspect ping`. If every count is zero after a
transition, the service never wrote to the outbox: that is a service bug, not a transport bug. A
service that imports the Redis client or calls `.delay()` bypasses the outbox entirely — rule 4 of
CLAUDE.md, and a bug rather than a shortcut. If `dispatched` climbs but nothing reacts, the event
was dispatched to *nobody*: check `registered_handlers()` and confirm the module is named
`handlers.py`.

**A handler keeps failing.** `docker compose logs worker | grep -i -A20 traceback`. The log line
carries `event_id`, `topic`, `handler` and `attempt`; `attempts` and `last_error` are on the outbox
row. Retries are automatic with backoff up to `EVENT_MAX_ATTEMPTS`, and
`celery -A config inspect scheduled` shows the ones waiting. Reproduce against the exact payload
before changing code:

```bash
docker compose exec api python manage.py shell -c \
  "from apps.events.models import OutboxEvent; \
   from apps.events.registry import get_handler; \
   from apps.events.tasks import apply_once; \
   row = OutboxEvent.objects.get(id='<uuid>'); \
   print(apply_once(get_handler('risk-evaluator'), row.to_envelope()))"
```

It returns `False` if that pair is already claimed — delete the one `ProcessedEvent` row to re-run.
Reprocessing is safe: deduplication is `(event_id, handler)`.

**An event was dead-lettered.** The row *is* the dead letter — `dead_lettered_at` and `last_error`
set, nothing copied, nothing deleted. Read it in the admin, fix the handler, then use
**"Re-queue selected dead-lettered events"**. Handlers that already applied it dedup; the one that
failed applies it for the first time. Never clear the flag to make a number go down.

**SSE never reaching the browser.** Test the endpoint outside the browser first:

```bash
curl -N -H 'Accept: text/event-stream' localhost:8000/api/stream
```

If curl streams and the browser does not, it is CORS (`CORS_ALLOWED_ORIGINS` must include
`http://localhost:4321`; `EventSource` sends no custom headers, so do not require `X-Actor` on
that route). If curl itself hangs with no output, it is buffering: the endpoint must run on ASGI
(uvicorn, not `runserver` behind WSGI), send an initial comment line and periodic heartbeats, and
any proxy in front needs `proxy_buffering off` plus `X-Accel-Buffering: no` on the response.
If events arrive once and then stop, the browser opened several `EventSource` connections —
there is exactly one shared connection behind the store (§9 of ARCHITECTURE).

**Conflicting migrations.** Two migrations with the same parent produce
`Conflicting migrations detected; multiple leaf nodes`. Do not delete someone's migration:

```bash
docker compose exec api python manage.py makemigrations --merge
docker compose exec api python manage.py showmigrations workflow
```

Before writing a new one, `makemigrations --check --dry-run` tells you whether the model change
is already covered.

**loaddata failing on a foreign key.** Fixture load order matters: `catalog` and `workflows` carry
the rows every other fixture points at. Load them in the documented order
(`catalog workflows portfolio work activity`), never a single glob. `DeserializationError:
Problem installing fixture ... matching query does not exist` means a fixture references a pk that
its own dependency fixture does not define — fix the fixture, do not loosen the FK.

**loaddata duplicating rows.** Fixtures must use explicit stable primary keys. A fixture with
`"pk": null` inserts a new row on every run, so `make seed` twice gives you doubled projects.
Check with:

```bash
grep -c '"pk": null' backend/apps/*/fixtures/*.json
```

Idempotency is a tested property:
`uv run --project backend pytest apps/portfolio -k SeedIdempotencyTests`.

## Running tests

`make test` runs the whole suite in the `api` container. The runner is `pytest` through
`pytest-django`, and the suite is Django `TestCase` classes grouped by behaviour (CLAUDE.md
rule 15) — pytest collects them natively, so selection is by class and method name, not by
function name.

```bash
make test                                                    # whole suite, in the container

uv run --project backend pytest                              # whole suite, on the host
uv run --project backend pytest apps/prioritization          # one app
uv run --project backend pytest apps/prioritization -k DeadlinePressureSignalTests
uv run --project backend pytest apps/prioritization -k "DeadlinePressureSignalTests and saturates"
```

`-k` matches substrings of the test id, which for a `TestCase` includes the class name — so a
class name is the natural unit to select. To run exactly one method, address it by path instead:

```bash
uv run --project backend pytest \
  apps/prioritization/tests/domain/test_deadline_pressure.py::DeadlinePressureSignalTests
uv run --project backend pytest \
  apps/prioritization/tests/domain/test_deadline_pressure.py::DeadlinePressureSignalTests::test_overdue_target_date_saturates_the_signal
```

The same flags work inside the container: `docker compose exec api pytest apps/work -k
OutboxDeliveryTests`. Run it there whenever the test needs `postgres:5432` or `redis:6379`.

Useful while iterating:

```bash
uv run --project backend pytest -x                # stop at the first failure
uv run --project backend pytest --lf              # rerun only what failed last time
uv run --project backend pytest -q --no-header    # quiet output
```

Three things to know when a run looks wrong:

- A `SimpleTestCase` that suddenly errors with a database access message is not a runner problem.
  That base class forbids database access on purpose; the code under test reached the ORM and
  `domain/` is no longer pure.
- A `TransactionTestCase` truncates tables instead of rolling back and does not reuse
  `setUpTestData` caching, so it is slower and it wipes rows other classes seeded. That is the
  price of real commits; keep those classes limited to outbox, `on_commit`, drain and handler
  behaviour.
- Subtests report as one test id. `-k` selects the whole method, not an individual
  `with self.subTest(days=...)` case; read the failure output to see which parameter failed.

## The standards gate is part of the loop

`docs/standards/BACKEND.md` §8 defines the gate; `docs/standards/PATTERNS_BACKEND.md` §11 lists the
antipatterns review rejects. Three rules bite hardest when operating locally:

- **`make lint` runs before the commit, not in CI.** It is `ruff check . && ruff format --check . &&
  mypy` — three commands, all of which must pass. Outside the container the equivalents are
  `uv run --project backend ruff check`, `... ruff format --check` and `... mypy apps`. A red
  `make lint` is a broken build; do not push it and wait for CI to say so.
- **mypy is strict over `domain/` and `services/`.** An explicit `Any` or a `# type: ignore` without
  an error code and a reason comment is a defect there, not a warning. If `mypy` passes only because
  a file is excluded, you have moved the problem, not fixed it.
- **Never `git commit --no-verify`.** The pre-commit hooks run `ruff check --fix`, `ruff format` and
  `uv lock --check`; bypassing them is how the lockfile drifts from `pyproject.toml` and how the
  next `make up` builds a different dependency set than yours. If a hook blocks you, fix the code or
  the lockfile (`uv lock`), then commit again.

Verify before you hand work over:

```bash
make lint
make test
uv lock --check --project backend                              # lockfile matches pyproject.toml
git commit                                                     # hooks run; no --no-verify
```

## Common mistakes

- Running `python manage.py ...` on the host instead of `docker compose exec api ...`, then
  debugging a connection refused to `postgres:5432`.
- Clearing `dead_lettered_at` or deleting `ProcessedEvent` rows before reading the worker
  traceback, which hides a handler bug that comes back on the next event.
- Reaching for `XADD`, `XPENDING` or `XINFO GROUPS`. They no longer apply: `make outbox` and
  `/api/v1/health/pipeline` are the equivalents.
- Using `runserver` to test `/api/stream`. It buffers; the stream looks broken when the code is
  fine.
- `docker compose down -v` when only Redis needed clearing — you lose the seeded database for no
  reason.
- Editing fixture rows by hand to fix a data problem. Fixtures are regenerated by
  `backend/scripts/xlsx_to_fixtures.py`; a hand edit is overwritten on the next regeneration.
- Adding a workflow state through a migration instead of the admin. States are data (rule 1).
- Committing with `--no-verify` because a hook was slow or noisy, then discovering `uv.lock` no
  longer matches `pyproject.toml` when the next `make up` rebuilds the image.
- Debugging "the drain never dispatches in the test" when the test is a `TestCase`. It wraps every
  test in a transaction that never commits, so `on_commit` does not fire and the claim cannot see
  the outbox row. Move the class to `TransactionTestCase`, and narrow the registry with
  `only_handlers(...)` so the event does not fan out to `sse-fanout` and reach Redis.
- Trying to select a single test with `-k test_something` after copying a module-level
  `def test_...` from another project. There are none here; tests are `TestCase` classes, so
  select by class name or by `path::Class::method`.
- Running `make test` and calling it done. `make test` is not `make lint`; ruff format, the `ANN`/`D`
  rules and mypy fail independently of the test suite.
- Silencing a `mypy` error in `domain/` or `services/` with a bare `# type: ignore` or an explicit
  `Any` to get `make lint` green. Both are defects there (BACKEND.md §1); the ignore needs the error
  code and a reason.
- Hand-editing `pyproject.toml` or `uv.lock` to unblock a failing install instead of `uv add` /
  `uv remove` / `uv lock`. The hook catches it; bypassing the hook does not fix it.
