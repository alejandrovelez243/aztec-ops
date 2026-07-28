---
name: event-driven-flow
description: Load when adding, renaming or versioning an event topic, writing an OutboxEvent from a service, implementing or debugging a Redis Streams consumer (priority-recalculator, risk-evaluator, sse-fanout), touching the outbox relay or the DLQ, or when an event is emitted but never reaches the browser.
---

# Adding an event end to end

The bus is: service writes `OutboxEvent` in the same transaction as the state change → relay
publishes to the Redis stream `aztec.events` → consumer groups handle it → `sse-fanout`
`PUBLISH aztec.sse` → `GET /api/stream` → Astro island. See §6 of `docs/ARCHITECTURE.md`.

## Why the outbox exists

Without it a service does `save()` then `redis.xadd()`. The concrete failure: **the
transaction commits and the publish never happens** — the process is killed, Redis is
restarting, the network drops between the two calls. The state change is durable, the event is
gone forever, and the priority score, the risk flags and the read model `ProjectSnapshot` stay
stale with no way to notice. The reverse also breaks: publish first, then the transaction rolls
back, and consumers react to a change that never existed.

The outbox removes the dual write. `OutboxEvent` is a row in the same PostgreSQL transaction as
the aggregate mutation and the `ActivityRecord`: either all three exist or none do. The relay is
a separate process that reads unpublished rows with `SELECT ... FOR UPDATE SKIP LOCKED` and
publishes them. If it crashes mid-publish the row is still unpublished and gets republished —
which is exactly why delivery is at-least-once and consumers must be idempotent.

## Standards that bind here

`docs/standards/BACKEND.md` and `docs/standards/PATTERNS_BACKEND.md` are normative for every file
touched in this flow: `domain/events.py`, `services/`, `consumers/`, the relay and the DLQ. Three
rules bite hardest here.

**Typed envelope, not loose dicts** (BACKEND §1, PATTERNS §1). The envelope and each topic payload
are frozen Pydantic models (`model_config = ConfigDict(frozen=True)`) in
`backend/apps/<context>/domain/events.py`, annotated end to end. The only `dict[str, Any]` allowed
is the `OutboxEvent.payload` JSONB column itself, and a consumer parses it into the topic's model
at the first line that reads it — no `envelope["payload"]["from"]` reaching into a handler body, no
`Any` without an adjacent comment naming the JSONB reason. `BaseModel` is the same type system
django-ninja already uses, so an envelope or payload crosses to the API without a parallel schema
restating its fields, and a malformed payload fails at construction rather than at the boundary.

**No bare `except`, nothing swallowed** (BACKEND §5, `BLE` in ruff). Services and queryset methods never
catch `Exception`. The consumer boundary is the one permitted catch-all, and only in the full shape:
log with `event_id` and consumer group, publish to `aztec.events.dlq`, `XACK` in `finally`.
`except IntegrityError` on the `ProcessedEvent` claim is the only silent path, and only after a
debug log plus the ack. `except: pass` and `except: return None` fail review anywhere in this flow.

**The docstring states the idempotency key** (BACKEND §2). Every consumer handler is a public
symbol, so the docstring is mandatory and must say what the signature cannot: the topic consumed,
the dedup key `(event_id, consumer_group)`, what the effect is, and what happens on the duplicate
path. A docstring that says "Handles the event" is a missing docstring. Payload models state
their `version` and what a bump means.

## Checklist

### 1. Name the topic and document it

`<entity>.<event>` or `<entity>.<aspect>.<event>`, lowercase, dot-separated, past tense.
Current topics (§6):

```
project.created            project.updated          project.state_changed
project.priority.recalculated                       project.risk.changed
task.created               task.state_changed
blocker.raised             blocker.resolved         note.added
```

Add the new topic to that list in §6 of `docs/ARCHITECTURE.md` in the same commit. An
undocumented topic is invisible to whoever writes the next consumer.

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
retyping a field means `version: 2` and a consumer that handles both until nothing emits 1.
`entity.id` is the business code (`PRJ-01`), not the database primary key — consumers in other
contexts must not need a FK into your models.

### 3. Emit from the service, inside the transaction

Only `backend/apps/<context>/services/` writes events, wrapped in `transaction.atomic()`, alongside the
`ActivityRecord`. A service that imports the Redis client is a bug (CLAUDE.md rule 4).

```python
# backend/apps/workflow/services/transition.py  (shape, not a literal copy)
with transaction.atomic():
    project.workflow_state = transition.to_state
    project.save(update_fields=["workflow_state"])
    activity_repo.record(verb="STATE_CHANGED", entity=project, from_value=..., to_value=...,
                         reason=reason, actor=actor, correlation_id=correlation_id)
    outbox_repo.append(topic="project.state_changed", entity=("project", project.code),
                       payload={"from": ..., "to": ..., "reason": reason},
                       actor=actor, correlation_id=correlation_id, version=1)
```

