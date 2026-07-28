---
name: django-clean-arch
description: How to lay out and write code inside an Aztec Ops Django app (domain/, models.py with its QuerySet/Manager, services/, api/, admin.py, consumers/). Load when adding or modifying a use case, an endpoint, a named query, a model or an event consumer in backend/apps/catalog, backend/apps/workflow, backend/apps/portfolio, backend/apps/work, backend/apps/activity, backend/apps/prioritization or backend/apps/events — or when reviewing whether the §7 dependency rules hold.
---

# Layered Django in Aztec Ops

Normative source: `docs/ARCHITECTURE.md` §6, §7, §8. This skill is the operational version of it.

## Standards that bite when you cut the layers

Binding: `docs/standards/BACKEND.md` and `docs/standards/PATTERNS_BACKEND.md`. Three rules decide
whether a layering change is correct or only looks correct.

1. **Named queries live on the model's `QuerySet`, exposed through its `Manager`** (PATTERNS §2).
   A query is a method on the queryset of the model that owns the rows, and it returns that
   queryset type, so the vocabulary composes and stays lazy:
   `Task.objects.assigned_to(user).open().overdue(as_of=today)` is one query with the predicates
   ANDed. A module-level `open_tasks_for(user) -> list[Task]` is materialised and cannot be
   narrowed, so every new combination needs a new function. Build the manager with
   `QuerySet.as_manager()`, or `Manager.from_queryset(...)` when the manager also needs behaviour
   that is not a filter over its own table. A method that must materialise (a count, an aggregate,
   a `dict`) is fine, but its docstring says so, because it ends the chain. The one exception is
   `repositories.py`: a query that *spans contexts* and so belongs to no single model, living in
   the context that consumes it.
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
queryset method returns its own queryset type, or says in its docstring why it materialises.

## App layout

```
backend/apps/<context>/
  domain/          specifications.py, policies.py, value_objects.py, events.py, errors.py
  models.py        Django ORM only — fields, constraints, and the QuerySet/Manager carrying
                   every named query the context needs
  repositories.py  rare. Only a query spanning contexts, in the context that consumes it
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
- `models.py` — fields, `Meta`, `__str__`, `constraints`, `indexes`, plus the `QuerySet` that
  names every query and the `Manager` exposing it. Every `select_related` / `prefetch_related` /
  filter lives there, so the N+1 fix has one home. A `@property` that reads already-loaded fields
  is fine (`Blocker.is_open`), as is a converter to a domain value object (`Project.to_summary()`).
  A method that decides business outcomes is not.
- `repositories.py` — only exists where a query spans contexts. Owner load aggregates `work.Task`
  keyed by user against `accounts.User.weekly_capacity_points`, so no single model's manager can
  carry it without one context learning about another; it lives in `portfolio`, which consumes the
  answer. There is exactly one such module. Anything else goes on the manager.
- `services/` — one module per use case, one public function per use case. Opens the
  transaction, calls the managers, calls domain, writes `ActivityRecord` and `OutboxEvent`.
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
4. `backend/apps/work/models.py` — the named query on `BlockerQuerySet`: `locked()`
   (`select_for_update`), `with_relations()` (`select_related("project", "task")`), `open()`. The
   service composes them: `Blocker.objects.locked().with_relations().open()`.
5. `backend/apps/work/services/resolve_blocker.py` — the transactional service (pattern below).
6. `backend/apps/work/api/schemas.py` — `ResolveBlockerIn(reason: str)`, `BlockerOut`.
7. `backend/apps/work/api/routers.py` — `POST /api/blockers/{blocker_id}/resolve`, calls the service,
   returns `BlockerOut`. Domain errors are mapped centrally, not caught here.
8. `backend/apps/activity` needs no change: the service writes `ActivityRecord` with verb
   `BLOCKER_RESOLVED` through the shared activity helper.
9. Consumers: `ProjectSnapshot` rebuild and risk re-evaluation already subscribe to every
   `project.*` / `blocker.*` topic, so a correctly published event needs no consumer edit. If
   the topic is genuinely new, register it in the consumer's topic set.
10. Tests: one `TestCase` class per behaviour, and the base class is chosen per layer being
    touched (CLAUDE.md rule 15).
    - `domain/errors.py`, `domain/events.py` → `SimpleTestCase`. It forbids database access, so
      the purity of `domain/` is proved by the test base, not by discipline.
    - queryset methods, `services/`, `api/routers.py` → `TestCase`. Each test runs inside a
      transaction that is rolled back, which is why it is fast.
    - the `OutboxEvent` actually reaching the relay, or anything relying on `on_commit` →
      `TransactionTestCase`. On `TestCase` the transaction never commits, so the assertion
      passes while proving nothing.

```python
# backend/apps/work/tests/test_resolve_blocker.py
from django.test import SimpleTestCase, TestCase

