# Aztec Ops — project rules

Operational portfolio manager. Django + django-ninja + PostgreSQL + transactional outbox
drained onto Celery + SSE + Astro, all on Docker Compose.

**Normative spec: `docs/ARCHITECTURE.md`. Read it before writing code in an area you have not
touched yet.** If something contradicts that document, fix one of the two — do not ignore it.

Process docs live in `docs/`: `ARCHITECTURE.md` (what and why), `DATA_MODEL.md` (schema),
`CONTRIBUTING.md` (how to work here, incl. Git Flow), `RUNBOOK.md` (how to operate it),
`API.md`, `EVENTS.md`, `standards/` (code standards and pattern catalogs), `docs/adr/` (decisions).
`PRODUCT.md` owns product truth, `DESIGN.md` owns the visual system. Any agent should be able to
pick up work from those files alone.

## Layout

```
backend/     Django project (apps/, config/, scripts/)
frontend/    Astro project (src/pages, src/components, src/lib)
docs/        Documentation
```

Python commands run as `uv run --project backend <cmd>`.

## Stack

Python 3.12 · Django 6 · django-ninja · PostgreSQL 16 · Redis 7 · uv · pytest · ruff · mypy
· Astro 7 (requires Node >= 22.12) · Docker Compose.

Versions are never pinned by hand in this file. It records the tooling, not the numbers —
`pyproject.toml`, `uv.lock` and `package.json` are the source of truth.

## Dependencies — CLI only

Never hand-edit `pyproject.toml`, `package.json`, or any lockfile to add a dependency. Use the
tool's own CLI so the resolver picks the current version and writes the lockfile:

```bash
uv add --project backend django django-ninja      # runtime
uv add --project backend --dev pytest ruff mypy    # dev
uv remove --project backend <pkg>
npm create astro@latest             # scaffold
npx astro add <integration>         # Astro integrations, via its own command
npm install <pkg>                   # frontend runtime dep
```

Before using a library API, check its current docs with `context7` rather than relying on
memory. Framework majors move; assumed signatures are how a build silently targets a version
that no longer exists.

## Pre-commit

`pre-commit` runs `ruff check --fix` and `ruff format` on staged files, plus `uv lock --check`
so the lockfile can never drift from `pyproject.toml`. Install the hooks once, before your first commit, with
`uv run --project backend pre-commit install`. Do not bypass with `--no-verify`.

## Hard rules

1. **No business enums in code.** States, priorities, types and transitions live in the
   database and are edited from the admin. Code compares against `code` or `category`,
   never against labels.
   Two things are also editable from the product, each behind the ops-lead check and each
   writing an `ActivityRecord`:
   - **Roles**, through `POST`/`PATCH /api/v1/catalog/roles`. A role is the one vocabulary
     somebody needs *while doing something else* — registering a person who does a job nobody
     has typed yet — and a trip to `/admin/` mid-form is how that person gets filed under the
     wrong role permanently.
   - **Workflows**: the graphs themselves, their states and their transitions, plus which
     workflow a given project or task follows. Decided 2026-07-29, superseding the earlier
     "workflows are edited in the admin". The reason is the product's own claim: the operation
     can change without a deploy. A lifecycle that can only be reshaped by someone with a
     Django admin account is not configurable by the operation, it is configurable by
     engineering — and the moment two clients need different lifecycles, the person who knows
     that is the operations lead, not the person with admin rights.

   What did **not** change: code still compares `code` and `category`, never labels; a state
   change still goes through the transition service; and enforcement still reads
   `WorkflowTransition` rows for the record's *resolved* workflow. Authoring a graph and
   obeying it stay separate concerns. The admin remains as a second door, not the only one.
2. **State changes only through transitions.** No API route assigns `workflow_state`
   directly. Everything goes through the transition service, which validates against
   `WorkflowTransition`.
3. **Every meaningful change writes an `ActivityRecord`.** State, priority, owner, blocker
   raised or resolved. Append-only. Reprioritizations carry a `correlation_id` so the whole
   decision can be reconstructed.
4. **Events go through the outbox, never directly.** The service writes `OutboxEvent` in the same
   transaction as the change and stops. Celery is the transport: `events.drain_outbox` claims the
   row and queues one `events.handle_event` per registered handler. A `service` that imports the
   Redis client is a bug, and so is a `service` that calls `.delay()` — naming a task is naming a
   consumer, which is what rule 8 forbids. The producer names a **topic**, never a handler.
