# 0012 — Soft delete for tasks, scoped at the application layer

## Status

Accepted — 2026-07-29. Extends the archival pattern
[0006](0006-workflows-and-taxonomies-as-data.md) already relies on for `portfolio.Project` to the
`work.Task` aggregate, and adds one topic to [0010](0010-celery-as-the-bus.md)'s bus.

## Context

An operator needs to remove a task. Tasks get created by mistake, get superseded by a
reorganisation of the work, or turn out to belong to a different project — and until now the only
way to make one disappear was the Django admin, which deletes the row.

A hard `DELETE` on `work_task` is not a small operation in this schema, and the cascade graph says
why:

- `work_note.task` is `CASCADE`. Deleting the task deletes the conversation about it, including
  the notes that explain why it was created in the first place.
- `work_blocker.task` is `CASCADE`. Deleting the task deletes the record of the impediment it hit,
  and with it the only evidence for why the project sat blocked for three weeks.
- `work_taskdependency.task` is `CASCADE` and `.depends_on` is `SET_NULL`. Every other task that
  was waiting on this one silently loses its prerequisite pointer and keeps only the prose label.
- `ActivityRecord` is append-only and holds no foreign key at all. It addresses the task by its
  business code, so the trail survives the delete and points at nothing — as does the
  already-published `task.created` envelope, which a consumer may replay months later.

So the row is the anchor of four kinds of history, and three of them are wired to disappear with
it. The requirement, meanwhile, is not "forget this ever existed"; it is "get it off my board".

`portfolio.Project` already answers exactly that question with `is_archived`, and the product is
built around it: `ProjectQuerySet.active()`, `ProjectSnapshotQuerySet.in_attention()`, the
`is_archived` facet on `GET /queue`, and `ProjectQuerySet.next_code`'s explicit refusal to reuse an
archived project's code.

The remaining question was not *whether* to soft delete, but **where the scope is applied**: on the
default manager, so every query is filtered unless it opts out — or on each read, so every query
states which set it wants.

## Decision

**`Task.is_archived` is a soft delete, and the scope is applied per read, never on the default
manager.**

- `Task.is_archived: BooleanField(default=False)`, named after `Project.is_archived` and not
  `is_deleted`, because it is the same fact about the same kind of thing. Indexed as
  `(project, is_archived)` — the leading pair of the task list, the board and the counts aggregate.
- `TaskQuerySet.active()` and `TaskQuerySet.archived()` are the two scopes. They are **not** folded
  into `open()`, `counts()`, `blocked()` or `in_board_order()`. Those are *definitions* — "open" is
  a statement about a `WorkflowState.category` and nothing else — and a definition carrying the
  scope would apply it twice in `active().open()` and, worse, would silently re-apply it in the
  reads that must not have it.
- The default manager stays unfiltered. `Task.objects.all()` means all tasks. Django's own
  `Manager` override would make the filter invisible at the call site, which is the mechanism by
  which the three exemptions below would have been broken by someone who never saw them.
- `DELETE /api/v1/tasks/{code}` sets the flag and answers `204`; `PATCH {"is_archived": false}`
  restores. Both go through the one `update_task` service, which reads with `locked()` and
  **without** `active()` — the single write in the product that must see removed rows, because
  restoring one means finding one and re-removing one must answer "nothing changed" rather than
  "no such task".
- One topic, `task.archive_changed`, carrying `is_archived`, rather than a `task.deleted` and a
  `task.restored`. The subscriber's question is a boolean; the roster's
  `member.activation_changed` is the same decision for the same reason.
- Two activity verbs, `TASK_REMOVED` and `TASK_RESTORED`, written against the **project**, mirroring
  the `TASK_ADDED` that `create_task` writes. The timeline on `/projects/{code}` is where a task
  appearing and disappearing has to read as one story, and a record filed under a task nobody can
  open is a record nobody reads.

## Consequences

Good:

- History survives. Notes, blockers, dependency edges, activity records and already-published
  envelopes all keep pointing at a row that still exists.
- Removal is reversible from the product, not only from a database console, and the whole round
  trip is audited on both sides.
- No new error rows in `config/errors.py`: an unknown code is the existing `TaskNotFound` → 404,
  and an already-removed task is the same `204`.

