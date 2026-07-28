---
name: aztec-domain
description: Business glossary and dataset context for Aztec Ops. Load before writing or reviewing anything in backend/apps/catalog, backend/apps/workflow, backend/apps/portfolio, backend/apps/work, backend/apps/prioritization, backend/apps/activity, the seed fixtures or the frontend views — i.e. whenever the words engagement type, blocked, at risk, no next step, health, score, owner load, or the source spreadsheet appear.
---

# Aztec Ops — domain

Normative source: `docs/ARCHITECTURE.md`. This skill is the working summary; when the two
disagree, the architecture document wins and this file gets fixed.

## 1. What the system is for

Aztec Ops is an operational command center, not a Jira clone. It exists to answer three
questions every morning (`ARCHITECTURE.md` §1):

1. What should be worked on today, and why exactly that?
2. What is at risk, blocked, or has no clear next step?
3. Who is overloaded?

This is the build order and the acceptance test for any feature. Question 1 is the
prioritization engine plus the `breakdown` shown next to the number. Question 2 is the risk
specifications and the blockers / no-next-step panels. Question 3 is computed owner load
against `TeamMember.weekly_capacity_points`. A feature that does not serve one of the three
belongs in §12 "deliberately out of scope", not in the repo.

## 2. Engagement type

`EngagementType` (`backend/apps/catalog`) is one of three, and it is a taxonomy row with a `weight`,
never a Python enum:

| Source label | What it is | Why it is prioritized differently |
|---|---|---|
| `Proyecto` | Bounded delivery with a target date and a scope | Deadline pressure is the dominant signal; slipping a date has contractual consequences |
| `Mantenimiento o recurrente` | Ongoing service, no end date | Never "finishes", so deadline pressure means little; it surfaces through staleness and open blockers instead |
| `Diagnostico` | Short assessment, small business value, fixed window | Cheap and short, so raw business value under-ranks it; the `weight` modifier keeps it from being permanently starved by large projects |

Counts in the source: 12 Proyecto, 6 Mantenimiento o recurrente, 4 Diagnostico.

`engagement_type.weight` is a **modifier** in the prioritization engine (§4.1) — it adjusts the
weighted sum, it is not a seventh signal. Compare against `engagement_type.code`, never against
the Spanish label; the label is data and an operator may rename it from the admin.

`EngagementType` also drives which workflow a project follows, through `WorkflowBinding`. A
Diagnostic can have a shorter lifecycle than a recurring maintenance engagement.

## 3. The three operational conditions (§3.3 invariants)

These are the definitions. Do not re-derive them locally in a view or a serializer; they live
as specifications in `backend/apps/prioritization` / risk domain code (§5) and everything else reads
the resulting `RiskFlag` list.

**Blocked** — a project is blocked if **any** of:
- it has at least one open `Blocker` (`resolved_at IS NULL`), or
- its `workflow_state.category == 'BLOCKED'`, or
- at least one of its tasks is in a state whose category is `BLOCKED`.

Note the disjunction: a project with every task in progress is still blocked if an access
request is pending. Query `category`, never a state `code`.

**No clear next step** — the project has no `next_step` **and** no task in a state whose
category is `IN_PROGRESS`. Both conditions must hold. A project with an empty `next_step` but
active work is fine; a project with neither is invisible work and is exactly what the challenge
asks us to surface.

**At risk** — not a single flag but the union of what the specifications emit:
`IsOverdue`, `HasNoTargetDate`, `IsStale`, `OwnerOverloaded`, plus `IsBlocked` and
`HasNoNextStep`. A null `target_date` is a risk signal (`NO_TARGET_DATE`), not benign missing
data — 5 of the 22 source projects have none, so this fires on real rows.

Adding a condition is one specification class plus one registry line (`CLAUDE.md` rule 8). If
you find yourself editing an existing `if`, stop.

## 4. Health is derived

`health` is computed from the risk flags. There is no editable `health` column that a human
sets, and no API route accepts one. The reason is auditability: an operator marking a project
"Sano" while it holds two open blockers is precisely the failure mode this system replaces.

The source spreadsheet does carry a `health` column (`Sano` 5, `En riesgo` 4, `Bloqueado` 13).
It is imported **only as a cross-check** against what our specifications derive. If the derived
value disagrees with the imported one, that is a finding to investigate in the specs — not a
reason to trust the spreadsheet. The system always uses the derived value.

Same principle for `PriorityScore`: derived, versioned by `policy_version`, and always stored
with its `breakdown`. The only human-set alternative is an explicit `PriorityOverride` with a
mandatory reason, labeled as an override in the UI (§4.2).

## 5. The source dataset

Normalized export: `data/raw/dataset.json`, four sheets — `Projects` (22), `Tasks` (82),
`Team` (5), `Notas` (junk, ignore). 16 distinct clients, 5 team members.

Traps, all of which `backend/scripts/xlsx_to_fixtures.py` resolves once at fixture-generation time so
that no runtime code ever sees them:

- **Null dates.** `start_date` null on 9 projects, `target_date` null on 5. Null is a signal,
  not a default — never backfill it with `today()` or a sentinel.
- **The literal string `'None'`.** A pandas `.astype(str)` on the xlsx produces `'None'` and
  `'nan'` cells. The current export is clean, but the generator must still normalize them; do
  not delete that normalization because a spot check looked fine.
