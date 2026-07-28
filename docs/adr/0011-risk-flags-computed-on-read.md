# 0011 — Risk flags are computed on read, not stored

## Status

Accepted — 2026-07-28. Narrows [0005](0005-deterministic-versioned-prioritization.md): the ranking
stays deterministic, versioned and persisted exactly as that ADR argued; the *risk* half of the same
engine stops being persisted. Removes a topic and a handler from
[0010](0010-celery-as-the-bus.md)'s bus.

## Context

The prioritization context evaluated six risk specifications — `BLOCKED`, `OVERDUE`,
`NO_NEXT_STEP`, `NO_TARGET_DATE`, `STALE`, `OWNER_OVERLOADED` — and wrote the satisfied ones into
`prioritization_riskflag`, one row per raised flag, cleared rather than deleted so the history was
reconstructible. A `risk-evaluator` handler reconciled that table on every event and on every clock
tick, emitted `project.risk.changed` when the set moved, and `snapshot-builder` copied the result
into `ProjectSnapshot.risk_flags` and `ProjectSnapshot.health`.

Three copies of one conclusion, and every one of them could be wrong.

Look at what the six conditions actually are. Overdue is `due_date < today`. Stale is
`last_activity < now - N days`. Blocked is "the state category is BLOCKED, or an open blocker row
exists, or a task sits in a blocked state". No-next-step, no-target-date and owner-overloaded are
the same shape: **pure functions of rows that already exist**, over a clock. Not one of them
required an observation that could not be repeated.

Storing a value like that buys exactly one thing — you can query it in SQL — and costs exactly one
thing: invalidation. And the invalidation here is not hypothetical. Two of the six change with no
event whatsoever. A project whose target date is today is not overdue; at midnight it is, and
nothing was written, nothing was delivered and no handler ran. The system's answer to that was to
put the clock on the bus so a tick could re-evaluate what the calendar had changed — which is to
say we built a scheduler, a topic, a handler, a consumer group and a retry policy so that a stored
copy of `due_date < today` could be brought back into agreement with `due_date` and `today`.

Meanwhile the stored copy was wrong for the entire window between midnight and the next tick, the
snapshot's copy was wrong until the rebuild after that, and a reader had no way to tell which of
the three they were looking at.

The score is not like this, and the difference is not performance. `PriorityScore` is stored because
**we need to know that it changed**: `ActivityRecord` records `PRIORITY_CHANGED` with a `from_value`
and a `to_value`, and without a stored previous value there is no before, so no movement, no audit
record, no event and no live reprioritization pushed to an open board. A stored score earns its
invalidation cost by making a comparison possible. A stored flag earned nothing: nothing in the
system ever needed to know that `OVERDUE` was raised *at a particular moment* rather than simply
being true now.

## Decision

**Risk flags and health are derived on every read. Nothing stores them.**

- `prioritization_riskflag` is dropped, with its model, its queryset, its admin and its migration
  state. The `Specification` classes in `apps/prioritization/domain/` are **unchanged** — they were
  already pure and already tested under `SimpleTestCase`; only their persistence goes.
- `evaluate_risk_for_project`, the `risk-evaluator` handler and the `project.risk.changed` topic are
  removed. A derived value computed on read has no previous set to compare against, therefore no
  moment of change, therefore no event. The flags travel in **every project payload** instead:
  `GET /api/v1/queue` and `GET /api/v1/projects/{code}` both carry `risk_flags` and `health`,
  evaluated at the instant of the request.
- `ProjectSnapshot` keeps what is genuinely expensive — the score copy, the aggregate task and
  blocker counts, the owner load, the last-activity instant — and loses `risk_flags` and `health`,
  which are derived from those same columns. It gains `in_progress_task_count`, the one fact
  `HasNoNextStep` needed that no column carried, from the aggregate that already produced the other
  four counts.
- Evaluation takes its inputs from what the read already has. `ProjectSnapshot.to_risk_input()`
  builds a `ProjectRiskInput` from the row in hand, so a page of 22 projects is one query and 22
  in-memory evaluations — never a query per project inside a loop.
