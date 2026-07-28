# Aztec Ops — project rules

Operational portfolio manager. Django + django-ninja + PostgreSQL + Redis Streams (outbox)
+ SSE + Astro, all on Docker Compose.

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
2. **State changes only through transitions.** No API route assigns `workflow_state`
   directly. Everything goes through the transition service, which validates against
   `WorkflowTransition`.
3. **Every meaningful change writes an `ActivityRecord`.** State, priority, owner, blocker
   raised or resolved. Append-only. Reprioritizations carry a `correlation_id` so the whole
   decision can be reconstructed.
4. **Events are published through the outbox, never directly.** The service writes
   `OutboxEvent` in the same transaction as the change; the relay publishes. A `service` that
   imports the Redis client is a bug.
5. **Consumers are idempotent.** Deduplicate on `event.id`. At-least-once: the same event
   will arrive twice.
6. **Layers.** `domain/` is pure (no Django). `api/` never imports `models`, it calls
   `services/`. Queries live in `repositories.py`. See §7 of ARCHITECTURE.
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
    on repository and outbox interfaces, not on Redis or a concrete query. Frontend: components
    receive data and emit intent, never fetch; transition buttons and risk flags render from what
    the API returns, so a new workflow state needs zero frontend changes.
15. **Tests are Django `TestCase` classes, grouped by behaviour under test.** No loose module-level
    test functions. Pick the base class deliberately, because it changes what the test can prove:
    `SimpleTestCase` for pure domain logic (it *forbids* database access, so the purity of
    `domain/` is enforced by the test base rather than by discipline); `TestCase` for ordinary
    database tests; `TransactionTestCase` for anything involving the outbox, `on_commit` or the
    relay — `TestCase` wraps each test in a transaction that never commits, so an outbox test
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
**English**. User-facing labels stored in the database may be in Spanish; that is data, not code.

## Commands

```bash
make up          # docker compose up: postgres, redis, api, relay, worker, ticker, frontend
make seed        # loaddata fixtures + recompute scores (idempotent)
make test        # pytest
make lint        # ruff + mypy
make relay       # run the outbox relay in the foreground (debugging)
make down        # stop everything
```

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
