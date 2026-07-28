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

**Problem.** A state change lives in PostgreSQL, the event lives in Redis. Publishing after
`commit()` means a Redis outage, a container kill or a network blip between the two writes loses
the event permanently, and nothing in the database says so. Publishing before commit means an
event for a transaction that rolled back. Both are the dual-write failure.

**Where.** `backend/apps/events/models.py` (`OutboxEvent`), the write helper used by every service,
and the relay process (`make relay`). The relay is the only code in the repository that imports the
Redis client for publishing.

```python
# backend/apps/events/outbox.py
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

The relay claims with `SELECT ... WHERE published_at IS NULL ORDER BY occurred_at, id
FOR UPDATE SKIP LOCKED`, `XADD`s to `aztec.events`, then writes `published_at` and
`stream_entry_id`. `SKIP LOCKED` is what lets two relay processes run without coordination.

**Rule for extending.** A new topic is registered in `docs/EVENTS.md` and `ARCHITECTURE.md` §6
first, then emitted through `enqueue_event` inside the same `transaction.atomic()` as the mutation
and the `ActivityRecord`. Payload changes that remove or retype a field bump `version`.

**Smell.** `import redis` anywhere under `services/`. A `transaction.on_commit(publish)` callback —
that is the dual write with extra steps, since the process can die between commit and callback.
Rows accumulating with `published_at IS NULL` means the relay is down; zero such rows with a stale
UI means a service forgot to write the outbox at all.

---

## 2. Repository

**Problem.** The same query — "open blockers of this project", "tasks whose state category is not
`DONE` or `CANCELLED`" — encodes a business definition. Spread across views and services, those
definitions drift, and `open` starts meaning three different things.

**Where.** `backend/apps/<context>/repositories.py`. Queries live here, not in `api/`, not inline in
a service.

```python
# backend/apps/work/repositories.py
class BlockerRepository:
    def open_for_project(self, project_id: int) -> list[Blocker]:
        return list(
            Blocker.objects.filter(project_id=project_id, resolved_at__isnull=True)
            .select_related("owner", "task")
            .order_by("raised_at")
        )

    def get_for_update(self, blocker_id: int) -> Blocker:
        return Blocker.objects.select_for_update().get(pk=blocker_id)
```

**Rule for extending.** Add a method when the same filter appears twice, when the filter encodes a
domain rule, or when the service needs `select_for_update`. Return model instances or plain data —
never a `QuerySet` the caller can extend, because a lazily-extended queryset moves the query back
out of this file.

**When it is not worth it.** A primary-key or `code` lookup with no joins and no domain predicate:
`Project.objects.get(code=code)` in a service is fine and wrapping it adds a file for nothing.
Do not create an abstract base class or a `Protocol` for a repository that has one implementation —
see §11 on premature abstraction. The interface here is the method set, not an ABC.

**Smell.** `.filter(...)` inside `api/routers.py`. A repository method named `get_queryset`. A
repository that imports another context's models: cross-context reads go through the published
aggregate (`Project`) or through events, never into `work`'s internals from outside.

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
    blocker = blocker_repository.get_for_update(blocker_id)
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

**Why it never publishes to Redis.** The database transaction is the only thing that can be rolled
back. A `XADD` inside the transaction is not rolled back with it, so a later `IntegrityError` would
leave a published event describing a change that never happened. Writing a row instead makes the
event as atomic as the change; delivery becomes the relay's problem, and the relay retries.

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
carrying the rebalanced weights, then `make recompute`. The `code` is frozen once a `PriorityScore`
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
    edge = transition_repository.active_edge(
        from_state=project.workflow_state, to_state_code=to_state_code
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

## 7. Publish/Subscribe with consumer groups

**Problem.** One committed fact has several independent reactions — rescore, re-evaluate risk, push
to the browser. In one handler, a failing risk evaluation also stops the browser from updating.

**Where.** `backend/apps/<context>/consumers/`, reading `aztec.events` through Redis Streams
consumer groups. Three groups by reason to react: `priority-recalculator`, `risk-evaluator`,
`sse-fanout`, plus the `snapshot-rebuild` group in `backend/apps/portfolio/consumers/`.

```python
def handle(envelope: Envelope, group: str) -> None:
    try:
        with transaction.atomic():
            ProcessedEvent.objects.create(event_id=envelope.id, consumer_group=group)
            apply_effect(envelope)          # the effect and the claim, one transaction
    except IntegrityError:
        logger.info("duplicate event %s for %s", envelope.id, group)
    redis.xack(STREAM, group, envelope.stream_entry_id)   # ack on both paths
