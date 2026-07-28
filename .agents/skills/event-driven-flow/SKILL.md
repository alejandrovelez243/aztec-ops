---
name: event-driven-flow
description: Load when adding, renaming or versioning an event topic, writing an OutboxEvent from a service, implementing or debugging a registered Celery event handler (priority-recalculator, snapshot-builder, sse-fanout), touching the outbox drain or a dead-lettered event, or when an event is emitted but never reaches the browser.
---

# Adding an event end to end

The bus is: service writes `OutboxEvent` in the same transaction as the state change →
`events.drain_outbox` (a Celery task) claims the row and queues one `events.handle_event` per
**registered handler** subscribed to the topic → `sse-fanout` does `PUBLISH aztec.sse` →
`GET /api/stream` → Astro island. See §6 of `docs/ARCHITECTURE.md`, §5 and §6 of `docs/EVENTS.md`,
and [ADR 0010](../../../docs/adr/0010-celery-as-the-bus.md).

There is **no relay process, no Redis stream, no consumer group and no DLQ stream.** Redis is the
Celery broker and the `aztec.sse` pub/sub channel, nothing else. If you are about to write
`XADD`, `XREADGROUP`, `XACK` or `XPENDING`, you are working from a stale mental model.

## Why the outbox exists — and why it survived the transport change

Without it a service does `save()` then publishes. The concrete failure: **the transaction commits
and the publish never happens** — the process is killed, the broker is restarting, the network drops
between the two calls. The state change is durable, the event is gone forever, and the priority
score, the risk flags and the read model `ProjectSnapshot` stay stale with no way to notice. The
reverse also breaks: publish first, then the transaction rolls back, and handlers react to a change
that never existed.

The outbox removes the dual write. `OutboxEvent` is a row in the same PostgreSQL transaction as the
aggregate mutation and the `ActivityRecord`: either all three exist or none do. That guarantee is a
property of the *table*, not of the transport, which is why swapping Redis Streams for Celery
changed nothing about it.

`enqueue_event` does call `transaction.on_commit(drain_outbox.delay)`, and that is **not** a dual
write: the kick is an optimization, and a broker failure there is caught and logged. Celery Beat
sweeps the same table every `EVENT_DRAIN_INTERVAL_SECONDS`, so the worst case is latency. **Never**
move the `OutboxEvent` write itself into `on_commit` — that is the dual write with extra steps.

Delivery is at-least-once: the drain can die after queuing tasks and before committing
`published_at`. Handlers must be idempotent.

## Standards that bind here

`docs/standards/BACKEND.md` and `docs/standards/PATTERNS_BACKEND.md` (§1 outbox, §7 pub/sub) are
normative for every file touched in this flow: `domain/events.py`, `services/`, `handlers.py`, and
`apps/events/tasks.py`. Three rules bite hardest.

**Typed envelope, not loose dicts** (BACKEND §1, PATTERNS §1). The envelope and each topic payload
are frozen Pydantic models (`model_config = ConfigDict(frozen=True)`) in
`backend/apps/<context>/domain/events.py`, annotated end to end. The only `dict[str, Any]` allowed
is the `OutboxEvent.payload` JSONB column itself, and a handler parses it into the topic's model at
the first line that reads it — no `envelope.payload["from"]` reaching into a handler body, no `Any`
without an adjacent comment naming the JSONB reason. `BaseModel` is the same type system
django-ninja already uses, so an envelope or payload crosses to the API without a parallel schema
restating its fields, and a malformed payload fails at construction rather than at the boundary.

**No bare `except`, nothing swallowed** (BACKEND §5, `BLE` in ruff). Services, handlers and queryset
methods never catch `Exception`. The **delivery boundary in `apps/events/tasks.py` is the one
permitted catch-all**, and it is already written: log with `event_id`, `topic`, `handler` and
`attempt`, retry with backoff, dead-letter past the budget, and re-raise on both branches. A handler
that catches its own failure to "keep things moving" marks the event applied and loses it forever.
`except IntegrityError` on the `ProcessedEvent` claim is the only silent path, and it lives in
`_claim`, not in your handler.

**The docstring states the idempotency key and the failure mode** (BACKEND §2). Every handler is a
public symbol, so the docstring is mandatory and must say what the signature cannot: what it
reacts to, what the effect is, what it emits, and — in `Raises:` — what a failure means, because
raising is the retry path. A docstring that says "Handles the event" is a missing docstring. Payload
models state their `version` and what a bump means.

## Checklist

### 1. Name the topic and document it

`<entity>.<event>` or `<entity>.<aspect>.<event>`, lowercase, dot-separated, past tense.
Current topics:

