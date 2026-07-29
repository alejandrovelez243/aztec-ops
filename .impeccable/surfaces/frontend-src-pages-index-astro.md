---
version: 1
slug: "frontend-src-pages-index-astro"
primary_target: "frontend/src/pages/index.astro"
related_targets: []
---

Scope: / ("Resumen" in the UI) — landing surface of the shell. Mode: Operate.
Audience/job: operations lead, first screen of the morning; decide where the day goes in one scan.
Composition (approved comp C "Zonas del día", 2026-07-28): tiered density zones instead of a KPI band —
"Atender hoy" (top of queue as large cards: priority-wash header from taxonomy color, owner avatar,
state chip, due chip, big mono score, mini breakdown bars, Ver proyecto), "Esta semana" (medium
two-column cards), then Resumen-specific regions: owner load (team teaser) and latest activity.
Counts live in zone labels ("Atender hoy · 3"), not in stat tiles — the hero-metric template is refused.
Memorable moment: an SSE update visibly moves/flashes a card between zones with the carta spring.
Constraints: zone thresholds come from queue data (rank + risk severity), never hardcoded state codes.
Unresolved: latest-activity region needs the global activity endpoint (backend addition); until it
exists the region ships as a designed pending state, not silently omitted.
