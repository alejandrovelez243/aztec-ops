---
name: django-clean-arch
description: How to lay out and write code inside an Aztec Ops Django app (domain/, models.py, repositories.py, services/, api/, admin.py, consumers/). Load when adding or modifying a use case, an endpoint, a repository, a model or an event consumer in backend/apps/catalog, backend/apps/workflow, backend/apps/portfolio, backend/apps/work, backend/apps/activity, backend/apps/prioritization or backend/apps/events — or when reviewing whether the §7 dependency rules hold.
---

# Layered Django in Aztec Ops

Normative source: `docs/ARCHITECTURE.md` §6, §7, §8. This skill is the operational version of it.

## Standards that bite when you cut the layers

Binding: `docs/standards/BACKEND.md` and `docs/standards/PATTERNS_BACKEND.md`. Three rules decide
whether a layering change is correct or only looks correct.

1. **Repository return types are materialized and annotated** (BACKEND §1). Every repository
   function has parameter and return annotations. A function annotated `-> list[Blocker]` returns
   `list(...)`, never the lazy `QuerySet` — otherwise the query runs in the caller's layer and the
   annotation is false. Never return a `QuerySet` a service can extend; that moves the query back
   out of `repositories.py` (PATTERNS §2).
2. **A service protects an invariant or it should not exist** (PATTERNS §11). One public function
   per module, `@transaction.atomic`, keyword-only args, `now` and `correlation_id` passed in, and
   at minimum one of: a typed domain error raised, an `ActivityRecord`, an `OutboxEvent`. A function
   that only forwards fields to `Model.objects.update()` is an ORM passthrough — delete the layer or
   find the missing rule.
3. **`domain/` is importable with Django uninstalled** (BACKEND §4 DIP). No `import django`, no
   `apps.*` import outside its own package, no `.objects`, no `timezone.now()`. mypy runs strict
   over `domain/` and `services/`: no bare `Any` without an adjacent reason comment, no `cast()`
   asserting something the surrounding code has not established.

Checkable before you commit: `uv run --project backend mypy apps` passes; the §7 grep table below
returns nothing; every new `services/` module exports exactly one public function; every new
`repositories.py` function has a return annotation matching what it actually returns.

## App layout

```
backend/apps/<context>/
  domain/          specifications.py, policies.py, value_objects.py, events.py, errors.py
  models.py        Django ORM only
  repositories.py  every queryset the context needs
  services/        use cases, transactional
  api/             routers.py, schemas.py
  admin.py
  consumers/       only in contexts that consume events
  migrations/
  fixtures/
```

What goes where:

- `domain/` — pure Python. Risk specifications (`IsBlocked`, `HasNoNextStep`), prioritization
  signal strategies, `PriorityPolicy` weight math, typed errors (`TransitionNotAllowed`,
  `ReasonRequired`), event topic constants. Takes plain values or Pydantic models, never model
  instances that it queries through. No `import django`, no `.objects`, no `timezone.now()` —
  pass `now` in. Value objects are `BaseModel` (frozen ones with
  `model_config = ConfigDict(frozen=True)`) because django-ninja already speaks that type system:
  a domain object reaches the API without a parallel schema restating its fields, and it is
  validated at construction rather than at the boundary.
- `models.py` — fields, `Meta`, `__str__`, `constraints`, `indexes`. A `@property` that reads
  already-loaded fields is fine (`Blocker.is_open`). A method that hits the database or decides
  business outcomes is not.
- `repositories.py` — module-level functions or a small class per aggregate. Every
  `select_related` / `prefetch_related` / filter lives here so the N+1 fix has one home.
- `services/` — one module per use case, one public function per use case. Opens the
  transaction, calls repositories, calls domain, writes `ActivityRecord` and `OutboxEvent`.
- `api/` — `schemas.py` (Ninja Pydantic in/out) and `routers.py` (parse, call service, return
  schema). No querysets, no `Project.objects`.
- `consumers/` — idempotent handlers keyed on `event.id` via `ProcessedEvent`.

## Dependency rules (§7), as greppable checks

| Rule | Check |
|---|---|
| `domain/` imports neither Django nor other apps | `rg -n "^\s*(from\|import) (django\|apps\.)" backend/apps/*/domain/` returns nothing |
| `api/` does not import `models` | `rg -n "import.*models" backend/apps/*/api/` returns nothing |
| `services/` does not import `api/` | `rg -n "from apps\..*\.api" backend/apps/*/services/` returns nothing |
| services never touch Redis | `rg -n "redis" backend/apps/*/services/` returns nothing |
| no cross-context model imports | a context imports another context's `models` nowhere; it goes through events or a published interface |

