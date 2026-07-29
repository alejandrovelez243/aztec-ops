# 0005 — Deterministic versioned prioritization, not an LLM ranking

## Status

Accepted — 2026-07-28. Narrowed by [0011](0011-risk-flags-computed-on-read.md): the *score* stays
deterministic, versioned and persisted exactly as decided here; the risk flags evaluated by the same
engine are no longer persisted, because a stored derivation with no before/after to publish buys
nothing.

## Context

The product's single hardest requirement is an explicit, explainable prioritization criterion.
The operations lead does not need a list; they need a list they can defend to the person whose
project is fourth. Success, as `PRODUCT.md` states it, is closing the tab with an ordered list
they can justify to someone else.

That imposes three properties the ranking must have:

- **Reproducible.** The same portfolio state must produce the same order today and next month.
  If the ranking moves, a data change moved it, and the system must be able to name which one.
- **Auditable.** Every reprioritization writes an `ActivityRecord`, and a `POLICY` record has to
  name the signal that moved. That is impossible if the ranking is the output of a process that
  cannot state its own inputs.
- **Answerable.** "Why is this first" must be answered by the record, not by re-running anything.

The source data makes this sharper rather than softer. All 22 projects have status `Activo`,
not one of the 82 tasks is complete, and 5 projects have no target date. There is no historical
outcome signal to learn from and no completion data to correlate against — the entire dataset is
open backlog. A model asked to rank it would be doing style transfer on field values, not
inference.

This role is advertised as AI Engineer, so choosing not to put a model in the ranking needs to
be stated rather than assumed. The judgement is that the AI-engineering decision here *is* the
scoping decision: knowing that a ranking which drives operational decisions and carries a
persisted audit trail is the wrong place for a non-deterministic component, and that using one
would make the product's central promise — the number comes with its reasons — unkeepable.
A ranking is a policy, and a policy should be legible, versioned and diffable. The right places
for a model in this product are the ones where its failure mode is a bad suggestion rather than
a bad decision: summarizing a project's timeline, drafting a `next_step` for a project flagged
as having none, or classifying the free-text `blockers` and `dependency` columns. Those are
noted as extensions and are deliberately not in the ranking path.

## Decision

A 0–100 score computed as the weighted sum of six normalized signals, each an independent
strategy returning `(score_0_1, reason)`:

| Signal code | Weight | What it measures |
|---|---|---|
| `deadline_pressure` | 0.25 | Days until `target_date`. Overdue = 1.0. No date = 0.5 plus `NO_TARGET_DATE`. |
| `overdue_work` | 0.20 | Overdue tasks over open tasks. |
| `criticality` | 0.15 | Volume of open tasks whose `priority.code` is critical or high. |
| `business_value` | 0.15 | Contract value, log-normalized. |
| `blockage` | 0.15 | Open blockers, weighted by age. |
| `staleness` | 0.10 | Days without `ActivityRecord`, plus absence of `next_step`. |

- Weights live in `PriorityPolicy(version, is_active, weights JSONB)`, edited from the admin.
  Changing them means a new version with `is_active` moved; existing `PriorityScore` rows keep
  their own `policy_version`, so a past ranking stays reproducible.
- `engagement_type.weight` is a modifier that multiplies the weighted sum, never a seventh
  addend. Owner saturation raises the `OWNER_OVERLOADED` flag and never lowers the score.
- Every `PriorityScore` persists `breakdown` (JSONB: per signal, its raw value, weight,
  contribution and human-readable reason) next to `value` and `computed_at`. The UI renders the
  number and its breakdown in the same glance.
- No LLM, no randomness, no `datetime.now()` inside a strategy. Time enters as an explicit `now`
  on the input model. Strategies import neither Django nor another app and are unit-tested
  with no database. `SignalInput` and `SignalResult` are Pydantic models — the same type system
  django-ninja uses, so a breakdown reaches the API without a parallel schema restating its
  fields, and it is validated when it is constructed rather than at the boundary.
- A manual `PriorityOverride` requires a reason, is recorded as `PRIORITY_CHANGED` with origin
  `MANUAL`, and is labelled as an override in the UI. It is never written into
  `PriorityScore.value` and never styled like a computed score.

## Consequences

Good:

- The answer to "why is this first" is a stored row, available months later, with no
  re-execution and no dependency on any external service being up or any model version still
  existing.
- Tuning is a data change with a version number. Two policy versions can be diffed, and the
  scores computed under each remain attributable to the policy that produced them.
- The engine is pure, so its tests need no database and no fixtures, and they assert the reason
  text rather than only the number — the reasons are product surface, not logging.
- Adding a signal is one class plus one registry line, with no branch added to a dispatcher.

Cost we accepted:

- The weights are a judgement, and nothing in the dataset validates them. There are no completed
  projects to correlate against, so 0.25 for `deadline_pressure` is defensible reasoning, not a
  fitted parameter. We surface the reasoning and let the operations lead retune; we do not claim
  the numbers are optimal.
- The engine reads only what is structured. 61 of 82 tasks carry a free-text `dependency` and
  blockers arrive as prose; the score sees a typed `Blocker` row and its age, not what the text
  says. A blocker whose description says "client signs Monday" scores identically to one that
  says "vendor has gone silent for a month".
- Deterministic means brittle at boundaries. A project one day past its target date jumps to
  `deadline_pressure` 1.0 with no easing, and the operations lead will occasionally disagree with
  a ranking that is arithmetically correct. `PriorityOverride` is the pressure valve, and heavy
  override use is the signal that the policy needs a new version.
- Six signals cannot express a genuinely novel situation. When one appears the fix is a code
  change and a deploy, where a model would have absorbed it — badly, and without saying so.

## Alternatives considered

- **LLM-driven ranking.** Rejected. Not reproducible, so the same portfolio can order
  differently on two consecutive mornings with no data change and no explanation. Not auditable,
  because a generated rationale is a plausible story about the output rather than the mechanism
  that produced it, and persisting it in `ActivityRecord` would make the audit trail actively
  misleading. It also puts a network call and a per-request cost on the path of the system's
  main view, and there is no ground truth in this dataset to evaluate it against.
- **LLM as a re-ranker over the deterministic top N.** Rejected for the same reason at smaller
  scale: the final order is what the operations lead defends, so the last step is exactly the
  step that must be explainable.
- **Learned weights from historical outcomes.** Rejected as impossible here, not as wrong. The
  dataset has no completed projects and no outcome labels. It is the natural successor once the
  system has been running long enough to have them, and `PriorityPolicy` versioning is the seam
  that would let learned weights ship as version N+1 without touching the engine.
- **A single sort key such as due date, or priority alone.** Rejected. It is explainable and
  useless: it ignores blockage, staleness and the absence of a next step, which is where this
  portfolio's actual risk sits — 5 projects have no target date at all and would sort into a
  meaningless position.
- **Hardcoded weights in Python.** Rejected. Retuning would need a deploy, and past scores would
  become unreproducible the moment the constant changed.
