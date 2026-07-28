# Backend design patterns

> The patterns this backend actually uses, and nothing else. Normative source for *why*:
> `docs/ARCHITECTURE.md`. Schema: `docs/DATA_MODEL.md`. Workflow of a change: `docs/CONTRIBUTING.md`.
> Each section states the problem it solves here, where it lives, a sketch, the rule for extending
> it, and the smell that means it is being misused.

Vocabulary is fixed by the other docs: `WorkflowState.category`, `OutboxEvent`, `ProcessedEvent`,
`ProjectSnapshot`, `PriorityScore.breakdown`, `RiskFlag`, `SignalInput` / `SignalResult`.
Do not invent synonyms for any of them.

---

## 1. Transactional Outbox

**Problem.** A state change lives in PostgreSQL, the delivery lives in a broker. Publishing after
`commit()` means a broker outage, a container kill or a network blip between the two writes loses
the event permanently, and nothing in the database says so. Publishing before commit means an
event for a transaction that rolled back. Both are the dual-write failure. This is independent of
the transport, which is why the outbox survived Redis Streams being replaced by Celery
([ADR 0010](../adr/0010-celery-as-the-bus.md)).

**Where.** `backend/apps/events/models.py` (`OutboxEvent`) and
`backend/apps/events/services/enqueue_event.py`, the write helper used by every service. The
transport is `backend/apps/events/tasks.py`; no service imports it.

```python
# backend/apps/events/services/enqueue_event.py
def enqueue_event(
    *, topic: str, entity_type: str, entity_id: str, payload: dict[str, Any],
    actor: str, correlation_id: UUID, occurred_at: datetime, version: int = 1,
) -> UUID:
    """Insert the envelope. Caller is already inside transaction.atomic()."""
    event = OutboxEvent.objects.create(
        id=uuid4(), topic=topic, entity_type=entity_type, entity_id=entity_id,
        payload=payload, actor=actor, correlation_id=correlation_id,
        occurred_at=occurred_at, version=version,
    )
    return event.id
```

`enqueue_event` also does `transaction.on_commit(drain_outbox.delay)` — and that is **not** the
dual write, because the kick is an optimization, not the guarantee. A broker outage there is caught
and logged; the Beat sweeper picks the row up on the next pass. The rule is: the *event* is written
in the transaction, the *notification* may fail.

The drain task claims with `SELECT ... WHERE published_at IS NULL ORDER BY occurred_at, id
FOR UPDATE SKIP LOCKED`, queues one `events.handle_event` per subscribed handler, then writes
`published_at`. `SKIP LOCKED` is what lets two workers drain without coordination.

**Rule for extending.** A new topic is registered in `docs/EVENTS.md` and `ARCHITECTURE.md` §6
first, then emitted through `enqueue_event` inside the same `transaction.atomic()` as the mutation
and the `ActivityRecord`. Payload changes that remove or retype a field bump `version`.

**Smell.** `import redis` or `.delay()` anywhere under `services/` — a service that names a task
names a consumer, which is pattern 4's failure in a different costume. A publish written *instead
of* the outbox row inside `on_commit`; that is the dual write with extra steps, since the process
can die between commit and callback. Rows accumulating with `published_at IS NULL` means the worker
is down; zero such rows with a stale UI means a service forgot to write the outbox at all.

---

## 2. Named query on a QuerySet, exposed through a Manager

**Problem.** The same query — "open blockers of this project", "tasks whose state category is not
`DONE` or `CANCELLED`" — encodes a business definition. Spread across views and services, those
definitions drift, and `open` starts meaning three different things.

**Where.** `backend/apps/<context>/models.py`, on the queryset of the model that owns the rows.
Queries live there, not in `api/`, not inline in a service.

```python
# backend/apps/work/models.py
CLOSED_CATEGORIES: Final = (StateCategory.DONE, StateCategory.CANCELLED)


class TaskQuerySet(models.QuerySet["Task"]):
    def assigned_to(self, user: User | str) -> "TaskQuerySet":
        """Narrow to the tasks carried by one person, named by row or by ``User.code``."""
        ...

    def open(self) -> "TaskQuerySet":
        """Tasks still live: their state's category is neither ``DONE`` nor ``CANCELLED``."""
        return self.exclude(workflow_state__category__in=CLOSED_CATEGORIES)

    def overdue(self, as_of: date) -> "TaskQuerySet":
        """Tasks whose due date has already passed at ``as_of``. Undated is never overdue."""
        return self.filter(due_date__lt=as_of)


class Task(models.Model):
    objects = TaskQuerySet.as_manager()
```