```
project.created            project.updated          project.state_changed
project.priority.recalculated
task.created               task.updated             task.state_changed
blocker.raised             blocker.resolved         note.added
clock.ticked
```

Add the full entry to `docs/EVENTS.md` §4 and the line to `docs/ARCHITECTURE.md` §6 in the same
commit, then register the `TOPIC_*` constant in `backend/apps/events/domain/envelope.py`. That
catalog is not documentation-only: `register_handler` validates every subscription against it at
import, so an unregistered topic is a boot failure rather than a handler that silently never fires.

### 2. Define the versioned payload

The envelope is fixed; only `payload` changes per topic.

```json
{
  "id": "uuid",
  "topic": "project.state_changed",
  "occurred_at": "2026-07-28T10:00:00Z",
  "actor": "camila",
  "correlation_id": "uuid",
  "entity": {"type": "project", "id": "PRJ-01"},
  "payload": {"from": "execution", "to": "blocked", "reason": "..."},
  "version": 1
}
```

Declare the payload as a typed Pydantic model in `backend/apps/<context>/domain/events.py` (pure, no
Django import). `version` starts at 1. Adding an optional field keeps the version; removing or
retyping a field means `version: 2` and a handler that handles both until nothing emits 1.
`entity.id` is the business code (`PRJ-01`), not the database primary key — handlers in other
contexts must not need a FK into your models. For a non-project entity, put `project_code` in the
payload so `snapshot-builder` can aggregate without one.

### 3. Emit from the service, inside the transaction

Only `backend/apps/<context>/services/` writes events, wrapped in `transaction.atomic()`, alongside
the `ActivityRecord`. A service that imports the Redis client is a bug, and so is a service that
calls `.delay()` — naming a task is naming a consumer (CLAUDE.md rules 4 and 8).

```python
# backend/apps/workflow/services/transition.py  (shape, not a literal copy)
with transaction.atomic():
    project.workflow_state = transition.to_state
    project.save(update_fields=["workflow_state"])
    write_activity(ActivityCommand(verb="STATE_CHANGED", ...))
    enqueue_event(
        topic=TOPIC_PROJECT_STATE_CHANGED,
        entity_type=ENTITY_PROJECT, entity_id=project.code,
        payload=StateChangedPayload.of(...).model_dump(mode="json"),
        actor=actor, correlation_id=correlation_id, occurred_at=now, version=1,
    )
```

The service names a **topic** and stops. It does not know that `snapshot-builder` exists. Reuse the
`correlation_id` of the request so "deprioritize A to prioritize B" stays one movement in the
timeline.

### 4. Decide which handler reacts

One handler per **reason to react**, never one per topic — a failing handler must not block the
others, and each gets its own delivery task. Registered handlers:

| Handler | Declared in | Reacts to | Emits |
|---|---|---|---|
| `priority-recalculator` | `apps/prioritization/handlers.py` | the nine write-side topics + `clock.ticked` | `project.priority.recalculated` |
| `snapshot-builder` | `apps/portfolio/handlers.py` | `ALL_TOPICS - {clock.ticked}` | nothing |
| `sse-fanout` | `apps/events/handlers.py` | the SSE allowlist | nothing |

`snapshot-builder` subscribes by subtraction, so a new topic reaches the read model automatically —
which is the point: forgetting one produces a silently stale command center, not an error. The
ranking engine subscribes to the write side only and never to what it emits; that is what keeps the
graph acyclic. If your new topic must change the ranking, add its constant to `ENGINE_TOPICS`.

There is **no `risk-evaluator` and no `project.risk.changed`** (ADR 0011). Risk flags are computed
on read, so they have no moment of change to announce and no reactor to reconcile them. Do not add
a topic for a derived value: if it can be recomputed from rows that already exist, publish the fact
that moved those rows and let the reader derive it.

### 5. Implement the handler

A handler is a **function** in `backend/apps/<context>/handlers.py`. The module name is
load-bearing: app-ready calls `autodiscover_modules("handlers")`, so a reactor in `reactors.py` is
never imported and silently never runs — no error anywhere.

