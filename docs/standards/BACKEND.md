# Backend code standards — Python / Django

> Read this before writing Python in `backend/`. It says how code is written; `docs/ARCHITECTURE.md`
> says what the system is, `docs/DATA_MODEL.md` says what the tables are, `docs/CONTRIBUTING.md`
> says how work moves. Where a rule here is enforced by a tool, §8 names the tool.

Layers per app, from `ARCHITECTURE` §7: `domain/` (pure), `models.py` (persistence *and* the
named queries, as `QuerySet`/`Manager` methods), `services/`, `api/`, `handlers.py`. Every rule
below is stated against those directories.

## 1. Typing

Every function is annotated, parameters and return type, including `-> None`. This holds in
`domain/`, `services/`, `api/`, `handlers.py` and test helpers. `models.py` is
annotated for methods — queryset methods included — and Django field assignments are typed by
`django-stubs`.

mypy runs in strict mode over `backend/apps/*/domain/` and `backend/apps/*/services/`, and in
non-strict mode elsewhere. `domain/` has no excuse: it imports no Django, so nothing in it is
untypeable.

### ORM typing that is honest

Annotate what the ORM actually returns. A queryset method that narrows returns its own queryset
type — `def open(self) -> "TaskQuerySet"` — which is what lets
`Task.objects.assigned_to(user).open().overdue(as_of=today)` type-check as one lazy query. A method
that promises a `list[Project]` must materialize it: returning a lazy queryset behind a `list`
annotation is a lie that shows up as a query executed inside a template.

```python
# WRONG — the annotation says list, the value is a lazy queryset, and get() can raise
def open_blockers(project_id: int) -> list[Blocker]:
    return Blocker.objects.filter(project_id=project_id, resolved_at__isnull=True)

# RIGHT
def open_blockers(project_id: int) -> list[Blocker]:
    return list(
        Blocker.objects.filter(project_id=project_id, resolved_at__isnull=True)
        .select_related("owner")
        .order_by("raised_at")
    )
```

Custom managers get their own type so callers keep the narrow API:

```python
class ProjectQuerySet(models.QuerySet["Project"]):
    def active(self) -> "ProjectQuerySet":
        return self.filter(is_archived=False)


class Project(models.Model):
    objects: ClassVar[models.Manager["Project"]] = ProjectQuerySet.as_manager()
```

`cast()` is acceptable when the stub is genuinely weaker than the runtime guarantee and the
guarantee is visible in the same function — for example after `select_related("owner")` plus an
`owner__isnull=False` filter, where the field is `Optional[User]` in the model but cannot be
`None` in that queryset. It is a lie when it asserts something the code has not established:

```python
# WRONG — nothing here guarantees the project has an owner
owner = cast(User, project.owner)

# RIGHT — the absence is a domain fact, so handle it
if project.owner is None:
    return SignalResult(score=0.0, reason="Project has no owner; load cannot be evaluated.")
```

### Pydantic models, not dicts, across a boundary

A `dict[str, Any]` crossing from a query into a signal strategy erases every field name
the reader needs.

```python
# WRONG
def build_signal_input(project: Project) -> dict[str, Any]: ...

# RIGHT — backend/apps/prioritization/domain/value_objects.py
class SignalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_code: str
    target_date: date | None
    open_task_count: int
    overdue_task_count: int
    urgent_open_task_count: int
    open_blocker_count: int
    oldest_blocker_age_days: int | None
    business_value: Decimal | None
    next_step: str
    last_activity_at: datetime | None
    now: datetime
```

JSONB columns are the one place a mapping is the right type: `PriorityScore.breakdown`,
`OutboxEvent.payload`, `ActivityRecord.metadata`. Type them as `dict[str, Any]` at the model and
convert to a Pydantic model at the first layer that reads them.

Pydantic rather than a plain record type here because it is the same type system django-ninja
already uses: a domain object crosses to the API without a parallel schema restating its fields, and
it is validated at construction instead of at the boundary. The cost is real — `BaseModel` validates
every instance it builds, so in a tight loop it is measurably slower than a bare record — but at 22
projects and six signals that is irrelevant, and one type system end to end is worth more than the
microseconds.

