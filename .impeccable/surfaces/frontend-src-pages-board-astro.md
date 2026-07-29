---
version: 1
slug: "frontend-src-pages-board-astro"
primary_target: "frontend/src/pages/board.astro"
related_targets: []
---

Scope: /board (UI "Tablero") — where one project is worked, not where the portfolio is browsed. Mode: Operate.
Audience/job: move real work along; see a project's tasks and change their state by hand.

Composition (user correction, 2026-07-28 — the first build had the two halves the wrong way round):
- LEFT RAIL (~340-380px): the PROJECTS, stacked in sections by workflow state, dragged VERTICALLY
  between sections. The rail is the index.
- MAIN BOARD: the TASKS of the selected project, one column per task state, dragged HORIZONTALLY.
  The board is the table you work on.
- Toolbar: engagement type as a SELECT field (not pills), plus the MIEMBROS avatar bar that filters
  by owner. Avatars are photo-when-the-API-has-one, initials otherwise; the dataset has no photos,
  and the branch is written so a future field changes one line.
Sections and columns come from GET /api/v1/workflows (added for this surface): the states a workflow
owns, in the operator's order, so an empty state still renders as a drop target. That endpoint
publishes no transitions — legality stays on the per-project/per-task `transitions` list.
Memorable moment: the card physics on both axes, and a colleague's move arriving as visible travel
with an attribution chip.
Constraints: no hardcoded state code or colour; every drag has a keyboard/menu twin; nothing is
painted before the server confirms.
Unresolved: task-level legal moves are not exposed by a list endpoint the way a project's are; the
board relies on the server refusing an illegal task move.