Run these in CI alongside `make lint`. If a rule is violated, move code — do not add an exception.

## Add a new use case, end to end

Example: "resolve a blocker on a project". Files touched, in this order.

1. `backend/apps/work/domain/errors.py` — add the typed error the use case can raise, e.g.
   `BlockerAlreadyResolved(DomainError)`. Nothing Django-specific.
2. `backend/apps/work/domain/events.py` — the topic constant `BLOCKER_RESOLVED = "blocker.resolved"`
   (already listed in §6; reuse, do not invent a new spelling).
3. `backend/apps/work/models.py` + migration — only if new fields are needed. One migration per logical
   change, descriptively named.
4. `backend/apps/work/repositories.py` — `get_open_blocker_for_update(blocker_id)` returning the row
   with `select_for_update()` and `select_related("project")`.
5. `backend/apps/work/services/resolve_blocker.py` — the transactional service (pattern below).
6. `backend/apps/work/api/schemas.py` — `ResolveBlockerIn(reason: str)`, `BlockerOut`.
7. `backend/apps/work/api/routers.py` — `POST /api/blockers/{blocker_id}/resolve`, calls the service,
   returns `BlockerOut`. Domain errors are mapped centrally, not caught here.
8. `backend/apps/activity` needs no change: the service writes `ActivityRecord` with verb
   `BLOCKER_RESOLVED` through the shared activity helper.
9. Consumers: `ProjectSnapshot` rebuild and risk re-evaluation already subscribe to every
   `project.*` / `blocker.*` topic, so a correctly published event needs no consumer edit. If
   the topic is genuinely new, register it in the consumer's topic set.
10. Tests: a pure test for the domain error condition (no DB), an integration test that the
    service writes exactly one `ActivityRecord` and one `OutboxEvent` in the same transaction,
    and an API test for the 409 on an already-resolved blocker.
11. `backend/apps/work/admin.py` — expose the new field if the operation must edit it.

## The transactional application-service pattern

Mutate the aggregate, write the `ActivityRecord`, write the `OutboxEvent` — one transaction,
in that order, no Redis anywhere.

```python
# backend/apps/work/services/resolve_blocker.py
from django.db import transaction
from django.utils import timezone

from apps.activity.services.record import write_activity
from apps.events.services.outbox import enqueue_event
from apps.work import repositories as repo
from apps.work.domain.errors import BlockerAlreadyResolved
from apps.work.domain.events import BLOCKER_RESOLVED


@transaction.atomic
def resolve_blocker(*, blocker_id: int, actor: str, reason: str, correlation_id: str) -> Blocker:
    blocker = repo.get_blocker_for_update(blocker_id)
    if blocker.resolved_at is not None:
        raise BlockerAlreadyResolved(blocker_id)

    blocker.resolved_at = timezone.now()
    blocker.save(update_fields=["resolved_at"])

    write_activity(
        entity_type="project",
        entity_id=blocker.project_id,
        verb="BLOCKER_RESOLVED",
        actor=actor,
        from_value="open",
        to_value="resolved",
        reason=reason,
        metadata={"blocker_id": blocker.id, "kind": blocker.kind},
        correlation_id=correlation_id,
    )
    enqueue_event(
        topic=BLOCKER_RESOLVED,
        entity={"type": "project", "id": blocker.project.code},
        payload={"blocker_id": blocker.id, "reason": reason},
        actor=actor,
        correlation_id=correlation_id,
    )
    return blocker
```

Notes that matter:

- `@transaction.atomic` on the service function, not on the view and not inside the repository.
  Either all three writes land or none do.
- `enqueue_event` only inserts an `OutboxEvent` row. The relay (`make relay`) is the single
  process that talks to Redis.
- `correlation_id` is generated at the API boundary (or forwarded from the incoming event in a
  consumer) and threaded through, so "deprioritize A to prioritize B" reads as one decision.
- The router stays this thin:

```python
@router.post("/blockers/{blocker_id}/resolve", response=BlockerOut)
def resolve(request, blocker_id: int, body: ResolveBlockerIn):
    return resolve_blocker(
        blocker_id=blocker_id,
        actor=request.actor,
        reason=body.reason,
        correlation_id=request.correlation_id,
    )
```