```

**Idempotency.** The unique constraint `(event_id, consumer_group)` *is* the deduplication — the
handler does not `SELECT` first, because check-then-insert races between two consumers in the same
group. At-least-once means the same envelope will arrive twice; the second arrival must produce one
effect and two acks.

**The DLQ.** After N failed attempts with backoff, the envelope is pushed to the Redis stream
`aztec.events.dlq` and surfaces in the admin. It is a stream, not a table, because it holds
consumer-side failures; relay-side failures stay visible in `events_outboxevent` as rows with
`published_at IS NULL` and a rising `attempts`. An event dying silently is worse than a loud error.

**Rule for extending.** One group per reason to react, never one per topic. A new topic joins an
existing group unless it needs to fail independently. Only topics the UI actually renders go on the
`sse-fanout` allowlist.

**Smell.** Forgetting `XACK` on the duplicate path — the entry stays pending forever and the group
lag grows with no failing log line. A handler that is idempotent "because the effect is a no-op
anyway" — that argument stops holding the first time the effect appends a row.

---

## 8. CQRS-lite with a read model

**Problem.** The command center needs project + score + risk flags + owner load + task counts +
oldest blocker age, sorted by score. Through the ORM that is a six-table join with per-row
aggregates on every request.

**Where.** `portfolio_projectsnapshot`, one row per project, keyed by `project_code`. The read API
(`GET /api/v1/projects`) queries only this table, one index scan on
`(is_archived, priority_score DESC)`.

```python
# backend/apps/portfolio/consumers/snapshot_rebuild.py
def rebuild(project_code: str, last_event_id: UUID) -> None:
    ProjectSnapshot.objects.update_or_create(
        project_code=project_code,
        defaults=snapshot_projection(project_code) | {"last_event_id": last_event_id},
    )
```

**What rebuilds it.** The `snapshot-rebuild` consumer group, on any event whose `entity.type` is
`project`, plus `task.*` and `blocker.*` events resolved to their project. A `task.*` event also
recomputes `owner_load_points` on every snapshot owned by that person, not just the event's project.
`project_id` is a plain column, not a FK: the read side must survive the write side being rebuilt.

**The staleness window, and why it is acceptable.** Between commit and rebuild there are three hops
— relay claim, `XADD`, consumer read — so the snapshot trails the write side by the relay poll
interval plus consumer lag, normally well under a second. That is acceptable because every consumer
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

**Service locator.** A global `get_service("transition")` resolved at call time hides every
dependency from the reader and from mypy. Pass collaborators as arguments, or import the module
function directly. The two registries here (signals, risk specs) are exceptions with a fixed key
space, validated at policy load — not a general lookup for anything.

**A god `utils` module.** `apps/common/utils.py` becomes the place logic goes when nobody decided
which context owns it, and it grows imports in both directions until nothing can be tested alone.
Name the module for what it does (`outbox.py`, `specifications.py`) and put it in the owning app.

**Anemic services that are ORM passthroughs.** `def update_project(**fields): Project.objects.filter(...).update(**fields)`
adds a layer and enforces nothing — no validation, no `ActivityRecord`, no outbox row. If a service
has no invariant to protect, either the use case is wrong or the endpoint should not exist.

**Premature abstraction of a single implementation.** An ABC with one subclass, a `Protocol` with
one implementer, a `BaseSignalEvaluator` before the second evaluator exists. The registries and the
`Specification` protocol earn their abstraction because there are six of each and the seventh is
expected. One repository does not need an interface — extract it when the second implementation
arrives, not in anticipation of it.