```python
# backend/apps/prioritization/handlers.py
from apps.events.domain.envelope import TOPIC_CLOCK_TICKED, EventEnvelope
from apps.events.domain.routing import project_code_of
from apps.events.registry import register_handler

PRIORITY_RECALCULATOR = "priority-recalculator"


@register_handler(name=PRIORITY_RECALCULATOR, topics=ENGINE_TOPICS)
def recalculate_priority(envelope: EventEnvelope) -> None:
    """Rescore the project this event names.

    Emits ``project.priority.recalculated`` only when the value or the breakdown moved, so a
    redelivery that changes nothing publishes nothing.

    Args:
        envelope: A delivered event on one of :data:`ENGINE_TOPICS`.

    Raises:
        ProjectNotFound: The event names a project that no longer exists. Deliberately not caught:
            raising is what produces the retry, the log line and finally the dead letter.
    """
    evaluate_risk_for_project(project_code=project_code_of(envelope), now=envelope.occurred_at)
```

What the handler may assume, and what it must not do:

- It runs **inside an open `transaction.atomic()` that already holds its `ProcessedEvent` claim**
  for `(envelope.id, name)`. Everything it writes commits with that claim or not at all, which is
  what makes a retry a real retry instead of a silent skip. Never open a second transaction, never
  call `commit`, never open a second connection.
- It is never called twice for the same `envelope.id` under the same name *after a success*. It can
  absolutely be called twice after a failure.
- `envelope.topic` is always in its declared `topics`.
- **Time comes from `envelope.occurred_at`** (or `payload.tick_at` for a tick), never
  `timezone.now()`. A redelivery must land on the same result.
- It must **raise** on failure. `try/except: pass` here is an event that vanished.

`sse-fanout` is the one handler permitted to hold a Redis client, because publishing *is* its
effect and there is no database write for it to be inconsistent with.

### 6. Decide whether it reaches the browser

If the UI must react live, add the topic to `SSE_ALLOWLIST_TOPICS` **and** to
`frontend/src/lib/stream/topics.ts`, and subscribe an island to it. There is one shared
`EventSource`; components subscribe to the store, never open their own connection (§9). If nothing
in the UI changes, leave the topic out of the fanout instead of publishing noise the client
discards.

### 7. Add the delivery test

Integration tests required by ARCHITECTURE §11, and on this path the base class is the whole point
(CLAUDE.md rule 15). **Everything on the event path is `TransactionTestCase`.** `TestCase` wraps
each test in a transaction that never commits: `transaction.on_commit` callbacks never fire, and the
drain's `SELECT ... FOR UPDATE SKIP LOCKED` runs on a connection that cannot see rows your test
transaction has not committed. An outbox, drain or handler test written on `TestCase` passes while
proving nothing — a false pass, not a slow test you optimised.

Two helpers exist and you should use both:

- `apps/events/tests/celery_support.py` — `EagerCeleryMixin`, so queued tasks run inline.
- `apps/events/tests/registry_support.py` — `only_handlers(...)` and `RecordingHandler`. **Narrow
  the registry in every handler test.** Committing an outbox row kicks the drain inline under eager
  Celery, and a full registry fans the event out to `sse-fanout`, which reaches Redis.

What to cover, one class per behaviour:

- service call → exactly one `OutboxEvent` row with the right topic and `version`; a rolled-back
  transaction leaves zero rows.
- drain → one `events.handle_event` per subscribed handler, and the row is marked `published_at`.
- handler applied twice with the same `event.id` → the effect happens once, the second call reports
  `duplicate`.
- past `EVENT_MAX_ATTEMPTS` → `dead_lettered_at` and `last_error` set on the row; the re-queue path
  is a real retry.
- end to end: outbox → handler → `ProjectSnapshot` updated / envelope on `aztec.sse`.

```python
# backend/apps/prioritization/tests/test_handlers.py  (shape, not a literal copy)
from django.test import TransactionTestCase

from apps.events.registry import get_handler
from apps.events.tasks import apply_once
from apps.events.tests.celery_support import EagerCeleryMixin
from apps.events.tests.registry_support import only_handlers


class RiskEvaluatorTests(EagerCeleryMixin, TransactionTestCase):
    """One effect per (event, handler), however many times it is delivered."""

    def setUp(self) -> None:
        self.project = ProjectFactory(code="PRJ-01")

    def test_duplicate_delivery_applies_the_effect_once(self) -> None:
        envelope = envelope_for("project.state_changed", entity_id="PRJ-01")

        with only_handlers("priority-recalculator"):
            first = apply_once(get_handler("priority-recalculator"), envelope)
            second = apply_once(get_handler("priority-recalculator"), envelope)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(
            ProcessedEvent.objects.filter(
                event_id=envelope.id, handler="priority-recalculator"
            ).count(),
            1,
        )
```

Going through `apply_once(get_handler(NAME), envelope)` rather than calling the function directly is
deliberate: it puts the registration name, the claim and the duplicate path under test too.

