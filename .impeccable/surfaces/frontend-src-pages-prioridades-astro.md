---
version: 1
slug: "frontend-src-pages-prioridades-astro"
primary_target: "frontend/src/pages/prioridades.astro"
related_targets: []
---

Scope: /prioridades — the full ranked queue. Mode: Operate.
Audience/job: the defensible ordered list; answer "why is this first" without leaving the row.
Composition (approved comp B "Cola + contexto", 2026-07-28): master-detail. Left: ranked rows —
mono rank plate, project + client, owner avatar, risk dot strip (real criteria set from API, unlit =
hollow — Present-Absence), due chip, mono score. Selected row: 3px cobalto rail + cobalto wash.
Right: pinned context panel — score with thin arc, "Por qué está aquí" per-signal bars with the
persisted reasons, risk chips, próximo paso, actions (Cambiar estado / Ajustar prioridad). Override
always labelled manual with its reason, never masquerading as computed.
Responsive: <1024 the panel collapses and rows expand in place (comp A pattern) — same content, one
gesture away. <768 rows become stacked cards.
Unresolved: rank delta (▲2) needs a prior-rank source from the snapshot; if the API cannot provide
it, the delta chip is omitted — never fabricated.