**Why a queryset and not a module-level function.** Because it composes:

```python
Task.objects.assigned_to(user).open().overdue(as_of=today)
```

collapses into **one** lazy query whose predicates are ANDed in a single `WHERE`. The free-function
form, `open_tasks_for(user) -> list[Task]`, is materialised and therefore a dead end: every new
combination of person, state, date and priority needs another function, and the definition of
"open" is re-typed each time. That is the same combinatorial fragmentation that keeps
row-to-value-object converters off free functions and on the model (§9). Django already ships the
right tool.

**Manager or `as_manager()`.** Use `TaskQuerySet.as_manager()` when nothing but queryset methods
are needed. Use `Manager.from_queryset(TaskQuerySet)` when the manager also needs behaviour that is
not a filter over its own table — `Workflow.objects.resolve(...)` reads `WorkflowBinding` first, so
it is a manager method, not a queryset method.

**Rule for extending.** Add a method when the same filter appears twice, when the filter encodes a
domain rule, or when the service needs `select_for_update` (`locked()`). Every method returns the
queryset type, so chaining type-checks under `mypy --strict`. A method that must materialise — a
count, an aggregate, a `dict` — is legitimate, but its docstring says so, because it ends the
chain. Docstrings state what the query means in domain terms, not what the ORM call does.

**The one exception: `repositories.py`.** A query that **spans contexts** belongs to no single
model, and only that query keeps a module. Owner load aggregates `work.Task` keyed by assignee
against `accounts.User.weekly_capacity_points`: on `accounts` it would teach identity that
`work.Task` exists, on `work` it would put a portfolio question inside the context that merely owns
the rows. So it is a module-level function in `apps/portfolio/repositories.py` — the context that
*consumes* the answer, never the one that owns the rows. There is exactly one such module.

**What we do not do.** No abstract base class or `Protocol` over a manager with one implementation
(see §11 on premature abstraction) — the persistence engine is not being swapped and the domain is
tested pure with `SimpleTestCase`, so the usual justifications for a repository layer are benefits
this project never collects.

**Smell.** `.filter(...)` inside `api/routers.py`. A queryset method that does not return a
queryset without saying why in its docstring. A queryset method on one context's model that
imports another context's models: cross-context reads go through the published aggregate
(`Project`) or through events, never into `work`'s internals from outside.

---

## 3. Application Service / Command Handler

**Problem.** A use case is a transaction with four effects that must all happen or none:
the aggregate changes, the audit trail records it, the event is queued, the caller gets a result.
Split across a view and a model method, one of the four gets forgotten.

**Where.** `backend/apps/<context>/services/<use_case>.py` — one module, one public function.

Anatomy, in this exact order:

```python
# backend/apps/work/services/resolve_blocker.py
@transaction.atomic
def resolve_blocker(
    *, blocker_id: int, actor: str, reason: str, correlation_id: UUID, now: datetime,
) -> Blocker:
    # 1. validate — typed domain errors, raised, never caught here
    blocker = Blocker.objects.locked().with_relations().get(pk=blocker_id)
    if blocker.resolved_at is not None:
        raise BlockerAlreadyResolved(blocker.pk)
    if not reason.strip():
        raise ResolutionReasonRequired(blocker.pk)

    # 2. mutate the aggregate
    blocker.resolved_at = now
    blocker.resolution_reason = reason
    blocker.save(update_fields=["resolved_at", "resolution_reason"])

    # 3. record the activity
    write_activity(
        entity_type="blocker", entity_id=blocker.project.code, verb="BLOCKER_RESOLVED",
        origin="MANUAL", actor=actor, from_value="open", to_value="resolved",
        reason=reason, metadata={"blocker_id": blocker.pk, "kind": blocker.kind},
        occurred_at=now, correlation_id=correlation_id,
    )

    # 4. write the outbox row — same transaction, no Redis
    enqueue_event(
        topic="blocker.resolved", entity_type="blocker", entity_id=blocker.project.code,
        payload={"blocker_id": blocker.pk, "kind": blocker.kind, "reason": reason},
        actor=actor, correlation_id=correlation_id, occurred_at=now,
    )

    # 5. return the aggregate; the router maps it to a schema
    return blocker
```