5. **Handlers are idempotent, and a handler is a registry entry.** Deduplicate on
   `(event.id, handler)` via `ProcessedEvent`. At-least-once: the same event will arrive twice.
   A handler is a function in `apps/<context>/handlers.py` — the module name is fixed, app-ready
   autodiscovers exactly `handlers`, so one declared elsewhere silently never runs — decorated
   with `@register_handler(name=..., topics={...})`, taking one `EventEnvelope` and returning
   `None`. It runs inside the transaction that holds its claim: it must **raise** on failure (that
   is how the retry, the log line and the dead letter happen) and must never open or commit its
   own transaction. Time comes from `envelope.occurred_at`, never `timezone.now()`. Past
   `EVENT_MAX_ATTEMPTS` the outbox row is dead-lettered and re-queuable from the admin.
6. **Layers.** `domain/` is pure (no Django). `api/` never imports `models`, it calls
   `services/`. See §7 of ARCHITECTURE.
   **Named queries live on the model's `QuerySet`, exposed through its `Manager`** —
   `Task.objects.assigned_to(user).open().overdue()`. Use `Manager.from_queryset()` so every
   named query composes and stays lazy. A module-level `open_tasks_for(user) -> list[Task]`
   cannot be filtered further, so every new combination needs a new function; that is the same
   combinatorial fragmentation that keeps converters off free functions. Django already gives us
   the right tool, and the query belongs on the model you are already holding.
   A `repositories.py` module survives for exactly one case: a query that **spans contexts** and
   therefore belongs to no single model — owner load aggregates `work.Task` keyed by user, and
   `accounts` must not learn that `work.Task` exists. That module lives in the context that
   *consumes* the query, not the one that owns the rows. Anything else goes on the manager.
   **A model knows how to describe itself.** Converting a row into its domain value object is a
   method on the model — `ActivityRecord.to_entry()`, `Project.to_summary()` — never a private
   `_to_entry(record)` function sitting in `repositories.py`. The object owns the mapping of its
   own fields; a free function that reads twelve attributes off an object it was handed is
   procedural code wearing a module for a class, and it fragments as soon as a second caller
   needs the same projection. This is a projection of self, not a business rule, so it does not
   violate "models.py holds persistence only".
7. **Prioritization is deterministic and explainable.** Every score persists its `breakdown`
   with a reason per signal. No LLM in the ranking.
8. **Adding a prioritization signal or a risk criterion = one class + one registry entry.**
   If you have to edit an existing `if`, the design is wrong.
9. **Seed data is Django fixtures.** `manage.py loaddata`, stable primary keys, idempotent.
   No custom importer in the runtime path.

## Code standards

Full rules: `docs/standards/BACKEND.md` and `docs/standards/FRONTEND.md`.
Pattern catalogs: `docs/standards/PATTERNS_BACKEND.md` and `docs/standards/PATTERNS_FRONTEND.md`.

Non-negotiable on both sides:

10. **Pydantic `BaseModel` everywhere, never `dataclass`.** Every structured value that crosses a
    boundary — signal inputs, event envelopes and payloads, domain value objects, service
    commands and results, API schemas — is a `pydantic.BaseModel`. One type system end to end:
    django-ninja schemas *are* Pydantic models, so a domain object can be returned, validated and
    serialized without a translation layer that exists only to restate the same fields. Immutable
    value objects use `model_config = ConfigDict(frozen=True)`. `dataclass` is not used.
11. **Everything is typed.** Python: every function annotated including its return; `mypy --strict`
    over `domain/` and `services/`, where an explicit `Any` is a defect. TypeScript: `strict`, no
    `any`, no `!` non-null assertion — narrow instead. API types are generated from the OpenAPI
    schema, never hand-written.
12. **Everything public is documented.** Google-style docstrings on every public module, class,
    service method, signal and specification; JSDoc on every exported function, store and
    non-obvious prop. The docstring states the invariant and the failure mode — never a
    restatement of the signature. Comments explain *why*; a comment restating the code is deleted.
13. **Impossible states are unrepresentable.** No trio of independent booleans standing in for one
    view state; use a discriminated union. No boolean flag parameter that selects behaviour; split
    the function.
14. **SOLID is checked at review, per side.** Backend: one use case per service; a new priority
    signal or risk criterion is a class plus a registry line, never a new `elif`; services depend
    on the outbox interface and on named queries, not on Redis or an inline `filter()`. Frontend: components
    receive data and emit intent, never fetch; transition buttons and risk flags render from what
    the API returns, so a new workflow state needs zero frontend changes.
