---
name: aztec-local-dev
description: Operating Aztec Ops locally — docker compose up/down, migrations, make seed with Django fixtures, tests and lint, Django admin, running the outbox relay in the foreground, inspecting the aztec.events stream and the DLQ, resetting the database. Load it when a command fails, when the relay does not publish, when a consumer group has stuck pending entries, when SSE never reaches the browser, or when loaddata breaks.
---

# Operating Aztec Ops locally

Six services in `docker-compose.yml`: `postgres` (16), `redis` (7), `api` (Django/ASGI on 8000),
`relay` (outbox relay), `worker` (consumer groups: priority-recalculator, risk-evaluator,
sse-fanout), `web` (Astro 5 on 4321). Python deps are managed with `uv`.

## Command table

| Command | What it does |
|---|---|
| `make up` | `docker compose up -d` for postgres, redis, api, relay, worker, web |
| `make down` | `docker compose down` (keeps volumes) |
| `make logs` | `docker compose logs -f api relay worker` |
| `make migrate` | `docker compose exec api python manage.py migrate` |
| `make seed` | `loaddata catalog workflows portfolio work activity` + `make recompute` |
| `make recompute` | recompute `PriorityScore`, risk flags and `ProjectSnapshot` |
| `make test` | `docker compose exec api pytest` — the runner is pytest via pytest-django, collecting Django `TestCase` classes natively |
| `make lint` | `ruff check . && ruff format --check . && mypy` |
| `make relay` | runs the relay in the foreground with `--verbosity 2` (debugging) |
| `make shell` | `docker compose exec api python manage.py shell` |
| `make reset` | drops volumes, migrates, seeds from scratch |

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
(`workflow`), `OutboxEvent`, `ProcessedEvent` and the DLQ view live there.
API docs: http://localhost:8000/api/docs. Frontend: http://localhost:4321.

Health check before debugging anything else:

```bash
docker compose ps
docker compose exec postgres pg_isready -U aztec
docker compose exec redis redis-cli PING
curl -s localhost:8000/api/health
```

## Running the relay in the foreground

The relay is the only process allowed to `XADD`. To watch it while you trigger a transition:

```bash
docker compose stop relay
make relay        # or: docker compose run --rm relay python manage.py run_outbox_relay --verbosity 2
```

In another shell, cause an event and watch it drain:

```bash
curl -s -X POST localhost:8000/api/projects/PRJ-01/transition \
  -H 'Content-Type: application/json' -H 'X-Actor: camila' \
  -d '{"to_state": "blocked", "reason": "waiting for client access"}'
```

## Inspecting the stream and the DLQ

```bash
docker compose exec redis redis-cli XINFO STREAM aztec.events
docker compose exec redis redis-cli XLEN aztec.events
docker compose exec redis redis-cli XRANGE aztec.events - + COUNT 5
docker compose exec redis redis-cli XINFO GROUPS aztec.events
docker compose exec redis redis-cli XPENDING aztec.events sse-fanout
docker compose exec redis redis-cli XLEN aztec.events.dlq
docker compose exec redis redis-cli XRANGE aztec.events.dlq - + COUNT 10
docker compose exec redis redis-cli SUBSCRIBE aztec.sse     # fan-out channel
```

Undelivered outbox rows (the relay is behind or dead):

```bash
docker compose exec api python manage.py shell -c \
  "from apps.bus.models import OutboxEvent; print(OutboxEvent.objects.filter(published_at__isnull=True).count())"
```

## Resetting the database

```bash
make down
docker compose down -v            # drops the postgres and redis volumes
make up
docker compose exec api python manage.py migrate
make seed
```

Redis only, keeping the database (clears stream, groups and dedup state):

```bash
docker compose exec redis redis-cli DEL aztec.events aztec.events.dlq
docker compose exec api python manage.py shell -c \
  "from apps.bus.models import ProcessedEvent; ProcessedEvent.objects.all().delete()"
```

Deleting `aztec.events` destroys the consumer groups too. The worker recreates them with
`XGROUP CREATE ... MKSTREAM` on start; restart it: `docker compose restart worker`.

## Usual failures

**Relay not publishing.** Check in order: `OutboxEvent` rows with `published_at IS NULL`
(if zero, the service never wrote to the outbox — that is a service bug, not a relay bug);
`docker compose logs relay`; `redis-cli PING` from the `api` container. A service that imports
the Redis client bypasses the outbox and the relay will never see the event — that is rule 4 of
CLAUDE.md and it is a bug, not a shortcut.

**Consumer stuck with pending entries.** `XPENDING aztec.events <group>` shows entries that were
read and never acked, usually because the handler crashed. Inspect and reclaim:

```bash
docker compose exec redis redis-cli XPENDING aztec.events risk-evaluator - + 10
docker compose exec redis redis-cli XAUTOCLAIM aztec.events risk-evaluator worker-1 60000 0
docker compose exec redis redis-cli XACK aztec.events risk-evaluator <entry-id>
```

Do not ack blindly to make the number go down — read the worker traceback first. Reprocessing is
safe: consumers deduplicate on `event.id` via `ProcessedEvent`.

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
  price of real commits; keep those classes limited to outbox, `on_commit` and relay behaviour.
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
- Killing pending entries with `XACK` or `XGROUP DESTROY` before reading the worker traceback,
  which hides a consumer bug that will come back on the next event.
- Running `make relay` while the `relay` container is still up: two publishers compete for the
  same outbox rows. `docker compose stop relay` first.
- Using `runserver` to test `/api/stream`. It buffers; the stream looks broken when the code is
  fine.
- `docker compose down -v` when only Redis needed clearing — you lose the seeded database for no
  reason.
- Editing fixture rows by hand to fix a data problem. Fixtures are regenerated by
  `backend/scripts/xlsx_to_fixtures.py`; a hand edit is overwritten on the next regeneration.
- Adding a workflow state through a migration instead of the admin. States are data (rule 1).
- Committing with `--no-verify` because a hook was slow or noisy, then discovering `uv.lock` no
  longer matches `pyproject.toml` when the next `make up` rebuilds the image.
- Debugging "the relay never publishes in the test" when the test is a `TestCase`. It wraps every
  test in a transaction that never commits, so `on_commit` does not fire and the relay's second
  connection cannot see the outbox row. Move the class to `TransactionTestCase`.
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
