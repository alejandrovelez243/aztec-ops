---
version: 1
slug: "frontend-src-pages-priorities-astro"
primary_target: "frontend/src/pages/priorities.astro"
related_targets: []
---

Scope: /priorities (route English, UI Spanish "Prioridades") — the full ranked queue. Mode: Operate.
Audience/job: the defensible ordered list; answer "why is this first" without leaving the row.
Composition (approved comp B "Cola + contexto", 2026-07-28): master-detail. Left: ranked rows —
mono rank plate, project + client, owner avatar, risk dot strip (raised flags by severity, unlit =
hollow — Present-Absence), due chip, mono score. Selected row: cobalto rail + cobalto wash.
Right: pinned context panel — score with thin arc, "Por qué está aquí" per-signal bars with the
persisted reasons, risk chips, próximo paso, actions (Ver proyecto / Ajustar prioridad, the latter
ops-lead only). Override always labelled manual with its reason, never masquerading as computed.
Responsive: <1024 the panel collapses and rows expand in place (comp A pattern) — same content, one
gesture away. <768 rows become stacked cards.
Memorable moment: SSE recompute FLIP-reorders the rows and pops ▲/▼ deltas from client-side rank
memory; first paint shows no delta rather than a fabricated one.