**Why it never publishes, and never calls `.delay()`.** The database transaction is the only thing
that can be rolled back. A `PUBLISH` or a task enqueued inside the transaction is not rolled back
with it, so a later `IntegrityError` would leave a delivered event describing a change that never
happened. Writing a row instead makes the event as atomic as the change; delivery becomes the
drain's problem, and it retries. The second reason is pattern 7: naming a task is naming a
consumer, and a producer that names its consumers has to be edited to add a reaction.

**Rule for extending.** Time is a parameter (`now`), never `timezone.now()` inside the body — that
is what makes the service testable and a replay deterministic. `correlation_id` is threaded from
the API boundary so "deprioritize A to prioritize B" reconstructs as one movement. State changes
are the one thing a service does not do itself: they go through the transition service (§6).

**Smell.** A service returning an HTTP response or a `dict` shaped for JSON. A `try/except` around
a domain error — errors are mapped to status codes once, centrally. A service function taking
`request`. Two public functions in one service module.

---

## 4. Strategy + Registry

**Problem.** Six prioritization signals, each with its own arithmetic and its own sentence of
justification, that must be added or reweighted without editing the evaluator.

**Where.** `backend/apps/prioritization/domain/signals/`. Pure — no Django import, no database, no
`datetime.now()`.

```python
# backend/apps/prioritization/domain/signals/blockage.py
@register("blockage")
class BlockageSignal:
    def evaluate(self, data: SignalInput) -> SignalResult:
        if not data.open_blockers:
            return SignalResult(0.0, "No open blockers.")
        oldest_days = max((data.now.date() - b.raised_at.date()).days for b in data.open_blockers)
        score = min(1.0, 0.5 + oldest_days / 40)
        return SignalResult(
            score,
            f"{len(data.open_blockers)} open blocker(s); the oldest has been open for "
            f"{oldest_days} day(s) and needs intervention.",
        )
```

The evaluator iterates `PriorityPolicy.weights`, resolves each key through the registry, and
assembles `breakdown` (shape in `DATA_MODEL.md` §6.3). A weight key with no registered strategy —
or a registered strategy with no weight — raises at policy load rather than defaulting to zero.

**Rule for extending.** One file, one `@register("<code>")`, one new `PriorityPolicy` version
carrying the rebalanced weights, then recompute (the admin action or `POST /api/v1/recompute`). The `code` is frozen once a `PriorityScore`
has been written against it: it is the key inside every persisted `breakdown`. The evaluator is
never edited. Same shape for guards (`WorkflowTransition.guard` resolves a registered callable).

**Open/closed guarantee.** Adding a signal touches: a new file, a fixture row for the new policy
version. Nothing that already works is opened.

**Smell.** `if signal_code == "blockage":` anywhere. A signal reading the ORM instead of
`SignalInput`. A `reason` that restates the number ("score 0.9") instead of naming the fact that
produced it.

---

## 5. Specification

**Problem.** Risk conditions overlap and recombine: a project is blocked by an open `Blocker`, *or*
by its state category, *or* by a task's state category. Written as one nested `if`, adding a
seventh criterion means editing the sixth.

**Where.** `backend/apps/prioritization/domain/specifications.py`. Pure, database-free, and the
place where test coverage should be highest.

```python
class Specification(Protocol):
    def is_satisfied_by(self, project: ProjectView) -> bool: ...

class IsBlocked:
    flag_code = "BLOCKED"
    severity = "CRITICAL"

    def is_satisfied_by(self, project: ProjectView) -> bool:
        return (
            project.state_category == "BLOCKED"
            or bool(project.open_blockers)
            or any(t.state_category == "BLOCKED" for t in project.tasks)
        )

    def detail(self, project: ProjectView) -> str:
        return f"{len(project.open_blockers)} open blocker(s)."

# composition, not a bigger if
needs_attention = And(IsOverdue(), Not(IsBlocked()))
```

The six live specifications are `IsBlocked`, `IsOverdue`, `HasNoNextStep`, `HasNoTargetDate`,
`IsStale`, `OwnerOverloaded`. The evaluator runs the registry and returns the flags it satisfied;
`risk-evaluator` persists them as `RiskFlag` rows.

**Deriving health.** `health` is a function of the open flags, not a column anyone edits:
`BLOCKED` if a `CRITICAL` flag is raised, `AT_RISK` if any flag is raised, otherwise `HEALTHY`.
It is computed once and copied into `ProjectSnapshot.health` by the rebuild consumer. The source
spreadsheet's `health` is kept as `imported_health` and read by nothing at runtime.