Import cycles are broken with `TYPE_CHECKING`, never by moving an import inside a function:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.portfolio.models import Project
```

No bare `Any` without an adjacent comment giving the reason (third-party stub gap, JSONB payload,
`**kwargs` passthrough). `# type: ignore` carries the error code and the same kind of comment:
`# type: ignore[attr-defined]  # django-stubs does not model the reverse accessor here`.

## 2. Docstrings

Google style. Mandatory on every public module, class, service function, signal strategy,
specification, queryset/manager method and event handler. Not required on private helpers whose name
and signature already say everything, on `Meta` classes, on Django `__str__`, or on test classes and
test methods (the class name and the method name are the sentence).

A docstring earns its place by stating what the signature cannot: the invariant it upholds, the
failure mode, and the reason the code is shaped this way.

```python
# WRONG — restates the signature, tells the reader nothing
def transition_project(project_code: str, to_state_code: str, actor: str, reason: str) -> Project:
    """Transitions a project.

    Args:
        project_code: The project code.
        to_state_code: The target state code.
        actor: The actor.
        reason: The reason.

    Returns:
        The project.
    """
```

```python
# RIGHT
def transition_project(project_code: str, to_state_code: str, actor: str, reason: str) -> Project:
    """Move a project to another workflow state through a declared transition.

    The target state is reachable only if an active WorkflowTransition exists from the project's
    current state; the service never assigns workflow_state directly. Aggregate mutation,
    ActivityRecord and OutboxEvent are written in one transaction, so a caller either sees all
    three or none.

    Args:
        project_code: Business code, e.g. "PRJ-01".
        to_state_code: WorkflowState.code inside the project's bound workflow.
        actor: accounts.User.code, or "system" when the engine caused the change.
        reason: Free text. Required when the transition sets requires_reason.

    Returns:
        The project with its new state, refreshed from the row that was locked.

    Raises:
        TransitionNotAllowed: No active edge from the current state to to_state_code.
        ReasonRequired: The transition requires a reason and none was given.
        MissingRequiredFields: A field named in requires_fields is empty, e.g. next_step.
    """
```

Signal strategies and specifications document the fact they measure and the boundary values, since
those numbers are what a reviewer argues with: "Overdue returns 1.0; a null target_date returns 0.5
and raises NO_TARGET_DATE rather than being treated as distant."

## 3. Naming and Clean Code

Names say intent. No abbreviations (`proj`, `wf`, `prio`), no Hungarian prefixes (`str_code`,
`b_active`), no `data`/`info`/`obj` for a domain concept. No module named `utils.py`, `helpers.py`
or `managers.py` — a function that does not belong to `domain/`, a model's queryset or `services/`
usually means the layer is wrong, not that a dumping ground is missing.

A function does one thing at one level of abstraction. Guard clauses replace nesting:

```python
# WRONG
def resolve_blocker(blocker_id: int, reason: str, actor: str) -> Blocker:
    blocker = Blocker.objects.locked().filter(pk=blocker_id).first()
    if blocker is not None:
        if blocker.resolved_at is None:
            if reason:
                ...
            else:
                raise ResolutionReasonRequired(blocker_id)
        else:
            raise BlockerAlreadyResolved(blocker_id)
    else:
        raise BlockerNotFound(blocker_id)

# RIGHT
def resolve_blocker(blocker_id: int, reason: str, actor: str) -> Blocker:
    blocker = Blocker.objects.locked().filter(pk=blocker_id).first()
    if blocker is None:
        raise BlockerNotFound(blocker_id)
    if blocker.resolved_at is not None:
        raise BlockerAlreadyResolved(blocker_id)
    if not reason.strip():
        raise ResolutionReasonRequired(blocker_id)
    ...
```

No boolean parameter that selects behaviour. Two behaviours are two functions:

```python
# WRONG — the call site reads recompute_scores(project, True) and means nothing
def recompute_scores(project: Project, force: bool) -> PriorityScore: ...

# RIGHT
def recompute_score_if_input_changed(project: Project) -> PriorityScore | None: ...
def recompute_score(project: Project) -> PriorityScore: ...
```

Comments explain why. A comment restating the line below it is deleted, not reworded. Commented-out
code is deleted; git has it.

```python
# WRONG
# multiply by 100
value = base * 100

# RIGHT
# Log scale: a 28k contract does not deserve 3.5x the operational attention of an 8k one.
normalized = log1p(float(business_value)) / log1p(float(portfolio_max))
```

