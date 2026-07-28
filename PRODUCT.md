# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary user: an operations lead at a services company running many client engagements at
once. Confirmed situation: they open the system first thing in the morning, facing 22 active
projects and 82 open tasks spread across 5 people, and must decide within a few minutes what
the day is spent on. They are not browsing — they are triaging.

Secondary, non-driving audience: the reviewers of this technical challenge, who will see the
system in a five-minute screen recording. The interface is optimized for the operations lead;
the reviewer's value comes from seeing a tool that is genuinely operable, not a demo.

## Product Purpose

Turn a scattered portfolio into a defensible daily decision. The system answers three
questions every morning:

1. What should be worked on today, and why exactly that?
2. What is at risk, blocked, or has no clear next step?
3. Who is overloaded?

Success is the operations lead closing the tab with an ordered list they can justify to
someone else, and knowing which blockers need a human intervention today.

## Positioning

The ranking is computed, deterministic and explainable. Every project carries a 0–100 score
with a per-signal reason breakdown that is persisted alongside it, so "why is this first" is
answered by the record, not by an opinion. A manual override is possible but is always labelled
as an override with a mandatory reason — it never masquerades as a computed score.

The system also treats absence as data: a project with no next step, no due date, or no
activity is surfaced as a risk rather than sitting quietly at the bottom of a list.

## Operating Context

- Daily morning triage, on a desktop screen. Sessions are short and repeated.
- Work is classified into three engagement types that behave differently: Proyecto,
  Mantenimiento o recurrente, and Diagnostico.
- State changes follow configurable workflows, one per engagement type — a project cannot jump
  to an arbitrary state, only along legal transitions.
- Every state change, reprioritization, blocker raised or resolved, and owner change is written
  to an append-only activity record. Deprioritizing one project to prioritize another is stored
  as a single correlated decision.
- Data updates arrive live over SSE; the screen is expected to be left open while others work.

## Capabilities and Constraints

- Create and update projects and tasks; store owner, state, priority, due date, next step,
  blockers and notes.
- Detect projects at risk, blocked, or without a clear next step, through composable rules.
- Prioritized queue with an explainable score, plus manual override with a reason.
- Configurable taxonomies and workflows, editable from the Django admin without a deploy.
- Load per person, computed from tasks rather than stored.
- Deliberately out of scope for this version: real authentication and multi-tenancy, external
  notifications, task drag & drop, historical burndown metrics.
- Terminology is Spanish in the interface, because the source data is Spanish (Bloqueada, En
  progreso, Diagnostico, Mantenimiento o recurrente). Code, identifiers and documentation are
  English. UI language is Spanish; this is confirmed, not an open decision.

## Brand Commitments

The product carries its own identity as an internal operations tool named **Aztec Ops**. It
deliberately does not imitate the real Aztec corporate brand — no corporate colors, logo or
typography are available or assumed, and none may be invented.

## Evidence on Hand

- Real source dataset at `data/raw/dataset.json` (normalized from the provided spreadsheet):
  22 projects, 82 tasks, 5 team members, 16 clients. Client, project and person names are
  aliases created by the challenge authors.
- Measured properties that the interface must handle honestly: every project has status
  `Activo`; there is not a single completed task, so the data is pure open backlog; 5 of 22
  projects have no target date; 61 of 82 tasks carry a free-text dependency.
- No logo, no photography, no customer testimonials, no pricing, no press. None of these may be
  fabricated.

## Product Principles

1. **A number without a reason is noise.** Any score, ranking or health indicator is shown with
   the reasoning that produced it, close enough to read in the same glance.
2. **Absence is a signal.** Missing due dates, missing next steps and silence are surfaced, not
   hidden by empty cells.
3. **Decisions leave a trace.** Anything that changes what the team does is recorded with actor
   and reason, and is readable later as a timeline.
4. **The operation can change without a deploy.** New states, transitions and taxonomies are
   data, so the interface must render whatever the database defines rather than a fixed set.
5. **Triage over browsing.** The default view is ordered by what needs a decision today, not
   alphabetically or by recency.

## Accessibility & Inclusion

No product-specific standard was established. Baseline expectation: the interface is used daily
on a desktop screen for extended sessions, so contrast, hit targets and keyboard reachability of
the primary triage actions must hold up under repeated use.