**Three reads are deliberately not scoped, and each one is a rule rather than an oversight.** They
are the cost of this decision and the reason it is written down:

1. **`TaskQuerySet.next_code_for` counts removed tasks.** A code is permanent. It is what an
   already-published `task.created` envelope and every `ActivityRecord` about that task name, so
   handing `PRJ-01-T03` to a new task because the old one was removed would merge two histories in
   one timeline. `ProjectQuerySet.next_code` says the same thing about archived projects.
2. **`TaskDependencyQuerySet.adjacency()` and `.resolved()` keep removed nodes in the graph.** An
   edge whose endpoint was removed is still an edge. Filtering them would make the acyclicity check
   answer differently depending on what is currently hidden: remove the task that closes a loop,
   add the edge the loop forbade, restore the task, and the invariant that
   `work.domain.dependencies` exists to hold is gone. A graph that can be tricked by
   archive-then-restore is not a graph.
3. **`Blocker.objects.for_project(...)` keeps blockers raised against removed tasks.** A blocker on
   a removed task is still an open impediment on the project. Hiding it would let an operator clear
   a project's `BLOCKED` risk flag and lower its `blockage` signal by removing a task — exactly the
   accounting the typed blocker table exists to prevent. Clearing an impediment is resolving it,
   with a reason.

A fourth read is unscoped for a different reason and belongs beside them:
`workflow.repositories.records_on_states` counts removed tasks, because its question is referential
integrity — may this state be retired? — and a `PROTECT`ed foreign key does not care about the
operation's attention. Its docstring already said the same about archived projects.

Other costs:

- **Every new read of `work.Task` has to remember `.active()`.** That is the price of not putting
  the filter on the manager, and it is paid in review: a read added without it shows removed work.
  The mitigation is that the scope is a named method with a docstring listing its exemptions, not a
  `filter(is_archived=False)` somebody has to recognise.
- **A removed task's row still occupies the table and its code is still spent.** Correct: this is
  removal from attention, not from the ledger.
- **`GET /tasks/{code}` on a removed task is a 404**, which differs from `GET /projects/{code}` on
  an archived project — that one returns the project with `is_archived: true`. The asymmetry is
  deliberate: a project detail is a place an operator navigates to and archiving is a lifecycle
  stage there, while a removed task has no screen and no legal moves. It is reached instead through
  `GET /projects/{code}/tasks?is_archived=true`, which is what the restore control is built on. The
  `PATCH` response is read back unscoped, so a successful write is never answered with a 404.

## Alternatives considered

- **A hard `DELETE`.** Rejected on the cascade graph above: it destroys the notes, blockers and
  dependency edges that are the only record of why the work existed, and leaves the append-only
  trail and the published events naming a row that is gone.
- **A `Manager` whose `get_queryset` filters `is_archived=False`, with an `all_objects` escape
  hatch.** Rejected, and this is the closest call in this record. It makes the common case
  automatic — which is precisely what makes the three exemptions above dangerous: the code minter,
  the dependency graph and the blocker panel would each have had to *opt out* of an invisible
  default, and the person who forgot would get a plausible wrong answer rather than an error. A
  filter that is easy to forget is better than a filter that is impossible to see.
- **A `deleted_at` timestamp instead of a boolean.** Rejected. The instant is already in the
  `ActivityRecord`, where it is append-only and carries the actor and the correlation id; a second
  copy on the row would be a mutable, unattributed duplicate of it. `Project.is_archived` set the
  precedent and there is no question a timestamp here would answer that the trail does not.
- **Two topics, `task.deleted` and `task.restored`.** Rejected for the reason
  `frontend/src/lib/stream/topics.ts` already states about the roster: every subscriber would list
  both and handle them identically, which is the shape of the bug the first time somebody adds only
  one of them.
- **A dedicated `archive_task` service beside `update_task`.** Rejected. Removing and restoring are
  the same field moving in two directions, and two services would eventually disagree about what
  else a removal touches — the same argument `accounts.update_member` makes for keeping retire and
  restore in one place.
- **Cascading the removal to the task's notes and blockers.** Rejected. An impediment does not stop
  being real because the work it blocked was taken off the board, and consequence 3 above is what a
  cascade here would have quietly undone.
