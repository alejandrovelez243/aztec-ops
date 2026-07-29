---
version: 1
slug: "frontend-src-pages-workflows-astro"
primary_target: "frontend/src/pages/workflows.astro"
related_targets: []
---

Scope: /workflows (route English, UI Spanish "Flujos de trabajo") — the lifecycles the operation
runs on. Mode: Operate + Author.
Audience/job: an ops lead shaping how work moves, and any member reading why a move was refused.
Header (rebuilt 2026-07-29): one row — title + one line of purpose on the left ("Decide por dónde
pasa el trabajo: los estados de cada flujo y los movimientos permitidos entre ellos."), the primary
action "Nuevo flujo" (btn-primary, plus icon) on the right, revealed in the browser for
`isOpsLead`. Nothing else. It replaced two grey paragraphs that restated each other and both sent
the reader to the Django admin to create a flow — false since authoring moved onto this screen
(CLAUDE.md rule 1, 2026-07-29): the admin is a second door, never the way in. How to read the
diagram is the legend's job, beside the drawing it explains, not a lead above the page.
Composition: one section per workflow — section head (name, applies-to, state/move counts, code
chip, predeterminado / inactivo / engagement-type chips), the SVG graph, the legend under it, then
the transition table carrying the same edges accessibly. Authoring controls sit with the object
they change — the flow in its own head, the state in the diagram and the table, the move in its
row — never as a second column of buttons; the page owns the header only.
Copy: Spanish that names the action ("Nuevo flujo", "Crea el primero…"), never the interface. The
empty state pairs the absence with the action that fills it, `quiet` scene.
Motion: none authored in the header — the primary action answers through the shared hover / press
(0.97, firme) / focus-ring inventory. The one authored moment on this surface stays the graph's.
Responsive: the header wraps and the action drops under the title on narrow screens, keeping its
40px height; the diagram scrolls inside its own scroller and the page never scrolls sideways —
every grid states `minmax(0, 1fr)` and every child `min-width: 0`.
Permissions: reading is any authenticated member; every write is ops-lead only and lands in the
activity trail. The reveal is presentation — the API's 403 (`details.required = "ops_lead"`,
API.md §2.19) is the answer of record.
View states: four, not five — configuration never arrives over the stream, so `disconnected` shares
the ready arm and there is no staleness marker to draw. Retry reloads the document; the skeletons
paint in the geometry of the answer.