from apps.activity.models import ActivityRecord
from apps.events.models import OutboxEvent
from apps.work.domain.errors import BlockerAlreadyResolved
from apps.work.services.resolve_blocker import resolve_blocker
from apps.work.tests.factories import BlockerFactory


class BlockerAlreadyResolvedErrorTests(SimpleTestCase):
    """The typed error carries the blocker it refers to. No database involved."""

    def test_error_message_names_the_blocker(self) -> None:
        error = BlockerAlreadyResolved(42)
        self.assertIn("42", str(error))


class ResolveBlockerAuditTrailTests(TestCase):
    """Resolving writes exactly one ActivityRecord and one OutboxEvent, in one transaction."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.blocker = BlockerFactory(resolved_at=None)

    def test_resolving_writes_one_activity_record_and_one_outbox_event(self) -> None:
        resolve_blocker(
            blocker_id=self.blocker.id,
            actor="camila",
            reason="client granted access",
            correlation_id="c-1",
        )

        self.assertEqual(ActivityRecord.objects.filter(verb="BLOCKER_RESOLVED").count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(topic="blocker.resolved").count(), 1)

    def test_resolving_twice_raises_and_writes_nothing_the_second_time(self) -> None:
        resolve_blocker(
            blocker_id=self.blocker.id,
            actor="camila",
            reason="client granted access",
            correlation_id="c-1",
        )
        with self.assertRaises(BlockerAlreadyResolved):
            resolve_blocker(
                blocker_id=self.blocker.id,
                actor="camila",
                reason="again",
                correlation_id="c-2",
            )
        self.assertEqual(OutboxEvent.objects.filter(topic="blocker.resolved").count(), 1)


class ResolveBlockerApiTests(TestCase):
    """The route maps the domain error to 409 through the central handler."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.blocker = BlockerFactory(resolved_at="2026-01-01T00:00:00Z")

    def test_resolving_an_already_resolved_blocker_returns_409(self) -> None:
        response = self.client.post(
            f"/api/blockers/{self.blocker.id}/resolve",
            data={"reason": "again"},
            content_type="application/json",
            headers={"x-actor": "camila"},
        )
        self.assertEqual(response.status_code, 409)