15. **Tests are Django `TestCase` classes, grouped by behaviour under test.** No loose module-level
    test functions. Pick the base class deliberately, because it changes what the test can prove:
    `SimpleTestCase` for pure domain logic (it *forbids* database access, so the purity of
    `domain/` is enforced by the test base rather than by discipline); `TestCase` for ordinary
    database tests; `TransactionTestCase` for anything involving the outbox, `on_commit` or the
    drain — `TestCase` wraps each test in a transaction that never commits, so an outbox test
    written on it passes while proving nothing. Shared read-only fixtures go in
    `setUpTestData`, not `setUp`. Full rules in `docs/standards/BACKEND.md`.
16. **Guard clauses over nesting, named constants over magic numbers.** Weights live in
    `PriorityPolicy`, colors and spacing in the design tokens — never inline in a function or a
    template.

Enforced mechanically: `ruff` with `ANN`, `D`, `S`, `TRY`, `RET`, `PL`, `ARG`, `ERA` enabled;
`mypy` strict; `astro check` and TypeScript strict on the frontend. Whatever a linter can catch,
a reviewer should never have to.

## Language

All code, comments, documentation, agents, skills, commit messages and identifiers are in
**English**. That includes file and directory names, and **URL paths** — `/projects`,
`/priorities`, `/board`, never `/proyectos`.

What is in **Spanish** is what a person reads on screen: labels, headings, button text, empty
states, error copy, and the user-facing labels stored in the database. The rule is one line:
the machine speaks English, the interface speaks Spanish.

## Commands

Fifteen targets, and this is the complete list. Anything not here does not exist.

```bash
make help            # list every target with what it does
make up              # docker compose up: postgres, redis, api, worker, beat, frontend.
                     # Migrates and runs `seed` inside the api container before the port is
                     # bound, so one command takes a clean clone to a scored portfolio.
                     # worker = the one Celery worker: drains the outbox, runs every handler and
                     #          the clock ticks. Celery is the bus (ADR 0010) — there is no relay.
                     # beat = Celery Beat, holds the schedule (two ticks + the outbox sweep),
                     #        executes nothing
make down            # stop everything, keeping the volumes
make build           # build the images without starting anything
make ps              # service status and health
make logs            # follow logs — all services, or one with `make logs s=worker`
make reset           # DESTRUCTIVE: drop the volumes and bring the stack back up from empty
make clean           # remove containers, volumes and the built images
make makemigrations  # generate migrations into the bind-mounted source tree
make shell           # Django shell inside the api container
make dbshell         # psql against the compose database
make test            # pytest inside the api container
make lint            # manage.py check + ruff check + ruff format --check + mypy strict
make format          # apply ruff's fixes and formatting
the outbox admin at /admin/events/outboxevent/          # pending / dispatched / dead-lettered counts — replaces XPENDING
```

`.env` must carry `SEED_USER_PASSWORD` and the three `DJANGO_SUPERUSER_*` values **before** the
first `make up`, because seeding happens during `up`: leave them empty and the stack starts fine
but nobody can sign in. They are an upsert, so filling them in and running `make up` again fixes
it — that is also how you re-seed after editing a fixture.

There is deliberately no seed, migrate or superuser target: `up` does all three. There is no
recompute target either — rebuilding scores is an operator action, through the admin action
"Recompute priority for selected projects" or `POST /api/v1/recompute`; a command run from a
laptop against a production database is an accident, not an operation. For a one-off migration
outside `up`, use `docker compose exec api python manage.py migrate`.

## Conventions

- Conventional Commits.
- One migration per logical change, descriptively named.
- Every new endpoint has typed input and output schemas. No bare `dict` responses.
- Domain errors are typed exceptions mapped to HTTP in one central handler, not scattered
  `HttpResponse` returns.

## Subagents

See `.claude/agents/`. Delegate by context: domain modeling, API, event bus, prioritization,
frontend, tests, seed data, review.

## Skills

Canonical in `.agents/skills/`, symlinked into `.claude/skills/`. Never write a skill directly
into `.claude/skills/`.

## Recommended external tooling

- `context7` — current docs for Django, Ninja, Astro. Use it before assuming an API signature.
- `impeccable` / `frontend-craft` — visual direction for the frontend.
- `codebase-memory-mcp` — structural code exploration before reaching for grep.
- `magnific` — visual assets when needed.
