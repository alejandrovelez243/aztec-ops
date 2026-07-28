# 0001 — Django 6 with django-ninja for the API

## Status

Accepted — 2026-07-28.

## Context

The system has two audiences with opposite needs. The operations lead needs a fast, opinionated
triage view over 22 projects and 82 tasks. The operation itself needs to add a workflow state, a
transition, an engagement type or a priority weight without a deploy (ADR 0006). That second
requirement means someone must be able to edit `Workflow`, `WorkflowState`,
`WorkflowTransition`, `EngagementType`, `Priority` and `PriorityPolicy.weights` through a screen
that already exists on day one. Hand-writing CRUD for eight taxonomy tables and a transition
graph is a week of work that produces nothing the reviewer or the operations lead cares about.

At the same time the API surface is small and highly typed: a prioritized queue with a score
breakdown, a project detail with its legal transitions, a transition endpoint, and an SSE
stream. Every response is a declared schema (`CLAUDE.md` conventions), and the frontend
regenerates its TypeScript types from the OpenAPI document rather than transcribing them
(`npx openapi-typescript http://localhost:8000/api/openapi.json`). The pressure is therefore:
free admin CRUD on one side, honest Pydantic schemas and a correct OpenAPI document on the
other.

Django also brings the pieces the event path needs without extra infrastructure: real
`transaction.atomic()` around the aggregate mutation, the `ActivityRecord` and the
`OutboxEvent`, plus an ASGI application so `GET /api/stream` can hold an async generator open
while the rest of the API stays synchronous.

## Decision

Django 6 as the application framework, django-ninja as the HTTP layer.

- The Django admin is the taxonomy and workflow editor. It is a real part of the product, not a
  developer convenience — the RUNBOOK points the operations lead at it.
- All HTTP goes through ninja routers under `backend/apps/<context>/api/`, with Pydantic schemas in
  `schemas.py`. Routers parse, call a service, and return a schema. No querysets, no
  `Project.objects`, no bare `dict` responses.
- Domain errors (`TransitionNotAllowed`, `ReasonRequired`) are typed exceptions mapped to HTTP
  status codes in one central exception handler, not translated at each call site.
- The app is served under ASGI so the SSE endpoint (ADR 0004) is async without a second process.

## Consequences

Good:

- Taxonomy and workflow editing cost zero endpoints. `admin.py` per app is the whole feature.
- The OpenAPI document is generated from the same Pydantic schemas the API validates against, so
  the frontend's `frontend/src/lib/api/types.ts` cannot drift from the server without a regeneration
  diff.
- Migrations, transactions, and the ORM's `select_for_update(skip_locked=True)` — which the
  outbox relay depends on (ADR 0003) — are available with no additional dependency.
- Fixtures and `loaddata` come with the framework, which is what makes ADR 0007 cheap.

Cost we accepted:

- The layering in `docs/ARCHITECTURE.md` §7 fights Django's defaults. Django wants logic in
  models and views; we forbid both, and nothing in the framework enforces that `api/` never
  imports `models` or that `domain/` never imports Django. It holds through review and `mypy`
  strict over `domain/` and `services/`, which is a weaker guarantee than a compiler.
- The admin is admin-shaped. It exposes the transition graph as three related tables, not as a
  graph editor, and a misconfigured `WorkflowTransition` is easy to create there and only fails
  later at the API. We accept that and cover it with integration tests for illegal transitions.
- Two type systems describe the same objects: Pydantic schemas server-side and generated
  TypeScript client-side. The generation step is a manual command that someone will forget.
- ASGI plus a mostly-synchronous ORM means the SSE endpoint has to stay careful about
  `sync_to_async` boundaries; it reads Redis pub/sub only, never the database in a loop.

## Alternatives considered

- **Django REST Framework.** Rejected. Serializers duplicate model definitions with weaker
  typing than Pydantic, its OpenAPI generation needs extra packages and annotations to produce a
  document good enough to generate a client from, and ViewSets pull query logic back into the
  HTTP layer we are explicitly keeping thin.
- **FastAPI with SQLAlchemy.** Rejected. Better async story and the same Pydantic schemas, but
  no admin. Rebuilding admin-editable taxonomies and a workflow editor is the single largest
  chunk of work this decision avoids, and it is exactly the work that produces nothing visible.
- **Django templates with no API layer.** Rejected. It would remove the frontend/backend seam,
  but the SSE live-update requirement (ADR 0004) and the generated typed client both need a JSON
  API, and the read model in §8 is designed to be consumed as JSON.