**Rule for extending.** One class, a `flag_code`, a `severity`, one `@register_risk` line, one
table-driven database-free test asserting the `detail` string as well as the boolean.

**Smell.** A specification that queries the database. `severity` stored on the `RiskFlag` row as the
source of truth instead of coming from the registry entry. A `Project.health` field appearing in a
migration.

---

## 6. State machine as data

**Problem.** The operation must add a state (`en_espera_cliente`) without a deploy. A
`TextChoices` enum makes that a migration plus a hunt through every `if` that lists the blocked
codes.

**Where.** `backend/apps/workflow/` — `WorkflowState`, `WorkflowTransition`, `WorkflowBinding` as
rows; the transition service as the only writer of `Project.workflow_state` and
`Task.workflow_state`.

```python
# backend/apps/workflow/services/transition.py
@transaction.atomic
def transition_project(
    *, project: Project, to_state_code: str, actor: str, reason: str,
    correlation_id: UUID, now: datetime,
) -> Project:
    edge = (
        WorkflowTransition.objects.active()
        .from_state(project.workflow_state_id)
        .to_state_code(to_state_code)
        .first()
    )
    if edge is None:
        raise TransitionNotAllowed(project.workflow_state.code, to_state_code)
    if edge.requires_reason and not reason.strip():
        raise ReasonRequired(edge.pk)
    missing = [f for f in edge.requires_fields if not getattr(project, f, "")]
    if missing:
        raise MissingRequiredFields(missing)
    run_guard(edge.guard, project=project, now=now)
    ...
```

Guards are registered callables named by `WorkflowTransition.guard`; an empty string means no
guard. `requires_fields` is a JSONB list of aggregate field names (`["next_step"]`) — the API
renders the button disabled with the cause instead of letting the call fail.

**Why logic reads `category`, not `code`.** `code` is identity, open-ended, operator-created, unique
only inside its workflow — two workflows may both hold `bloqueada`. `category` is a closed set of
five values the operator picks but cannot extend. Branch on `category` and adding a state is a
fixture row; branch on `code` and it is a grep across the whole backend that will miss one place.

**Rule for extending.** A new state is data: the `WorkflowState` row, the transitions in and out of
it, a check of `WorkflowBinding`, and the same rows mirrored into
`backend/apps/workflow/fixtures/` with stable primary keys. Then an integration test that an
illegal transition into it raises `TransitionNotAllowed`.

**Smell.** `state.code == "bloqueada"`. Any assignment to `workflow_state` outside the transition
service. A second `WorkflowTransition` row for the same ordered pair instead of flipping
`is_active`.

---

## 7. Publish/Subscribe through a handler registry

**Problem.** One committed fact has several independent reactions — rescore, re-evaluate risk,
rebuild the read model, push to the browser. In one handler, a failing risk evaluation also stops
the browser from updating. And the producer must not learn who reacts, or adding a reaction becomes
an edit to the code that emitted the fact.

**Where.** `backend/apps/events/registry.py` holds the map. A reactor is a **function** in
`backend/apps/<context>/handlers.py` — the module name is load-bearing: app-ready calls
`autodiscover_modules("handlers")`, so a reactor in any other module is never imported and silently
never runs. Four are registered: `priority-recalculator` and `risk-evaluator`
(`apps/prioritization/handlers.py`), `snapshot-builder` (`apps/portfolio/handlers.py`),
`sse-fanout` (`apps/events/handlers.py`).

```python
# backend/apps/prioritization/handlers.py
@register_handler(name="risk-evaluator", topics=ENGINE_TOPICS)
def evaluate_risk(envelope: EventEnvelope) -> None:
    """Re-run the specifications for the project this event names.

    Raises on failure: that is how the retry, the log line and the dead letter happen.
    """
    evaluate_risk_for_project(project_code=project_code_of(envelope), now=envelope.occurred_at)
```

The transport calls it through `apply_once`, which is the whole idempotency mechanism:

```python
# backend/apps/events/tasks.py
with transaction.atomic():
    if not _claim(registration.name, envelope.id):   # INSERT ProcessedEvent(event_id, handler)
        return False                                  # duplicate: no-op, still a success
    registration.handle(envelope)                     # the effect and the claim, one transaction
```

**Idempotency.** The unique constraint `(event_id, handler)` *is* the deduplication — the transport
does not `SELECT` first, because check-then-insert races between two workers holding the same
delivery. At-least-once means the same envelope will arrive twice; the second arrival must produce
one effect and report `duplicate`. Because the claim shares the handler's transaction, a failed
attempt leaves no row, so the retry is a real retry and not a silent skip.

