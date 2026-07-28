---
name: prioritization-engine
description: Load when working on the ranking in backend/apps/prioritization — adding or changing a priority signal strategy, touching PriorityPolicy weights or policy versions, the PriorityScore breakdown JSONB, PriorityOverride, the risk Specifications (IsBlocked, IsOverdue, HasNoNextStep, HasNoTargetDate, IsStale, OwnerOverloaded) and RiskFlag severity, writing database-free tests for any of them, or answering "why is this project ranked first" from a persisted breakdown.
---

# Prioritization engine

Normative source: `docs/ARCHITECTURE.md` §4 (engine), §5 (specifications), §7 (layers).
Score is 0–100, deterministic, versioned, and always accompanied by its reasons. No LLM, no
randomness, no `datetime.now()` inside a strategy — time enters through `SignalInput.now`.

## The criterion

Weighted sum of six normalized signals, each an independent strategy returning
`(score_0_1, reason)`. Weights come from the active `PriorityPolicy.weights` JSONB.

| Signal code | Weight | What it measures |
|---|---|---|
| `deadline_pressure` | 0.25 | Days until `target_date`. Overdue = 1.0. No date = 0.5 plus `NO_TARGET_DATE`. |
| `overdue_work` | 0.20 | Overdue tasks / open tasks. |
| `criticality` | 0.15 | Volume of open tasks whose `priority.code` is critical or high. |
| `business_value` | 0.15 | Contract value, log-normalized. |
| `blockage` | 0.15 | Open blockers, weighted by age. |
| `staleness` | 0.10 | Days without `ActivityRecord`, plus absence of `next_step`. |

Three design points that get misread, and their justification:

- **`business_value` is logarithmic.** A 28k engagement does not deserve 3.5x the operational
  attention of an 8k one. Linear normalization lets the two or three largest contracts pin the
  top of the queue permanently and makes every other signal noise. Log compresses the tail so
  value orders projects without dominating them.
- **An old blocker raises the score.** `blockage` grows with `age_days` of the oldest open
  `Blocker`. A blocker that has been open for three weeks is not "waiting", it is rotting: it
  needs intervention today. Decaying it would hide exactly the projects the command center
  exists to surface.
- **Owner saturation does not lower the score.** When the owner's computed load exceeds
  `weekly_capacity_points`, the project gets the `OWNER_OVERLOADED` flag and keeps its score.
  Priority belongs to the work, not to who happens to be free. Lowering the score would make a
  bottleneck invisible precisely when it matters; the flag makes it a staffing decision.

Modifiers multiply the weighted sum, they are never extra addends. Currently
`engagement_type.weight` only.

## Adding a signal

One class plus one registry entry. Nothing in the evaluator changes.

1. Create `backend/apps/prioritization/domain/signals/<code>.py` implementing the signal interface:
   `evaluate(self, data: SignalInput) -> SignalResult`, where `SignalResult = tuple[float, str]`.
   Read an existing strategy first and copy its shape.

```python
# backend/apps/prioritization/domain/signals/blockage.py
from ..registry import register
from ..types import SignalInput, SignalResult

AGE_SATURATION_DAYS = 21


@register("blockage")
class Blockage:
    """Open blockers, weighted by the age of the oldest one."""

    def evaluate(self, data: SignalInput) -> SignalResult:
        if not data.open_blockers:
            return 0.0, "No open blockers."
        oldest = max(b.age_days(data.now) for b in data.open_blockers)
        score = min(1.0, oldest / AGE_SATURATION_DAYS)
        return score, (
            f"{len(data.open_blockers)} open blocker(s); the oldest has been open "
            f"for {oldest} day(s) and needs intervention."
        )
```

2. Register it under a stable `code` — the decorator is the registry entry. The `code` is the key
   used in `PriorityPolicy.weights` and in the persisted `breakdown`; it never changes once a
   score has been written with it.
3. Add the weight in a **new** `PriorityPolicy` version (fixture or data migration), rebalancing
   the others so the weights sum to 1.0 before modifiers. Move `is_active` to the new version.
   Do not edit the active row: existing `PriorityScore` rows keep their `policy_version` and stay
   reproducible.
4. Write the pure test in `backend/apps/prioritization/tests/domain/` as a
   `django.test.SimpleTestCase` class named after the signal and the situation. `SimpleTestCase`
   forbids database access, so it is the base class that turns "the engine is pure" into a
   mechanically enforced fact: a strategy that grows an ORM call fails the test instead of passing
   it. Table-driven over the boundaries (no blockers, one blocker at day 0, at
   `AGE_SATURATION_DAYS`, past it) with `subTest`, asserting the reason string as well as the
   number, with unittest assertions.