- **Free-text English blockers.** `Projects.blockers` is prose, and only four distinct strings
  across the whole file (e.g. "There are external dependencies or pending accesses. There are
  overdue tasks without a clear close path."). It becomes typed `Blocker` rows with a `kind`
  (`EXTERNAL_DEPENDENCY | ACCESS | DECISION | TECHNICAL`). An open blocker is a first-class
  row, never a substring match at query time.
- **Free-text dependency.** 61 of 82 tasks carry a `dependency` that is a task *title*, not a
  code ("Functional validation and release checklist"). Match it against task titles within the
  same project; when it does not resolve, keep it in `TaskDependency.raw_label` rather than
  dropping it. Cycles are rejected.
- **`Team` counters are a projection.** `open_tasks_assigned`, `blocked_tasks_assigned`,
  `high_or_critical_open`, `*_projects` are precomputed aggregates of the task rows. They are
  **not imported**. Owner load is recomputed from `Task`. Same for `Projects.open_tasks` and
  `Projects.overdue_tasks`.
- **`status` is `Activo` on all 22 projects.** The source has zero lifecycle variety, so the
  project workflow state is seeded from `stage` (Ejecucion 18, Descubrimiento 4), and the
  remaining states are exercised by demo fixtures so the "projects in different states"
  requirement is actually met.
- **No completed tasks at all.** Task status is `Por hacer` 23, `En progreso` 21,
  `En revision` 21, `Bloqueada` 17. The dataset is pure open backlog, so any code path that
  assumes a done task exists will only break in production. `Hecha` exists in the task
  workflow regardless.
- **`is_overdue` is a string `Si`/`No`** (34 / 48), and it is redundant with `due_date`.
  Derive overdue from the date; do not import the flag.
- **`business_value` and all counters arrive as strings** (`"28000"`). Cast at generation time.
  Business value is normalized on a log scale in the engine — 28k does not deserve 3.5x the
  attention of 8k.

Task priority in the source: `Critica` 13, `Alta` 38, `Media` 23, `Baja` 8 — these become
`Priority` rows with numeric `weight`.

## 6. Code standards that bind this vocabulary

Normative: `docs/standards/BACKEND.md` and `docs/standards/PATTERNS_BACKEND.md`. Three rules from
them decide whether the glossary above survives contact with the code:

1. **Compare `code` or `category`, never a label** (`BACKEND.md` §3, `PATTERNS_BACKEND.md` §6,
   `CLAUDE.md` rule 1). Taxonomies (`EngagementType`, `Priority`, `Blocker.kind`) are matched on
   `code`; workflow states on `category`, the closed five-value set. Any string literal in a
   comparison must be one of those; `"Proyecto"`, `"Bloqueada"`, `"Sano"` appearing in a condition
   is a defect, not a style preference.
2. **A domain definition lives in exactly one docstring** (`BACKEND.md` §2). The blocked
   three-way OR, the "no next step" conjunction and the derivation of `health` are stated in the
   docstring of the specification or the health function that owns them — with the boundary values
   — and nowhere else. A docstring that paraphrases the signature does not count as stating the
   invariant.
3. **Dataset traps are Pydantic model fields, not dicts or ORM reads**
   (`BACKEND.md` §1, `PATTERNS_BACKEND.md` §5, §9). Null `target_date`, open blocker counts and
   owner load reach a signal or a specification through `SignalInput` / `ProjectRiskInput` —
   `BaseModel`s typed as `date | None` and `int` — never `dict[str, Any]`, never a query inside
   `is_satisfied_by`. `Optional` is how the null-is-a-signal rule is enforced by mypy, and
   `BaseModel` is what makes it fail at construction instead of three layers later.

## Common mistakes

- Comparing against Spanish labels (`if state.label == "Bloqueada"`). Compare `code` for
  taxonomies, `category` for workflow states. Labels are operator-editable data.
- Treating "blocked" as "state category is BLOCKED". It is a three-way OR that includes open
  blockers and blocked tasks.
- Treating "no next step" as "`next_step` is empty". It also requires no in-progress task.
- Importing the `Team` sheet counters or `Projects.open_tasks` / `overdue_tasks` because they
  are right there in the file. They are a stale projection; recompute them.
- Backfilling a null `target_date` to make a sort or a `timedelta` work. That erases the
  `NO_TARGET_DATE` signal, which is one of the few risk conditions the real data triggers.
- Storing `health` as a column someone can PATCH, or lowering a score because the owner is
  overloaded. Owner saturation raises the `OWNER_OVERLOADED` flag; priority belongs to the
  work, not to who happens to be free.
- Regex-matching the free-text `blockers` prose at query time instead of reading `Blocker` rows.
- Dropping an unresolvable `dependency` string instead of keeping it in `raw_label`.
- Adding a risk condition or a prioritization signal by extending an existing branch instead of
  registering a new class.
- Restating the blocked / no-next-step / health definitions in a second place — a serializer
  comment, a consumer, a view — instead of leaving them in the owning specification's docstring.
  Two copies of a definition is one definition and one future contradiction.
- Writing a specification or signal docstring that repeats the signature ("Checks if the project
  is stale") instead of naming the fact and the boundary: `STALE_AFTER_DAYS = 14`, what a null
  `target_date` scores, what an ownerless project returns.
- Passing project facts into a signal or specification as `dict[str, Any]` scraped from the ORM.
  They take `SignalInput` / `ProjectRiskInput`; a `.objects.` call inside `is_satisfied_by` makes
  the spec impure and untestable without a database.
- Typing `target_date` or `business_value` as non-optional to avoid a mypy complaint. The null is
  the `NO_TARGET_DATE` signal; `date | None` is what keeps it from being backfilled downstream.
- Naming a domain concept `data`, `info`, `prio` or `wf`, or parking a domain helper in a
  `utils.py`. The glossary words are the identifiers: `engagement_type`, `open_blocker_count`,
  `owner_load_points`.