**Failure isolation.** One `events.handle_event` task per handler per event, so a handler that
raises retries on its own budget without touching the others. Retries are Celery's, with
exponential backoff and jitter, up to `EVENT_MAX_ATTEMPTS`.

**Dead-lettering.** Past the budget, `dead_lettered_at` and `last_error` are set **on the outbox row
itself**. There is no second queue and nothing is deleted: the failed event is still the row
carrying its topic, payload and correlation id, filtered in the admin and replayable with the
"Re-queue selected dead-lettered events" action. An event dying silently is worse than a loud error.

**Rule for extending.** One handler per reason to react, never one per topic. A new reaction is a
decorated function plus a row in `docs/EVENTS.md` §5 — **no producer edit, ever**. A new topic joins
an existing handler's `topics=` set unless it needs to fail independently. Only topics the UI
actually renders go on the `sse-fanout` allowlist. Handler names are released API: they are written
into every `ProcessedEvent` row, so renaming one replays history for it.

**Smell.** A service calling `.delay()`, or a producer that knows a handler name. A handler that
catches its own exception to "keep things moving" — that is an event that vanished, and it is marked
applied. A handler that opens its own `transaction.atomic()` or calls `commit` — it breaks the claim
and therefore the deduplication. A handler reading `timezone.now()` instead of
`envelope.occurred_at`, which makes a redelivery produce a different answer. A handler that is
idempotent "because the effect is a no-op anyway" — that argument stops holding the first time the
effect appends a row.

---

## 8. CQRS-lite with a read model

**Problem.** The command center needs project + score + risk flags + owner load + task counts +
oldest blocker age, sorted by score. Through the ORM that is a six-table join with per-row
aggregates on every request.

**Where.** `portfolio_projectsnapshot`, one row per project, keyed by `project_code`. The read API
(`GET /api/v1/projects`) queries only this table, one index scan on
`(is_archived, priority_score DESC)`.

```python
# backend/apps/portfolio/services/rebuild_snapshot.py, called by the snapshot-builder handler
def rebuild(project_code: str, last_event_id: UUID) -> None:
    ProjectSnapshot.objects.update_or_create(
        project_code=project_code,
        defaults=snapshot_projection(project_code) | {"last_event_id": last_event_id},
    )
```

**What rebuilds it.** The `snapshot-builder` handler, subscribed to every topic except
`clock.ticked` — derived by subtraction (`ALL_TOPICS - {clock.ticked}`) so a new topic is on the
subscription the day it is registered, because the failure mode of forgetting one is a silently
stale board. `task.*` and `blocker.*` events resolve to their project through `payload.project_code`. A `task.*` event also
recomputes `owner_load_points` on every snapshot owned by that person, not just the event's project.
`project_id` is a plain column, not a FK: the read side must survive the write side being rebuilt.

**The staleness window, and why it is acceptable.** Between commit and rebuild there are two hops
— the drain claim and the handler task — so the snapshot trails the write side by a broker round
trip, normally well under a second; if the on-commit kick was lost, by one Beat sweep instead. That is acceptable because every consumer
of this table is a human reading a dashboard that also receives the SSE patch, and because the
window is observable: `rebuilt_at` and `last_event_id` say exactly which delivery produced the row.
Decisions that must not be stale — the legal transitions for a project — are read from the write
side, never from the snapshot.

**Rule for extending.** A new displayed column is added to `ProjectSnapshot`, to the projection
function, and to the denormalization register in `DATA_MODEL.md` §11 with its writer named. A
denormalized field with no named writer goes stale silently.

**Smell.** A write path updating `ProjectSnapshot` directly instead of emitting the event that
rebuilds it. A read endpoint joining the snapshot back to `portfolio_project` — if the column is
missing, add it to the projection.

---

## 9. Value Object

**Problem.** A score is not a number: it is a number plus the six contributions and the sentence
that justifies each. Passed around as a bare float with the reasons rebuilt later, the explanation
and the value drift apart and the ranking stops being defensible.

**Where.** `backend/apps/prioritization/domain/value_objects.py` and equivalents per context.
Frozen Pydantic models, no identity, compared by value. Pydantic because it is the same type system
django-ninja already uses: a breakdown crosses to the API without a parallel schema restating its
fields, and it is validated at construction rather than at the boundary.