```python
# backend/apps/prioritization/tests/domain/test_blockage.py
from django.test import SimpleTestCase

from apps.prioritization.domain.signals.blockage import AGE_SATURATION_DAYS, Blockage


class BlockageAgeSaturationTests(SimpleTestCase):
    signal = Blockage()

    def test_no_open_blockers_scores_zero(self) -> None:
        score, reason = self.signal.evaluate(make_input(open_blockers=[]))
        self.assertEqual(score, 0.0)
        self.assertIn("No open blockers", reason)

    def test_oldest_blocker_saturates_the_signal(self) -> None:
        for days, expected in ((0, 0.0), (AGE_SATURATION_DAYS, 1.0), (AGE_SATURATION_DAYS + 7, 1.0)):
            with self.subTest(days=days):
                score, reason = self.signal.evaluate(make_input(blocker_age_days=days))
                self.assertEqual(score, expected)
                self.assertIn(str(days), reason)
```
5. Run `pytest backend/apps/prioritization -k <code>` and `make lint` (mypy strict covers `domain/`).
6. Run `make recompute` so persisted scores reflect the new policy version.

A signal key present in `weights` with no registered strategy — or the reverse — raises at policy
load. It is never silently defaulted.

## Adding a risk criterion

Same shape, Specification pattern (`docs/ARCHITECTURE.md` §5). Specifications compose with
`and` / `or` / `not`, so a new condition is a class, a `flag_code`, a severity and a registry
line. The evaluator is untouched.

```python
# backend/apps/prioritization/domain/specifications.py
@register_risk(flag_code="NO_TARGET_DATE", severity=Severity.MEDIUM)
class HasNoTargetDate(Specification):
    def is_satisfied_by(self, data: RiskInput) -> bool:
        return not data.is_archived and data.target_date is None
```

The evaluator returns a list of `RiskFlag`. Project health is **derived** from the flags at read
time; there is no editable health field, and the dataset's imported `health` column is a
cross-check only.

## Manual override

`PriorityOverride(project, position | boost, reason, actor, expires_at)`. `reason` is mandatory
and non-empty. The override is stored separately and is never written into `PriorityScore.value`:
the computed score stays visible next to it, so the ranking remains auditable and the override
remains reversible. It emits an `ActivityRecord` with verb `PRIORITY_CHANGED` and origin
`MANUAL` (engine-driven recomputations use origin `POLICY` and name the signal that moved), and
the UI labels the row as an override. A forced position with no recorded reason is
indistinguishable from a bug three weeks later — that is why the reason is a constraint, not a
convention.

## Reading a breakdown out loud

`PriorityScore.breakdown` stores, per signal: its `code`, `raw` value, `weight`, `contribution`
and `reason`. To justify a rank, sort the entries by `contribution` descending and read the top
two or three, then the modifiers and flags. For example:

> PRJ-07 scores 84 under policy v2. Deadline pressure contributes 25 of that: overdue by 6 days.
> Blockage contributes 13: one blocker open for 19 days. Overdue work contributes 12: 4 of 7 open
> tasks are past due. The engagement type modifier (Proyecto, 1.1) lifted the total, and the
> project carries `OWNER_OVERLOADED` — the score is real, the owner is the constraint.

Never justify a rank from the model fields directly. If the breakdown does not explain the
number, the breakdown is the bug.

## Code standards that bind here

Normative: `docs/standards/BACKEND.md` and `docs/standards/PATTERNS_BACKEND.md` (§4 Strategy +
Registry, §5 Specification, §9 Value Object). The three rules that bite hardest in this area:

- **A new signal or risk criterion is a class plus a registry line, never a branch.**
  `PATTERNS_BACKEND.md` §4 and `BACKEND.md` §4 (OCP): the evaluator iterates the registry and never
  learns a signal's name. Checkable: the diff that adds a signal touches one new file under
  `domain/signals/` plus one `PriorityPolicy` fixture row — `grep -n "signal_code ==" ` and
  `grep -n "flag_code ==" ` over `backend/apps/prioritization/` must stay empty.
- **No number that the operation may want to change lives in code.** `BACKEND.md` §3: weights come
  from the active `PriorityPolicy.weights`, keyed by registered signal code. Structural thresholds
  are `Final` named constants in `domain/` (`AGE_SATURATION_DAYS: Final[int] = 21`), never inline
  literals. Checkable: no float literal outside a constant or a fixture in a strategy body.
- **`domain/` is pure and strictly typed.** No Django import, no ORM access, no `datetime.now()`,
  no `dict[str, Any]` crossing into a strategy — the input is the `SignalInput` /
  `ProjectRiskInput` frozen Pydantic model, `SignalResult` is frozen too
  (`PATTERNS_BACKEND.md` §9). `BaseModel` rather than a plain container because it is the same
  type system django-ninja uses, so these objects reach the API without a parallel schema and are
  validated at construction instead of at the boundary.
  mypy strict covers `apps.*.domain.*`; a `cast()` here must be justified by a check in the same
  function (`BACKEND.md` §1). `OwnerOverloaded` reads `subject.owner_load_points`, it does not
  count tasks.

