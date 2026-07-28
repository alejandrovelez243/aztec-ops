# 0002 — PostgreSQL as the only datastore of record

## Status

Accepted — 2026-07-28.

## Context

Three parts of the design push directly on the database, and they push in different directions.

1. **The outbox (ADR 0003) needs a real queue table.** The relay is a separate process, and more
   than one relay may run. Claiming unpublished rows without double-publishing requires
   `SELECT ... FOR UPDATE SKIP LOCKED` inside a transaction. Without row-level locking the relay
   is either single-process by convention — which nothing enforces — or it duplicates on every
   restart race.
2. **Half the interesting data is document-shaped.** `PriorityScore.breakdown` holds one entry
   per signal with its raw value, weight, contribution and human-readable reason, and the set of
   signals changes when a policy version changes. `ActivityRecord.metadata` and
   `OutboxEvent.payload` carry a different shape per topic. `PriorityPolicy.weights` is a
   signal-code-to-weight map that is edited from the admin. Modelling these as columns means a
   migration every time a signal is added, which contradicts the whole point of ADR 0005 and 0006.
3. **The read side is a wide denormalized table.** `ProjectSnapshot` (§8) is rebuilt by a
   consumer and read by every request to the command center. It needs indexes on score and on
   risk flags, and it needs to be rebuilt inside a transaction so a request never sees a half-
   written snapshot.

The append-only `ActivityRecord` also has to stay append-only under concurrent writers, and
`ProcessedEvent` deduplication (§6) relies on a unique constraint doing the work rather than a
read-then-write check that races.

## Decision

PostgreSQL 16 is the single datastore of record, running as the `postgres` service in
`docker-compose.yml`. Redis is present, but only as transport (ADR 0003) and never as a source
of truth — anything in Redis can be lost and rebuilt from PostgreSQL.

- `JSONB` for `PriorityScore.breakdown`, `PriorityPolicy.weights`, `ActivityRecord.metadata` and
  `OutboxEvent.payload`. Everything the system filters or joins on stays a real column.
- The outbox relay claims rows with `SELECT ... FOR UPDATE SKIP LOCKED`.
- Idempotency is enforced by a unique constraint on `ProcessedEvent`, not by an application-level
  check.
- Tests run against PostgreSQL, not a substitute engine.

## Consequences

Good:

- Multiple relay processes are safe by construction, so scaling the relay is a compose replica
  count rather than a redesign.
- Adding a prioritization signal changes the contents of a `JSONB` document, not the schema. Old
  `PriorityScore` rows keep the breakdown shape of their own `policy_version`, which is what
  makes a past ranking reproducible.
- One transaction spans the aggregate mutation, the `ActivityRecord` and the `OutboxEvent`. That
  is the entire correctness argument of ADR 0003 and it does not exist without a real
  transactional database.

Cost we accepted:

- Development requires Docker. There is no "just run `manage.py runserver` against a file"
  path, and a reviewer who wants to poke at the code has to bring up a container first. The
  RUNBOOK carries that cost as a documented bring-up sequence.
- `JSONB` is unvalidated storage. A `breakdown` written with a misspelled signal key is accepted
  by the database and only surfaces when the UI renders a missing reason. The schema-level
  guarantee is replaced by tests over the engine and by the rule that every weight key must
  resolve to a registered signal at policy load.
- Querying inside `JSONB` is easy to reach for and easy to regret. The rule is that any field the
  command center filters or sorts by gets promoted to a column on `ProjectSnapshot`; the `JSONB`
  is for display and audit.
- One more moving part to operate, back up and reset. `make reset` exists because of it.

## Alternatives considered

- **SQLite.** Rejected. It has no useful `SELECT FOR UPDATE SKIP LOCKED`, so the outbox relay
  cannot be safely concurrent; its writer lock makes a relay, a worker and the API contending on
  the same file a source of `database is locked` errors under exactly the load a live demo
  creates; and its JSON support is weaker for the indexing the read side wants. The convenience
  of a zero-service database does not survive the outbox requirement.
- **PostgreSQL plus a document store for the payloads.** Rejected. It splits the transaction
  boundary — the whole problem ADR 0003 exists to remove — in exchange for JSON features
  `JSONB` already provides at this data volume.
- **Redis as the read model.** Rejected. `ProjectSnapshot` would be faster to read, but a
  rebuild would no longer share a transaction with the event handling, and a Redis flush would
  silently empty the main view until a full recompute. Redis stays disposable on purpose.