```
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
from apps.work.domain.errors import BlockerAlreadyResolved
from apps.work.domain.events import BLOCKER_RESOLVED
from apps.work.models import Blocker


@transaction.atomic
def resolve_blocker(*, blocker_id: int, actor: str, reason: str, correlation_id: str) -> Blocker:
    blocker = Blocker.objects.locked().with_relations().get(pk=blocker_id)
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

- `@transaction.atomic` on the service function, not on the view and not inside a queryset
  method. Either all three writes land or none do.
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

## When a query does NOT need a name

A named queryset method exists to give a business definition exactly one home — not to wrap the
ORM. `services/` is allowed to call a manager directly, so a query with nothing to name stays
inline:

- A lookup by primary key or `code` with no joins and no filtering policy:
  `Project.objects.filter(code=code).first()` in a service is fine.
- A one-off read in the admin or a management command.
- A read the read side already answers: `ProjectSnapshot` (§8) *is* the projection, so a second
  abstraction over it buys nothing.

Give it a name as soon as: the same filter appears twice, the query needs `select_related` to
avoid an N+1 in the command center, it needs `select_for_update` (`locked()`), or the "what counts
as an open blocker" rule is encoded in a `filter()`. That last one is a business rule that must
live in exactly one place.

We do not define abstract base classes or `Protocol` interfaces over a manager. The persistence
engine is not being swapped, and service tests are `TestCase` classes on a real PostgreSQL, with
`factory_boy` called from `setUpTestData`. Pure logic that deserves DB-free testing lives in
`domain/` and is tested on `SimpleTestCase`, which refuses database access outright — that is
where the isolation comes from, not from a mock repository.

## Common mistakes

- **Logic in the view.** A router that checks `if project.workflow_state.category == "BLOCKED"`
  before calling the service. The check belongs to the transition service; the router only
  translates HTTP.
- **An unnamed business query inline in a service.** `Project.objects.filter(is_archived=False)
  .exclude(workflow_state__category__in=(...)).select_related(...)` written in `services/` — the
  definition duplicates across use cases and the N+1 fix then has three homes. Name it on the
  queryset; a bare `code` lookup needs no name.
- **Importing models from `api/`.** Usually starts as "just for the type hint" in `schemas.py`.
  Use `ninja.Schema` with plain fields; if you need the shape of a model, describe it explicitly.
- **`domain/` importing Django.** `from django.utils import timezone` inside a specification, or a
  strategy that receives a `Project` model and calls `project.tasks.filter(...)`. Pass an already
  materialized Pydantic model / plain values in; the domain must run under `SimpleTestCase`, which
  raises on any database access.
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
- **A queryset method whose annotation lies.** `def open_blockers(...) -> list[Blocker]:
  return Blocker.objects.filter(...)`. The type checker is satisfied and the query still executes
  in the service, the router or the template. A narrowing method returns its own queryset type; a
  method that genuinely materialises returns the concrete type *and* says so in its docstring,
  because it ends the chain (BACKEND §1, PATTERNS §2).
- **A service that is an ORM passthrough.** `def update_project(code, **fields)` calling
  `.update(**fields)`. No validation, no `ActivityRecord`, no outbox row: the layer enforces nothing
  and the audit trail loses the change (PATTERNS §11).
- **`timezone.now()` or `datetime.now()` inside a service body.** `now` is a keyword parameter, so
  the use case is deterministic under test and under replay (PATTERNS §3). The same rule kills
  `date.today()` in a value object.
- **Missing annotations in the layer you just created.** `ANN` is on: every argument and every
  return, including `-> None`, in `domain/`, `models.py`, `services/`, `api/`, `consumers/`
  and test helpers. A bare `Any` needs an adjacent comment naming the reason (JSONB payload,
  stub gap, `**kwargs`).
- **A `Protocol` or ABC over a manager with one implementation.** Services call the manager. The
  interface is the method set; extract an abstraction when the second implementation exists, not
  before (PATTERNS §11).
- **A free function that re-does what a manager already composes.** `open_tasks_for(user)`,
  `overdue_tasks_for(user)`, `urgent_open_tasks_for(user)` are one chain wearing three names.
  Add the missing queryset method and let the caller compose.
- **An outbox or `on_commit` test written on `TestCase`.** The wrapping transaction never commits,
  so the callback never fires and the relay's `SELECT ... FOR UPDATE SKIP LOCKED` on a second
  connection cannot see the row. The test is green and proves nothing. Use `TransactionTestCase`.
- **A domain test written on `TestCase`.** It hides the fact that the specification or the signal
  reached the database. `SimpleTestCase` fails the moment `domain/` touches the ORM, which is the
  point of putting the code there.
- **A loose `def test_...` at module level, or `pytest.mark.django_db`.** Two mechanisms deciding
  the same thing. The base class already states what database access the test gets (CLAUDE.md
  rule 15); `pytest` only runs the classes.
- **A docstring that paraphrases the signature.** Public service functions, queryset methods,
  specifications, signal strategies and consumer handlers need Google-style docstrings stating the
  invariant, the failure mode and the `Raises:` list — not "Transitions a project. Args: project_code:
  The project code." (BACKEND §2).