## When a repository is NOT needed

A repository exists to keep queries out of services, views and consumers — not to hide the ORM.

Skip it when:

- The service needs a single `get_object_or_404`-style lookup by primary key with no joins and
  no filtering policy. Call the manager directly from the service and move on.
- The read is a one-off in the admin or a management command.
- The read side already has `ProjectSnapshot` (§8): the read API queries that table, so a second
  abstraction over it buys nothing.

Add it as soon as: the same filter appears twice, the query needs `select_related` to avoid an
N+1 in the command center, it needs `select_for_update`, or the "what counts as an open blocker"
rule is encoded in a `filter()`. That last one is a business rule that must live in exactly one
place.

We do not define abstract base classes or `Protocol` interfaces for repositories. Services import
`apps.<context>.repositories` as a module; tests use a real database with `factory_boy`. Pure
logic that deserves DB-free testing lives in `domain/`, and that is where the isolation comes
from — not from a mock repository.

## Common mistakes

- **Logic in the view.** A router that checks `if project.workflow_state.category == "BLOCKED"`
  before calling the service. The check belongs to the transition service; the router only
  translates HTTP.
- **Queries in the service instead of the repository.** `Project.objects.filter(...).select_related(...)`
  inline in `services/` — it duplicates across use cases and the N+1 fix then has three homes.
- **Importing models from `api/`.** Usually starts as "just for the type hint" in `schemas.py`.
  Use `ninja.Schema` with plain fields; if you need the shape of a model, describe it explicitly.
- **`domain/` importing Django.** `from django.utils import timezone` inside a specification, or a
  strategy that receives a `Project` model and calls `project.tasks.filter(...)`. Pass an already
  materialized Pydantic model / plain values in; the domain must run under `pytest` with no
  database.
- **Django signals as an event bus.** `post_save` on `Project` that publishes or recalculates.
  Signals fire outside the use case's intent, run inside someone else's transaction, and are
  invisible in the outbox. Every event is written explicitly by the service.
- **Publishing to Redis from a service.** Any `redis` import under `services/` is a bug (CLAUDE.md
  rule 4). Write `OutboxEvent`; the relay publishes.
- **Writing the `ActivityRecord` after commit**, in a second transaction or in a consumer. It must
  be in the same atomic block as the mutation, otherwise the audit trail can lie.
- **Assigning `workflow_state` directly** in a service or admin action instead of going through
  the transition service that validates `WorkflowTransition`, guards and `requires_fields`.
- **A consumer that assumes exactly-once.** Every handler starts by claiming `event.id` in
  `ProcessedEvent` and returns early if it was already processed.
- **Comparing against labels.** `if state.label == "Bloqueado"`. Compare `state.category` or
  `code`; labels are Spanish data edited from the admin.
- **A repository that returns a lazy queryset behind a `list` annotation.**
  `def open_blockers(...) -> list[Blocker]: return Blocker.objects.filter(...)`. The type checker is
  satisfied and the query still executes in the service, the router or the template. Wrap in
  `list(...)` (BACKEND §1), or annotate `QuerySet[Blocker]` and accept that the caller can extend
  it — which is itself banned by PATTERNS §2.
- **A service that is an ORM passthrough.** `def update_project(code, **fields)` calling
  `.update(**fields)`. No validation, no `ActivityRecord`, no outbox row: the layer enforces nothing
  and the audit trail loses the change (PATTERNS §11).
- **`timezone.now()` or `datetime.now()` inside a service body.** `now` is a keyword parameter, so
  the use case is deterministic under test and under replay (PATTERNS §3). The same rule kills
  `date.today()` in a value object.
- **Missing annotations in the layer you just created.** `ANN` is on: every argument and every
  return, including `-> None`, in `domain/`, `repositories.py`, `services/`, `api/`, `consumers/`
  and test helpers. A bare `Any` needs an adjacent comment naming the reason (JSONB payload,
  stub gap, `**kwargs`).
- **A `Protocol` or ABC for a repository with one implementation.** Services import
  `apps.<context>.repositories` as a module. The interface is the method set; extract an abstraction
  when the second implementation exists, not before (PATTERNS §11).
- **A docstring that paraphrases the signature.** Public service functions, repository functions,
  specifications, signal strategies and consumer handlers need Google-style docstrings stating the
  invariant, the failure mode and the `Raises:` list — not "Transitions a project. Args: project_code:
  The project code." (BACKEND §2).