Magic numbers become named constants in `domain/`, or — when the operation must be able to change
them — a `PriorityPolicy` weight or a taxonomy column. `0.25` inside a scoring function is a policy
decision hidden from the admin.

```python
# WRONG
score = deadline * 0.25 + overdue * 0.20

# RIGHT — weights come from the active PriorityPolicy row, keyed by registered signal code
for code, strategy in registry.items():
    result = strategy.evaluate(signal_input)
    contribution = result.score * policy.weights[code] * 100
```

Thresholds that are structural rather than operational still get a name:
`STALE_AFTER_DAYS: Final[int] = 14`.

## 4. SOLID, against this codebase

### SRP — one use case per service

```python
# WRONG — the transition service also scores, and now a scoring bug fails a state change
@transaction.atomic
def transition_project(...) -> Project:
    ...
    project.workflow_state = to_state
    project.save(update_fields=["workflow_state", "updated_at"])
    score = PriorityEngine().recompute(project)      # not this service's job
    RiskEvaluator().evaluate(project)                # nor this
```

```python
# RIGHT — the service ends at the outbox row; recomputation is a handler reacting to the event
@transaction.atomic
def transition_project(...) -> Project:
    ...
    write_activity(entity_type="project", entity_id=project.code, verb="STATE_CHANGED", ...)
    enqueue_event(topic="project.state_changed", entity_id=project.code, payload={...})
    return project
```

`priority-recalculator` and `snapshot-builder` consume `project.state_changed`. A slow or failing
recalculation cannot roll back a legitimate state change, and the concerns are deployable and
testable apart.

### OCP — a new signal is a class plus a registry line

```python
# WRONG — every new signal edits this function and its tests
def score(data: SignalInput) -> float:
    total = 0.0
    if data.target_date is not None:
        total += deadline_pressure(data) * 0.25
    elif data.open_blocker_count:
        total += 0.15
    ...
```

```python
# RIGHT — backend/apps/prioritization/domain/signals/blockage.py
@register("blockage")
class Blockage(SignalStrategy):
    """Open blockers, weighted by the age of the oldest: old blockers need intervention."""

    def evaluate(self, data: SignalInput) -> SignalResult: ...
```

The evaluator iterates the registry and never learns a signal's name. Same shape for risk:
one `Specification` subclass carrying its own Spanish `label`, a `flag_code`, a severity, one
`@register_risk` line. If a change requires editing an existing `if` — or adding a code-to-label
dictionary on either side of the wire — the design is wrong (`CLAUDE.md` rule 8).

### LSP — every specification honours one contract

`Specification.is_satisfied_by(subject: ProjectRiskInput) -> bool` is pure, side-effect free and
total. That is what makes `IsBlocked() & ~HasNoTargetDate()` safe to write.

```python
# WRONG — this one queries the database, so composing it changes cost and breaks pure tests
class OwnerOverloaded(Specification):
    def is_satisfied_by(self, subject: ProjectRiskInput) -> bool:
        load = Task.objects.filter(assignee_id=subject.owner_id).count()   # side effect
        return load > subject.owner_capacity_points
```

```python
# RIGHT — the caller loads the facts; the specification only decides
class OwnerOverloaded(Specification):
    def is_satisfied_by(self, subject: ProjectRiskInput) -> bool:
        if subject.owner_code == "":
            return False
        return subject.owner_load_points > subject.owner_capacity_points
```

A subclass may not narrow the input (accepting only projects with a `target_date`), raise where
siblings return `False`, or need a database that the others do not.

### ISP — one schema per use case

```python
# WRONG — a god schema whose optional fields tell the client nothing about what it will get
class ProjectSchema(Schema):
    code: str
    name: str
    tasks: list[TaskSchema] | None = None
    breakdown: dict[str, Any] | None = None
    timeline: list[ActivitySchema] | None = None
    to_state: str | None = None
```

```python
# RIGHT — backend/apps/portfolio/api/schemas.py
class ProjectListItem(Schema):        # the queue row, read from ProjectSnapshot
    code: str
    name: str
    priority_score: Decimal
    health: str
    risk_flags: list[RiskFlagOut]

class ProjectDetail(ProjectListItem): # the detail page adds what only it renders
    breakdown: dict[str, Any]
    tasks: list[TaskListItem]
    available_transitions: list[TransitionOption]

class TransitionRequest(Schema):      # input only
    to_state: str
    reason: str = ""
```