Reuse the `correlation_id` of the request so "deprioritize A to prioritize B" stays one movement
in the timeline.

### 4. Decide which consumer group handles it

One group per reason to react, never one group per topic — a failing group must not block the
others. Existing groups on `aztec.events`:

- `priority-recalculator` — recomputes `PriorityScore`, emits `project.priority.recalculated`.
- `risk-evaluator` — re-runs the risk specifications, emits `project.risk.changed`.
- `sse-fanout` — `PUBLISH aztec.sse` for topics the browser needs.

Plus the read-side rebuild of `ProjectSnapshot` (§8), which reacts to any event carrying
`entity.type == "project"`. If the new topic changes anything shown in the command center, it
must reach that rebuild or the UI shows stale joins.

### 5. Implement the idempotent handler

Handlers live in `backend/apps/<context>/consumers/`. Deduplicate on `event.id` with the
`ProcessedEvent` table, in the same transaction as the effect:

```python
with transaction.atomic():
    _, created = ProcessedEvent.objects.get_or_create(
        event_id=event.id, consumer_group="priority-recalculator"
    )
    if not created:
        return  # already applied; still XACK below
    recalculate(project_code=event.entity["id"])
```

The unique key is `(event_id, consumer_group)`: the same event is legitimately processed once
per group. `XACK` after the transaction commits, and `XACK` on the duplicate path too — an
unacked duplicate stays pending forever. Let unexpected exceptions propagate: the runner retries
with backoff and, after N failures, moves the entry to `aztec.events.dlq`, which is visible in
the admin.

### 6. Decide whether it reaches the browser

If the UI must react live, add the topic to the `sse-fanout` allowlist and to the store the
Astro islands subscribe to. There is one shared `EventSource`; components subscribe to the
store, never open their own connection (§9). If nothing in the UI changes, leave the topic out
of the fanout instead of publishing noise the client discards.

### 7. Add the delivery test

Integration tests required by §11, and on this path the base class is the whole point
(CLAUDE.md rule 15). **Everything on the event path is `TransactionTestCase`.** `TestCase` wraps
each test in a transaction that never commits: `transaction.on_commit` callbacks never fire, and
the relay's `SELECT ... FOR UPDATE SKIP LOCKED` runs on a second connection that cannot see rows
your test transaction has not committed. An outbox, relay or consumer test written on `TestCase`
passes while proving nothing — it is a false pass, not a slow test you optimised.

What to cover, one class per behaviour:

- service call → exactly one `OutboxEvent` row with the right topic and `version`; rollback of
  the transaction leaves zero rows.
- relay run → the entry appears on `aztec.events` with the envelope intact.
- handler invoked twice with the same `event.id` → the effect happens once and both calls ack.
- end to end: outbox → consumer → `ProjectSnapshot` updated / SSE frame emitted.

```python
# backend/apps/workflow/tests/test_outbox.py  (shape, not a literal copy)
from django.test import TransactionTestCase


class OutboxWriteTests(TransactionTestCase):
    """The service writes exactly one row, in the state change's transaction."""

    def setUp(self) -> None:
        self.execution = WorkflowStateFactory(code="execution")
        self.blocked = WorkflowStateFactory(code="blocked")

    def test_transition_writes_one_outbox_row(self) -> None:
        project = ProjectFactory(workflow_state=self.execution)

        transition_service.execute(code=project.code, to_state="blocked", actor="camila")

        events = list(OutboxEvent.objects.filter(entity_id=project.code))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].topic, "project.state_changed")
        self.assertEqual(events[0].version, 1)

    def test_rollback_leaves_no_outbox_row(self) -> None:
        project = ProjectFactory(workflow_state=self.execution)

        with self.assertRaises(IllegalTransition):
            transition_service.execute(code=project.code, to_state="closed", actor="camila")

        self.assertEqual(OutboxEvent.objects.count(), 0)


class RelayDeliveryTests(TransactionTestCase):
    """The relay claims committed rows on its own connection and publishes them intact."""

    def test_published_entry_carries_the_envelope(self) -> None:
        event = OutboxEventFactory(topic="project.state_changed", entity_id="PRJ-01")

        relay.drain_once()

        entries = redis.xrange("aztec.events", count=10)
        self.assertEqual(len(entries), 1)
        envelope = decode_envelope(entries[0][1])
        self.assertEqual(envelope.id, event.id)
        self.assertEqual(envelope.topic, "project.state_changed")
        self.assertEqual(envelope.entity["id"], "PRJ-01")
        event.refresh_from_db()
        self.assertIsNotNone(event.published_at)


class ConsumerIdempotencyTests(TransactionTestCase):
    """The same event.id applies its effect once per consumer group, and acks both times."""

    def test_duplicate_event_applies_the_effect_once(self) -> None:
        envelope = envelope_for("project.state_changed", entity_id="PRJ-01")

        for delivery in ("first", "second"):
            with self.subTest(delivery=delivery):
                handle(envelope)

        self.assertEqual(
            ProcessedEvent.objects.filter(
                event_id=envelope.id, consumer_group="priority-recalculator"
            ).count(),
            1,
        )
        self.assertEqual(PriorityScore.objects.filter(project__code="PRJ-01").count(), 1)
```

