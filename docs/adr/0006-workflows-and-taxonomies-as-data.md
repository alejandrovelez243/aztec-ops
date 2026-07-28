# 0006 — Workflows and taxonomies as data, not enums

## Status

Accepted — 2026-07-28.

## Context

The source data forces this. Its vocabulary is Spanish and specific to one operation:
`engagement_type` is Proyecto, Mantenimiento o recurrente, Diagnostico; `stage` is Descubrimiento
and Ejecucion; task status is Por hacer, En progreso, En revision, Bloqueada; priority is
Critica, Alta, Media, Baja. None of these are universal categories. They are one company's
current way of describing its work, and they will change — a new engagement type, a review step
that becomes two steps, a "Pausado" state that someone needs by the end of the quarter.

The dataset also shows the vocabulary is already incomplete. Every project's status is `Activo`;
there is no project lifecycle variety in the source at all, and there is not one completed task.
The states the operation actually needs (Paused, Blocked, Done, Cancelled) are absent from the
data but obviously required by the product. Any set of states hardcoded from this dataset would
be wrong on the first day of real use.

If those values were `TextChoices`, adding a state would mean a code change, a migration and a
deploy — and, worse, a search through the codebase for every `if status == ...` that needs a new
branch. The rule in `CLAUDE.md` that adding a signal or a criterion must never require editing an
existing `if` applies here too: a system whose vocabulary is spelled into its control flow cannot
absorb a new word.

There is a second, sharper problem. The product requires that a project cannot jump to an
arbitrary state. That is a graph of legal transitions, and different engagement types need
different graphs — a Diagnostico does not follow the lifecycle of a Mantenimiento o recurrente.
A graph with per-edge rules (a mandatory reason, a field that must be non-empty before the
transition is allowed) is data. Expressing it as code is expressing a table as nested
conditionals.

## Decision

Every taxonomy and every workflow lives in admin-editable tables.

- `backend/apps/catalog`: `EngagementType`, `ProjectType`, `Stage`, `Priority`, `Role`. Each has `code`
  (stable slug), `label`, `order`, `is_active`, `color`, plus the weights the prioritization
  engine consumes.
- `backend/apps/workflow`: `Workflow` (`applies_to` = `PROJECT` | `TASK`), `WorkflowState`,
  `WorkflowTransition`, `WorkflowBinding` (binds a workflow to an `EngagementType`, or marks the
  default).
- `WorkflowState.category` is one of `BACKLOG | IN_PROGRESS | BLOCKED | DONE | CANCELLED`. The
  rest of the system queries `category`, never the specific `code`. That is the mechanism that
  lets a new state be added without touching any logic: a new blocked-category state is blocked
  everywhere immediately.
- `WorkflowTransition` carries `label`, `requires_reason`, `requires_fields` (fields that must be
  non-empty, for example `next_step`), `guard` and `is_active`. A transition runs only if an
  active `from → to` edge exists; the API never assigns `workflow_state` directly, and an illegal
  transition raises `TransitionNotAllowed`, not a 500.
- Code compares against `code` and `category`, never against labels. Labels are Spanish display
  data. Colors come from the taxonomy row, not from a switch on state names.
- The frontend renders transition buttons from the legal transitions the detail response returns,
  including `requires_reason` and `requires_fields`. There is no hardcoded button list and no
  client-side guess at what is legal.

## Consequences

Good:

- Adding a state, a transition, an engagement type or a priority is an admin edit. No migration,
  no deploy, no release window.
- The Spanish/English split stays clean: labels are data, identifiers are English code. The UI
  can be translated by editing rows.
- Business logic reads `category`, so it survives vocabulary changes it has never heard of.
- `Priority.weight` and `EngagementType.weight` being editable rows is what makes the
  prioritization policy in ADR 0005 tunable without a code change.

Cost we accepted:

- Foreign keys everywhere a `CharField` would have sufficed. Every query that touches a state or
  a taxonomy needs a `select_related`, and forgetting one is an N+1 in the main view. That is why
  every queryset lives in `repositories.py` — one place to fix it.
- No compile-time checking of the vocabulary. `TextChoices` would let `mypy` catch a typo in a
  state code; a database row cannot. A misspelled `code` in a fixture is accepted and fails at
  runtime, and the mitigation is tests plus the fixtures' stable primary keys, not the type
  checker.
- The admin can create an incoherent configuration: a workflow with no initial state, a state
  with no outbound transitions, a transition graph with an unreachable branch. Nothing prevents
  it at write time, so validation has to be explicit and the RUNBOOK has to say what a broken
  workflow looks like from the UI.
- `category` is now itself a fixed vocabulary. We moved the enum up one level rather than
  eliminating it, and adding a sixth category *is* a code change. That is deliberate: five
  categories is a small, stable set that the logic can reason about, and it is the boundary that
  makes the layer below it free.
- Seed data becomes load-order sensitive. Taxonomies and workflows must be loaded before
  portfolio and work, which is why `make seed` names the fixtures in a fixed sequence.

## Alternatives considered

- **`TextChoices` on the models.** Rejected. Every vocabulary change becomes a migration and a
  deploy, and the transition rules end up as conditionals that must each be found and edited.
  With a dataset whose entire state vocabulary is one company's Spanish and demonstrably
  incomplete, this locks the wrong words into the code on day one.
- **A hybrid: taxonomies in the database, transitions in code.** Rejected. Transitions are the
  part with per-edge rules (`requires_reason`, `requires_fields`, guards) and the part that
  differs per engagement type. Keeping the graph in code preserves exactly the constraint we are
  trying to remove.
- **A workflow engine library.** Rejected. It would bring a state-machine DSL and its own
  persistence model for a graph that is four tables and one transition service, and admin
  editability — the actual requirement — is not what those libraries optimize for.