### DIP — services depend on ports

```python
# WRONG — a service that imports Redis, and an ORM query written in the use case
import redis
from apps.portfolio.models import Project

def transition_project(...) -> Project:
    project = Project.objects.select_for_update().get(code=project_code)
    ...
    redis.Redis().publish("aztec.sse", envelope)      # or recalculate.delay(project_code)
```

```python
# RIGHT
from apps.events.outbox import enqueue_event        # the outbox port
from apps.portfolio.models import Project

def transition_project(*, project_code: str, ...) -> Project:
    project = Project.objects.locked().with_relations().by_code(project_code).get()
    ...
    enqueue_event(topic="project.state_changed", ...)
```

A service writes the outbox row and stops. It does not publish, and it does not call `.delay()`
either — naming a task is naming a consumer, and the drain plus the handler registry is what keeps
the producer ignorant of its reactors. The only handler allowed a Redis client is `sse-fanout`,
where publishing *is* the effect. `domain/` depends on nothing: no Django, no other app, no
`apps.*` import outside its own package.

## 5. Error handling

Typed domain exceptions live in `backend/apps/<context>/domain/errors.py`, all descending from
`DomainError`, each carrying the identifiers needed to render a message:

```python
class TransitionNotAllowed(DomainError):
    def __init__(self, project_code: str, from_state: str, to_state: str) -> None:
        super().__init__(f"No active transition {from_state} -> {to_state} for {project_code}.")
        self.project_code = project_code
        self.from_state = from_state
        self.to_state = to_state
```

They are mapped to HTTP once, in a central exception handler on the ninja API — never with a
per-route `try/except` returning an `HttpResponse`. `TransitionNotAllowed` → 409, `ReasonRequired`
and `MissingRequiredFields` → 422, `*NotFound` → 404.

```python
# WRONG — the mapping is now in two places and the next route will forget one
@router.post("/{code}/transition")
def transition(request, code: str, payload: TransitionRequest):
    try:
        return transition_project(code, payload.to_state, ...)
    except TransitionNotAllowed as exc:
        return 409, {"detail": str(exc)}
```

Never catch bare `Exception`, with exactly one exception: the **delivery boundary** in
`backend/apps/events/tasks.py`, where every handler failure is treated identically. It is already
written, and it never swallows — both branches end in a raise:

```python
try:
    applied = apply_once(registration, envelope)
except Exception as error:
    context = {"event_id": str(envelope.id), "topic": envelope.topic,
               "handler": handler_name, "attempt": attempt}
    if attempt >= settings.EVENT_MAX_ATTEMPTS:
        row.mark_dead_lettered(error, attempt=attempt)   # on the outbox row, not a second queue
        logger.exception("event dead lettered", extra=context)
        raise
    row.record_failure(error, attempt=attempt)
    logger.warning("event handler failed; retrying", extra=context, exc_info=True)
    raise self.retry(exc=error, countdown=_backoff_seconds(attempt)) from error
```

A handler itself never catches its own failure: raising is what produces the retry, the log line
and finally the dead letter. `try/except: pass` inside a handler is an event that vanished while
being marked applied.

No `except ...: pass`, no `except ...: return None` that hides the cause. `IntegrityError` on the
`ProcessedEvent` claim is the one silent-by-design path, and it is silent only after being logged at
debug and acked — that is deduplication, not swallowing.

Validation belongs to the schema (shape, types, required fields) or to the domain (invariants:
`requires_fields`, mandatory override reason, dependency acyclicity). A router that validates is a
router that will disagree with the service.

## 6. Functions and modules

Sizes are smells, not limits. A service function past ~40 lines, a class past ~150, a module past
~400, a branch depth past 3, or more than 5 parameters: stop and look. The usual cause of an
oversized service is that it grew a second use case (split it) or is doing query assembly that
belongs on the model's `QuerySet` (move it) or arithmetic that belongs in `domain/` (extract it as a
pure function and unit-test it without a database).

Module layout inside an app, from `ARCHITECTURE` §7:

```
backend/apps/<context>/
  domain/          errors.py, events.py, value_objects.py, policies.py, specifications.py
  models.py        fields, constraints, indexes, __str__, and the QuerySet/Manager that carry
                   every named query (select_related, select_for_update). No business rules.
  repositories.py  rare. Only a query spanning contexts, living in the context that consumes it.
  services/        one module per use case, one public function, @transaction.atomic
  api/             routers.py, schemas.py
  handlers.py      event reactors, one function per registered handler. The module name is
                   fixed — app-ready autodiscovers exactly `handlers`, so a reactor elsewhere is
                   never imported and silently never runs.
  tests/           domain/ (no database), integration/
```