One caveat when writing these: `setUpTestData` is a `TestCase` optimisation and does not exist
on `TransactionTestCase`, which truncates the tables between tests. Build the shared rows with
factories in `setUp` here, and keep `setUpTestData` for the ordinary database tests around this
path — the API route that lists the DLQ, the `ProcessedEvent` queries — which are
plain `TestCase`. Anything purely about the envelope or the payload models (validation, version
bump, topic naming) is `SimpleTestCase`: no database, so the purity of `domain/events.py` is
enforced by the base class.

Assertions are the unittest methods, never bare `assert`. Do not reach for
`pytest.mark.django_db` — the base class already declares what database access the test gets.

## Debugging

```bash
# is the stream receiving anything?
docker compose exec redis redis-cli XLEN aztec.events
docker compose exec redis redis-cli XREVRANGE aztec.events + - COUNT 5

# groups, their lag and their consumers
docker compose exec redis redis-cli XINFO GROUPS aztec.events
docker compose exec redis redis-cli XPENDING aztec.events risk-evaluator - + 10

# events stuck in the dead letter stream
docker compose exec redis redis-cli XLEN aztec.events.dlq
docker compose exec redis redis-cli XRANGE aztec.events.dlq - + COUNT 10

# is it stuck before Redis? unpublished outbox rows
make relay   # run the relay in the foreground and watch it drain
```

Read the symptom this way: rows pile up in `OutboxEvent` unpublished → the relay is down.
`XLEN` grows but `XPENDING` grows with it → a consumer reads and never acks, or it crashes
before the ack. `XPENDING` flat and the DLQ growing → the handler raises deterministically on
that payload. Everything empty and the UI still stale → the service never wrote the outbox row.

## Common mistakes

- **Publishing from the service.** `redis` imported anywhere under `services/` is the bug; the
  service writes `OutboxEvent` only.
- **Non-idempotent consumers.** Incrementing a counter, appending an `ActivityRecord` or
  emitting a downstream event without checking `ProcessedEvent` first. At-least-once guarantees
  the duplicate will arrive.
- **Forgetting `XACK`** — especially on the duplicate path and after a caught exception. The
  entry stays pending, gets reclaimed, reprocessed, and the group's lag never drops.
- **Payload without `version`.** The first consumer change then has to guess the shape from the
  field set.
- **Undocumented topics** — not added to the list in §6, so nobody knows the event exists.
- **Swallowing handler exceptions** (`try/except: pass`, or acking on failure). The event never
  reaches the DLQ and the failure is invisible. Let it raise; the retry and DLQ path exist for
  this.
- Acking before the effect commits, so a crash between the two loses the work silently.
- Using the primary key in `entity.id` instead of the business code, coupling contexts.
- Adding a topic to `sse-fanout` without adding it to the frontend store — published, received,
  ignored.
- **Untyped envelope.** A payload passed around as `dict[str, Any]` past the layer that reads the
  JSONB column, or a handler indexing `envelope["payload"]["from"]` instead of a model field.
  The field names are then invisible to the next consumer and to mypy (BACKEND §1).
- **`except Exception` outside the consumer boundary** — in a service, a queryset method, the relay's
  claim query. And at the boundary, a catch-all that skips one of log, DLQ, `XACK`: each omission
  loses the failure, the evidence or the slot (BACKEND §5).
- **An outbox test written on `TestCase`.** It is a false pass. The test transaction never
  commits, so `on_commit` never fires and the relay's `FOR UPDATE SKIP LOCKED` on a second
  connection sees nothing; the assertions pass on rows that no other process could ever have
  read. Outbox writes, the relay, consumer idempotency and SSE fanout are `TransactionTestCase`
  (CLAUDE.md rule 15). Likewise `pytest.mark.django_db` or a module-level `def test_...` on this
  path — the base class is the declaration of what the test may touch.
- **Handler docstring that paraphrases the signature.** No topic named, no `(event_id,
  consumer_group)` key stated, no duplicate-path behaviour. `ruff D` passes on it and a reviewer
  still cannot tell whether the handler is idempotent (BACKEND §2).
