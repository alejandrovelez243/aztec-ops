# 0007 — Django fixtures for seed data, not a custom importer

## Status

Accepted — 2026-07-28.

## Context

The system ships with a real dataset: 22 projects, 82 tasks, 5 team members, 16 clients,
normalized into `data/raw/dataset.json` from the provided spreadsheet. Anyone bringing the
project up needs that data present, identical, and present again after a reset.

The data has rough edges that have to be resolved somewhere. `'None'` arrives as a four-character
string rather than a null. The `blockers` column is free text that has to become typed `Blocker`
rows with a `kind`. 61 of 82 tasks carry a free-text `dependency` that has to be matched against
task titles within the same project, with a `raw_label` fallback when it does not resolve. The
`Team` sheet's counters are a projection of the task data, not a source of truth, and importing
them would create a second version of numbers the system computes.

The question is when that resolution happens: once, at authoring time, with a human reading the
result; or on every load, inside code that runs against a live database.

That distinction matters more than it looks. An importer that parses text at load time is a
piece of production code with no production purpose — it must be tested, maintained, and kept
working against a spreadsheet nobody will open again. And its output is invisible: a reviewer
who wants to know what got seeded has to run it and query the database. Resolving the edges up
front makes the outcome a file in the diff.

## Decision

The spreadsheet is converted once into Django fixtures committed under `backend/apps/*/fixtures/`,
loaded with `manage.py loaddata catalog workflows portfolio work activity` behind `make seed`,
which then chains `make recompute` for scores and risk flags.

- A developer-only script, `backend/scripts/xlsx_to_fixtures.py`, regenerates the fixtures from the
  source when the data changes. It is never imported by Django code and never runs in a request,
  a service, a consumer or a migration.
- Every fixture object carries an explicit primary key, derived deterministically from the
  natural key (`project_code`, `task_code`, taxonomy `code`), so `loaddata` is an upsert and
  regenerating on unchanged input produces a byte-identical file.
- The generator resolves the rough edges: `'None'` and its siblings become real nulls, the
  `blockers` text becomes typed `Blocker` rows classified by a keyword table that lives in one
  dict, and `dependency` is matched against `Task.title` within the same project — never across
  projects — falling back to `raw_label` on no match. Nothing is dropped.
- The `Team` counters are not imported. They may be asserted against in a test, never used as a
  data source.
- Every seeded row gets an `ActivityRecord` with verb `SEEDED`, actor `system`, and an
  `occurred_at` fixed by the generator rather than `now()`, so the fixture stays deterministic.
- The source spreadsheet is read-only. The script never writes back to it.
- Because the source has no project lifecycle variety — every row is `Activo` — the project
  workflow state is seeded from `stage`, and the additional states are exercised by demo fixtures
  so the "projects in different states" requirement is met by data rather than by claim.

## Consequences

Good:

- `make seed` twice leaves the database identical: same row count, same primary keys, no
  duplicated `Blocker`, `TaskDependency` or `ActivityRecord`. Idempotency comes from stable
  primary keys, not from delete-and-reload.
- The seed is reviewable as JSON in a diff. A change to what gets seeded shows up in a pull
  request instead of hiding inside parsing logic.
- No parsing code in the runtime path means no runtime failure mode from the spreadsheet's shape,
  and no test suite maintaining a converter for an input that will not change again.
- `loaddata` is one command that the framework already ships and already tests.

Cost we accepted:

- The fixtures and the spreadsheet can diverge. Regeneration is a manual step someone will skip,
  and nothing in CI proves the committed fixtures still match `data/raw/dataset.json`.
- Fixture JSON is verbose and low-signal in a diff. Regenerating after a model rename produces a
  large mechanical change that is easy to approve without reading.
- Fixtures reference model fields by name and primary keys by value, so a model change breaks
  `loaddata` at load time rather than at type-check time, and a renamed field means regenerating
  rather than migrating.
- The dependency matching heuristic bakes its decisions into the committed data. A bad match is
  now a wrong row in a file rather than a bug that could be fixed centrally; correcting it means
  fixing the script and regenerating.
- Explicit primary keys mean the fixtures own a primary key range. Any row created another way in
  that range collides.

## Alternatives considered

- **A custom `.xlsx` importer as a management command run at startup.** Rejected. It puts
  parsing, null-coercion and fuzzy text matching into the runtime path, where all three become
  code to maintain and to trust, and it makes the seeded result invisible until someone queries
  the database.
- **Data migrations.** Rejected. Migrations are for schema and for corrections that must run
  exactly once in order. Seed data must be re-runnable and editable, and encoding 22 projects
  into a migration makes changing the demo data a migration-history problem.
- **A `post_migrate` signal that seeds automatically.** Rejected. Seeding would become implicit
  and would run in test databases and in any environment that migrates, with no way to opt out.
  `make seed` being an explicit command is the point.
- **`factory_boy` factories as the seed.** Rejected. Factories are for tests and generate
  plausible data. This dataset's specific irregularities — 5 projects with no target date, zero
  completed tasks, free-text dependencies — are exactly what makes the risk specifications fire
  on real rows instead of on invented ones. Generated data would hide them.