One public function per service module, named after the use case: `services/transition_project.py`,
`services/resolve_blocker.py`. Private helpers below it, prefixed `_`.

## 7. Tests

Tests are Django `TestCase` classes, one class per behaviour under test. There are no loose
module-level test functions. The runner stays pytest via `pytest-django` (`make test`), which
collects Django `TestCase` classes natively — but the base class, not a decorator, declares what a
test may touch.

### The three base classes

| Base | For | What it gives you |
|---|---|---|
| `django.test.SimpleTestCase` | pure domain logic: priority signals, risk `Specification`s, value objects, policy maths | Forbids database access. The purity of `domain/` is enforced by the base class instead of by discipline. Coverage should be high here (`ARCHITECTURE` §11). |
| `django.test.TestCase` | queryset methods, services, API routes, workflow transitions, seed idempotency | Each test runs inside a transaction that is rolled back, so it is fast. |
| `django.test.TransactionTestCase` | the outbox, `transaction.on_commit`, the drain, event handlers, anything using a second database connection | Real commits and real truncation between tests. |

The third row is the one people get wrong, so state it plainly: `TestCase` wraps each test in a
transaction that **never commits**. `on_commit` callbacks therefore never fire — so the drain is
never kicked — and `SELECT ... FOR UPDATE SKIP LOCKED` cannot see a row that no connection has
committed. An outbox test written on `TestCase` passes while proving nothing. That is
the entire reason `TransactionTestCase` exists in this codebase; it is slower, and it is not
optional for those tests.

### Shape of a test class

Named after the thing and the situation: `DeadlinePressureSignalTests(SimpleTestCase)`,
`IllegalTransitionTests(TestCase)`, `OutboxDeliveryTests(TransactionTestCase)`. Methods keep the
`test_` prefix and are named after the behaviour, not after the method under test. Shared read-only
setup goes in `setUpTestData` — created once per class and rolled back, which is the Django-specific
optimisation and the reason to prefer it — and `setUp` is only for per-test mutable state.
factory_boy factories still supply data; they are called from `setUpTestData`. Assertions use the
unittest methods (`self.assertEqual`, `self.assertIn`, `self.assertRaises`, and Django's own
`self.assertNumQueries`), not bare `assert`. Table-driven cases use subtests rather than duplicated
methods.

Do not introduce pytest fixtures or `pytest.mark.django_db`. The base class already says what
database access a test gets, and two mechanisms for one decision is how a suite ends up with "pure"
tests that quietly hit the database.

Domain logic needs no database at all. Signals and specifications take a `SignalInput` /
`ProjectRiskInput` model, so a test constructs one and asserts — no fixtures, milliseconds per case:

```python
# WRONG — a module-level function, and django_db on logic that never touches a table:
# it opens a database connection for nothing and lets a signal start querying unnoticed.
# Two behaviours in one test, so the first failing assert hides the second.
@pytest.mark.django_db
def test_deadline_pressure():
    assert DeadlinePressure().evaluate(make_input(target_date=None)).score == 0.5
    assert DeadlinePressure().evaluate(make_input(target_date=YESTERDAY)).score == 1.0

# RIGHT — SimpleTestCase forbids the database, so the signal's purity is proved, not assumed
class DeadlinePressureSignalTests(SimpleTestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.signal = DeadlinePressure()

    def test_missing_target_date_scores_half_and_names_the_gap(self) -> None:
        result = self.signal.evaluate(signal_input(target_date=None))
        self.assertEqual(result.score, 0.5)
        self.assertIn("no target date", result.reason.lower())

    def test_overdue_target_date_saturates_the_signal(self) -> None:
        result = self.signal.evaluate(signal_input(target_date=date(2026, 7, 22), now=NOW))
        self.assertEqual(result.score, 1.0)
```

Subtests over duplicated methods when the same behaviour is checked across a range of inputs, so one
failure names the input that broke it:

```python
    def test_score_rises_as_the_target_date_approaches(self) -> None:
        for days, expected in ((30, 0.2), (14, 0.5), (3, 0.9)):
            with self.subTest(days=days):
                result = self.signal.evaluate(signal_input(target_date=NOW.date() + timedelta(days=days), now=NOW))
                self.assertEqual(result.score, expected)
```

Assert the `reason` string as well as the number: the reason is what the UI shows to justify a rank,
so an untested reason is an untested feature. Time enters as `data.now`; a test that calls
`datetime.now()` is a test that fails on a Tuesday.

The database is for what actually needs it: illegal transitions and seed idempotency on `TestCase`;
outbox delivery, drain claiming and handler idempotency on `TransactionTestCase`.

```python
class IllegalTransitionTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.project = ProjectFactory(workflow_state__code="BLOCKED")

    def test_transition_without_a_declared_edge_is_rejected(self) -> None:
        with self.assertRaises(TransitionNotAllowed):
            transition_project(self.project.code, "DONE", actor="system", reason="")
```

```python
class OutboxDeliveryTests(EagerCeleryMixin, TransactionTestCase):
    def test_the_drain_dispatches_a_committed_outbox_row(self) -> None:
        # On TestCase this would pass while dispatching nothing: the row is never committed,
        # so the drain's claim could not see it and on_commit would never fire.
        with only_handlers("snapshot-builder"):
            transition_project("PRJ-01", "IN_PROGRESS", actor="system", reason="")

        row = OutboxEvent.objects.get(entity_id="PRJ-01")
        self.assertIsNotNone(row.published_at)
        self.assertTrue(
            ProcessedEvent.objects.filter(event_id=row.id, handler="snapshot-builder").exists()
        )
```

## 8. The mechanical gate

`backend/pyproject.toml` selects, on top of the existing `E, W, F, I, UP, B, C4, DJ, SIM, TC, RUF`:

| Rule set | Enforces |
|---|---|
| `ANN` | Annotations on every argument and return. `ANN401` bans bare `Any`. |
| `D` with `convention = "google"` | Docstring presence and Google format on public symbols. |
| `N` | pep8-naming: no Hungarian prefixes, no `l`/`O` names, correct class and constant casing. |
| `FBT` | Boolean positional parameters — the flag-argument rule in §3. |
| `BLE` | `except Exception` / bare `except`, per §5. |
| `TRY` | `raise` inside `try`, long try bodies, exception messages built at the raise site. |
| `ARG` | Unused arguments, which are usually a stale signature. |
| `RET` | Redundant `else` after `return` — the guard-clause rule. |
| `C90` | `max-complexity = 10`: the branch-depth smell in §6. |
| `ERA` | Commented-out code. |
| `PTH`, `DTZ` | `pathlib` over `os.path`; timezone-aware datetimes only. |

Per-file ignores: `D`, `ANN` relaxed under `*/migrations/*`; `D101` and `D102` under `*/tests/*` (the
class and method names are the docstring); `FBT` allowed in `models.py` for `BooleanField` defaults.

mypy: `strict = true` globally, with `disallow_untyped_defs`, `disallow_any_explicit` and
`warn_return_any` kept on for `apps.*.domain.*` and `apps.*.services.*`, the `django-stubs` plugin
configured with `django_settings_module = "config.settings"`, and `ignore_errors = true` for
`apps.*.migrations.*` only.

```bash
uv run --project backend ruff check                # lint
uv run --project backend ruff format --check       # formatting
uv run --project backend mypy apps                 # types
make lint                                          # all three, in the container
make test
```

What the tooling actually catches: missing annotations, bare `Any`, missing or malformed
docstrings, naming, boolean flags, bare `except`, commented-out code, complexity, import order,
naive datetimes, and every `domain/`-`services/` type error.

What only review catches, and therefore what a reviewer is responsible for: whether a docstring
states the invariant instead of paraphrasing the signature; whether a `cast()` is justified by the
surrounding code; whether a service does one use case; whether a new signal was added as a class
plus a registry line rather than an `if`; whether a specification stayed pure; whether a schema is
per-use-case; whether a comment explains why; whether a magic number should have been a
`PriorityPolicy` weight; whether the test names describe behaviour; and whether a test picked the
right base class — in particular whether anything touching the outbox, `on_commit`, the drain or a
handler is on `TransactionTestCase`, since on `TestCase` it passes without proving anything. A pull request that passes
`make lint` has cleared the floor, not the bar.