```python
from pydantic import BaseModel, ConfigDict


class SignalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float          # clamped to [0.0, 1.0] at construction
    reason: str           # names the fact: the days, the counts, the dates


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_version: str
    signals: tuple[SignalContribution, ...]
    modifiers: tuple[ModifierApplied, ...]

    @property
    def base(self) -> Decimal:
        return sum((s.contribution for s in self.signals), Decimal("0"))


class Countdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    days: int             # negative means overdue
    is_overdue: bool
```

**Rule for extending.** `model_config = ConfigDict(frozen=True)`, no `save()`, no database access,
constructed valid or not at all — clamping and the `contribution == round(raw * weight * 100, 2)`
invariant belong in the model, not in the caller. A change produces a new instance. Persistence is a separate step:
the value object is serialized into `PriorityScore.breakdown` and copied verbatim into
`ProjectSnapshot.breakdown`.

**Smell.** A mutable score object with a `recompute()` method. A `Countdown` that reads
`date.today()` — time arrives as `data.now`, always.

---

## 10. Factory for tests

**Problem.** A project worth testing needs a client, an engagement type, a workflow with an initial
state, an owner and a priority. Written by hand in each test, that setup is copy-pasted, and the
copies quietly diverge until a test passes on data the application could never produce.

**Where.** `backend/apps/<context>/tests/factories.py`, `factory_boy`.

```python
class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project

    code = Sequence(lambda n: f"PRJ-{n:02d}")
    client = SubFactory(ClientFactory)
    engagement_type = SubFactory(EngagementTypeFactory)
    workflow_state = SubFactory(WorkflowStateFactory, category="IN_PROGRESS")
    owner = SubFactory(UserFactory)
    target_date = None        # 5 of 22 source projects have none — the default is honest
    next_step = ""            # empty string, matching the column
```

**Keeping test data honest.** Defaults mirror what the seed actually contains: no completed task
exists in the source, `target_date` is legitimately null, `next_step` is `""` and never null. A
factory that defaults `target_date` to "next week" hides `NO_TARGET_DATE` and the test suite then
proves nothing about the real portfolio. Traits carry the deliberate variations
(`ProjectFactory(blocked=True)`), so the flag under test is the only thing the test states.

**Rule for extending.** Add a trait rather than a second factory class. A factory never sets
`workflow_state` on an existing project outside its own construction — that path belongs to the
transition service, and the integration test must exercise it.

**Smell.** A factory with `django_get_or_create` on a column the test then mutates. Fixtures loaded
into a pure domain test: signals and specifications are tested without a database.

---

## 11. Anti-patterns — banned here

**Fat models.** A `Project.resolve_blocker()` that mutates, audits and queues an event puts a
transaction boundary on a model that is also instantiated by the ORM in every query. Use cases live
in `services/`; `models.py` holds fields, constraints and indexes.

**Django signals as an event bus.** `post_save` fires inside the transaction, in the same process,
with no ordering, no retry, no dedup and no record that it ran. The bus is the outbox; a signal
handler doing domain work is invisible to `RUNBOOK.md` when it fails.

**A Celery task called from a service.** `recalculate.delay(project_code)` in a service body is the
same anti-pattern one layer down: it is a dual write (the enqueue is not rolled back with the
transaction) *and* a producer naming its consumer. Write the outbox row; the drain and the registry
decide who runs.

**Service locator.** A global `get_service("transition")` resolved at call time hides every
dependency from the reader and from mypy. Pass collaborators as arguments, or import the module
function directly. The three registries here (priority signals, risk specifications, event
handlers) are exceptions with a fixed key space, validated at policy load or at import — not a
general lookup for anything.

**A god `utils` module.** `apps/common/utils.py` becomes the place logic goes when nobody decided
which context owns it, and it grows imports in both directions until nothing can be tested alone.
Name the module for what it does (`outbox.py`, `specifications.py`) and put it in the owning app.

**Anemic services that are ORM passthroughs.** `def update_project(**fields): Project.objects.filter(...).update(**fields)`
adds a layer and enforces nothing — no validation, no `ActivityRecord`, no outbox row. If a service
has no invariant to protect, either the use case is wrong or the endpoint should not exist.

**Premature abstraction of a single implementation.** An ABC with one subclass, a `Protocol` with
one implementer, a `BaseSignalEvaluator` before the second evaluator exists. The registries and the
`Specification` protocol earn their abstraction because there are six of each and the seventh is
expected. One manager does not need an interface — extract it when the second implementation
arrives, not in anticipation of it.
