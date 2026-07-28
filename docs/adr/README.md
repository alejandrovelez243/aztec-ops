# Architecture decision records

One file per decision, short form: Status, Context, Decision, Consequences, Alternatives
considered. The table in `docs/ARCHITECTURE.md` §2 is the index of the same decisions in
one line each; these files carry the pressure that produced each decision and the cost we
accepted with it.

`docs/ARCHITECTURE.md` stays normative for *what the system is*. An ADR is normative for *why it
is that way*. When code contradicts an ADR, either the code is wrong or the ADR is superseded by
a new one — an ADR is never edited to match code that drifted.

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-django-and-django-ninja.md) | Django 6 with django-ninja for the API | Accepted 2026-07-28 |
| [0002](0002-postgresql.md) | PostgreSQL as the only datastore of record | Accepted 2026-07-28 |
| [0003](0003-transactional-outbox-with-redis-streams.md) | Transactional outbox relayed to Redis Streams | Accepted 2026-07-28, transport section superseded by 0010 |
| [0004](0004-sse-instead-of-websockets.md) | SSE instead of WebSockets for live updates | Accepted 2026-07-28 |
| [0005](0005-deterministic-versioned-prioritization.md) | Deterministic versioned prioritization, not an LLM ranking | Accepted 2026-07-28 |
| [0006](0006-workflows-and-taxonomies-as-data.md) | Workflows and taxonomies as data, not enums | Accepted 2026-07-28 |
| [0007](0007-django-fixtures-for-seed-data.md) | Django fixtures for seed data, not a custom importer | Accepted 2026-07-28 |
| [0008](0008-astro-with-islands.md) | Astro 7 with islands instead of an SPA | Accepted 2026-07-28 |
| [0009](0009-celery-beat-for-scheduling.md) | Celery Beat for scheduling, not for the bus | Accepted 2026-07-28, transport section superseded by 0010 |
| [0010](0010-celery-as-the-bus.md) | Celery as the bus, Redis Streams removed | Accepted 2026-07-28 |

## Writing a new one

Take the next free number, name the file `NNNN-<kebab-slug>.md`, copy the section order from any
existing record, and add the row above. To reverse a decision, write a new ADR that supersedes
the old one and set the old one's status to `Superseded by NNNN`. Numbers are never reused.