One caveat: `setUpTestData` is a `TestCase` optimisation and does not exist on
`TransactionTestCase`, which truncates tables between tests. Build shared rows with factories in
`setUp` here, and keep `setUpTestData` for the ordinary database tests around this path. Anything
purely about the envelope, the payload models or a *subscription shape* (which topics a handler
declares) is `SimpleTestCase` — the registry is pure Python, so no database is needed to assert
that no engine consumes what an engine emits.

Assertions are the unittest methods, never bare `assert`. Do not reach for
`pytest.mark.django_db` — the base class already declares what database access the test gets.

## Debugging

```bash
# the first command, always: is anything stuck or dead?
make outbox
curl -s localhost:8000/api/v1/health/pipeline

# the whole event path in one log stream
make logs-worker

# is the worker alive, and what is retrying?
docker compose exec api celery -A config inspect ping
docker compose exec api celery -A config inspect active
docker compose exec api celery -A config inspect scheduled

# which handlers are actually registered (catches a module not named handlers.py)
docker compose exec api python manage.py shell -c \
  "from apps.events.registry import registered_handlers; print(registered_handlers())"

# force one drain pass synchronously
docker compose exec api python manage.py shell -c \
  "from apps.events.tasks import drain_outbox; print(drain_outbox(batch_size=50))"

# is the fan-out publishing? (leave open, then trigger a change)
docker compose exec redis redis-cli SUBSCRIBE aztec.sse
```

Read the symptom this way. `pending` climbing in `make outbox` → the worker is down or cannot reach
the broker. `dispatched` climbing but nothing reacts → the event was dispatched to nobody: the
handler is not registered, or not subscribed to that topic. `dead_lettered > 0` → the handler raises
deterministically on that payload; read `last_error` on the row. Everything zero and the UI still
stale → the service never wrote the outbox row. Rows applied by every handler but the browser blank
→ SSE, not the bus (RUNBOOK §11).

`SELECT handler FROM events_processedevent WHERE event_id = '<uuid>'` is the replacement for
`XINFO CONSUMERS`: it says exactly which handlers applied a given event and which one is missing.

## Common mistakes

- **Publishing from the service, or calling `.delay()` from it.** Both are the bug: the first is a
  dual write, the second is a producer naming its consumer. The service writes `OutboxEvent` only.
- **A handler declared outside `handlers.py`.** It is never imported and never runs, with no error
  anywhere. This is the one failure mode of the registry design and it is silent.
- **Non-idempotent handlers.** Incrementing a counter, appending an `ActivityRecord` or emitting a
  downstream event without relying on the `ProcessedEvent` claim. At-least-once guarantees the
  duplicate will arrive.
- **Reading `timezone.now()` inside a handler.** A redelivery then produces a different number than
  the original, and nobody can reconstruct which one was right.
- **Opening a transaction inside a handler.** It breaks the enclosing claim and therefore the
  deduplication.
- **Swallowing handler exceptions** (`try/except: pass`). Nothing retries, nothing dead-letters, and
  the row is marked applied. Let it raise; the retry and dead-letter path exist for this.
- **Payload without `version`.** The first handler change then has to guess the shape from the
  field set.
- **Undocumented topics** — not in `docs/EVENTS.md` §4 and not in `envelope.py`, so
  `register_handler` refuses the subscription at import, or nobody knows the event exists.
- Using the primary key in `entity.id` instead of the business code, coupling contexts.
- Adding a topic to `sse-fanout` without adding it to the frontend store — published, received,
  ignored.
- **Untyped envelope.** A payload passed around as `dict[str, Any]` past the layer that reads the
  JSONB column, or a handler indexing `envelope.payload["from"]` instead of a model field. The field
  names are then invisible to the next handler and to mypy (BACKEND §1).
- **`except Exception` outside `apps/events/tasks.py`** — in a service, a handler, a queryset
  method. The delivery boundary already implements the full shape; a second one hides failures
  (BACKEND §5).
- **An outbox test written on `TestCase`.** It is a false pass: the test transaction never commits,
  so `on_commit` never fires and the drain sees nothing. Outbox writes, the drain, handler
  idempotency and SSE fanout are `TransactionTestCase` (CLAUDE.md rule 15). Likewise
  `pytest.mark.django_db` or a module-level `def test_...` on this path.
- **A handler test that does not call `only_handlers(...)`.** Under eager Celery the committed row
  kicks the drain inline and the full registry reaches Redis through `sse-fanout`.
- **Handler docstring that paraphrases the signature.** No topic named, no `Raises:` stating what a
  failure means. `ruff D` passes on it and a reviewer still cannot tell whether the handler is
  idempotent (BACKEND §2).