Docstrings on every strategy and specification state the fact measured and the boundary values
(`BACKEND.md` §2): "Overdue returns 1.0; a null `target_date` returns 0.5 and raises
`NO_TARGET_DATE`." Tests in `tests/domain/` are `SimpleTestCase` classes, one per behaviour under
test, with no factory and no database: the base class refuses database access, so purity is proved
by the suite rather than asserted in review, and coverage here should be high. Methods keep the
`test_` prefix, are named after the behaviour rather than the method under test, use the unittest
assertions (`self.assertEqual`, `self.assertIn`, `self.assertRaises`) and `subTest` for
table-driven cases, and assert the `reason` / `detail` string as well as the number
(`BACKEND.md` §7, `CLAUDE.md` rule 15). The database-backed tests of this app — recompute,
consumer idempotency, policy load — are `TestCase` classes; anything that goes through the outbox
or `on_commit` is a `TransactionTestCase`, because `TestCase` never commits and such a test would
pass while proving nothing.

## Common mistakes

- Hardcoding a weight inside a strategy, or reading it from a module constant instead of the
  active `PriorityPolicy.weights`. The strategy owns normalization, never weighting.
- Mutating the active `PriorityPolicy` row instead of creating a new version, which silently
  rewrites the meaning of every previously persisted score.
- Adding a branch to an existing `if` or to a dispatcher instead of adding a strategy or a
  specification. Rule 8 in `CLAUDE.md`: if you had to edit an existing conditional, the design
  is wrong.
- Returning a score with no reason, or a reason like "high priority" that restates the number.
  The reason must name the fact (days, counts, dates) that produced it.
- Comparing against labels — `priority.label == "Critica"`, `state.label == "Bloqueada"`.
  Compare against `priority.code` and `workflow_state.category`. Labels are Spanish data and
  are admin-editable.
- Importing Django or a `Project` model instance into `domain/`. Strategies take a plain
  Pydantic input model that already includes `now`.
- Calling `datetime.now()` inside a strategy, which makes the test non-deterministic and the
  score non-reproducible.
- Lowering the score for an overloaded owner, or decaying `blockage` with age. Both hide the
  situation the ranking exists to expose.
- Forgetting to recompute after changing data, a policy or a signal, so the API keeps serving
  stale `PriorityScore` and `ProjectSnapshot` rows. Recomputation is event-driven in normal
  operation; after a seed or a policy change run `make recompute` explicitly.
- Writing an override into `PriorityScore.value` "so the sorting is simpler". It destroys the
  audit trail and the UI can no longer label it as an override.
- An unnamed numeric literal inside a strategy — `21`, `0.5`, `40` written inline. It becomes a
  `Final` constant in `domain/` when it is structural, or a `PriorityPolicy` weight when the
  operation must be able to change it (`BACKEND.md` §3).
- Passing a `dict[str, Any]` into a strategy or specification instead of `SignalInput` /
  `ProjectRiskInput`, or returning a bare `(float, str)` tuple where a frozen `SignalResult` with
  its clamping invariant is expected (`BACKEND.md` §1, `PATTERNS_BACKEND.md` §9).
- A specification that reaches for the ORM to get the fact it needs — `Task.objects.filter(...)`
  inside `is_satisfied_by`. It breaks composition, LSP and every database-free test
  (`BACKEND.md` §4). The caller loads the facts; the specification only decides.
- A docstring on a strategy that paraphrases the signature ("Evaluates the signal.") instead of
  naming the measured fact and its boundary values (`BACKEND.md` §2). Ruff `D` passes; review does
  not.
- Writing a domain test as a loose module-level `def test_...`, or on `TestCase` instead of
  `SimpleTestCase`, or reaching for a factory to build a `SignalInput`. `TestCase` grants the
  database, so it lets an impure strategy pass; `SimpleTestCase` is what makes the purity of
  `domain/` a fact the suite proves. The database belongs to the integration tests: recompute,
  consumer idempotency, policy load (`BACKEND.md` §7, `PATTERNS_BACKEND.md` §10).
- Reintroducing `pytest.mark.django_db` or a pytest fixture anywhere in this app. The base class
  already declares what database access a test gets; a second mechanism for the same decision is
  how a pure test quietly starts hitting the database (`CLAUDE.md` rule 15).
- Testing the outbox emission of a recomputation on `TestCase`. It wraps the test in a transaction
  that never commits, so `on_commit` never fires and the relay's `SELECT ... FOR UPDATE SKIP
  LOCKED` on another connection cannot see the row — use `TransactionTestCase`.
- Bare `assert` in a test instead of `self.assertEqual` / `self.assertIn` / `self.assertRaises`,
  or duplicating a method per boundary value instead of one `subTest` loop.
- Introducing a `BaseSignalEvaluator` ABC or a second `Protocol` layer over the registry. The
  registry plus the existing protocol is the abstraction; anything above it is the premature
  abstraction banned in `PATTERNS_BACKEND.md` §11.