- The two derived facets, `?health=` and `?risk_flag=`, are applied in Python after evaluation. They
  cannot be `WHERE` clauses any more, and expressing them in SQL would mean writing the six
  specifications a second time as predicates — two implementations of "blocked" that agree until the
  day they do not.

## Consequences

Good:

- **A computed flag cannot be stale.** The class of bug where the board says a project is fine and
  the date says otherwise is not fixed, it is unrepresentable.
- Three copies of one conclusion became zero. A table, a JSONB column, an indexed enum column, a
  handler, a topic, a payload schema and their tests are gone; the six specifications that were
  always the real definition remain, untouched.
- The clock tick has one job again — re-scoring what `valid_until` says is due — instead of also
  compensating for a derived column that the calendar had invalidated.
- A freshly seeded portfolio is correctly flagged before anything has run, and so is a project
  created thirty seconds ago whose events are still in the outbox.
- Adding a specification is still one class and one registry line (`CLAUDE.md` rule 8), and now it
  needs no migration, no backfill and no reconciliation of existing rows.

Cost we accepted, written down rather than hidden:

- **The board no longer pushes "this project just became at risk" on its own.** There is no
  `project.risk.changed` frame. In practice most transitions still reach the browser, because
  anything that changes a flag by a human action also emits an event — `project.state_changed`,
  `task.state_changed`, `blocker.raised`, `blocker.resolved` — and the project is re-read with its
  freshly evaluated flags. What is genuinely lost is the calendar case: a project that goes overdue
  or stale at midnight pushes nothing. **It is correct the moment anyone loads it**, and the clock
  tick still re-scores what its `valid_until` says is due, so the ranking movement that usually
  accompanies it does still arrive.
- **Risk history is gone.** `RiskFlag.detected_at` could answer "blocked for 19 days"; nothing can
  now. That was a real capability and no screen used it. If it comes back it should come back as an
  `ActivityRecord` verb — an append-only fact about something that happened — and not as a mutable
  table that is also the source of truth for the present.
- **The two derived facets cost a full scan of the filtered match.** `?risk_flag=OVERDUE` now
  materialises every row matching the other facets, evaluates it, and pages in Python. At 22
  projects that is nothing; it is the first thing that will need attention if this portfolio becomes
  a thousand, and the fix at that point is a materialized view refreshed by the tick, not a return
  to a hand-maintained table.
- **`?risk_flag=` no longer uses an index.** The GIN index on `ProjectSnapshot.risk_flags` is
  dropped with the column.
- Reading a project detail costs a few more queries than it did, because the facts the
  specifications need are now collected on the read path rather than looked up as a flag set. One
  project, a handful of indexed lookups, and the answer is right.

## Alternatives considered

- **Keep the table and make the tick re-evaluate more often.** Rejected. It shrinks the staleness
  window without closing it, and it makes the correctness of a derived value proportional to how
  often a scheduler runs — which is a load-bearing dependency on a cron for something that is a
  one-line function of two columns.
- **Keep the stored flags purely as a denormalization, and treat the computed value as
  authoritative.** Rejected: that is the same three copies, with a convention saying which to
  believe. Conventions are not enforced by anything, and the wrong copy is the one that is indexed
  and therefore the one a query returns.
- **Store only the calendar-independent flags (`BLOCKED`, `NO_NEXT_STEP`, `NO_TARGET_DATE`) and
  compute the two time-derived ones.** Rejected. It splits one concept across two mechanisms, so
  every reader has to know which flags are trustworthy and which are computed, and adding a
  specification would require deciding which half it belongs to — exactly the `if` that
  `CLAUDE.md` rule 8 exists to prevent.
- **Compute the flags but keep `health` as a column so the queue can filter and sort on it.**
  Rejected for the same reason, one level up: health is a function of the flags, so a stored health
  is a stored flag set with fewer digits. The facet moved to Python instead.
- **Drop `PriorityScore` too, for symmetry.** Rejected, and the asymmetry is the point of this
  record. The score is stored because a *change* in it is a fact the system must publish and audit;
  the previous value is the only thing that makes the change observable. Flags never had that
  requirement.
