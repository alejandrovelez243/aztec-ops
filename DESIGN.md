---
name: Aztec Ops
description: The operations wall rebuilt as software — white cards with real weight and spring physics on a warm paper surface, one cobalt accent, priority read as color before it is read as text.
---

# Design System: Aztec Ops

## Overview

**Creative North Star: "El Muro" — the operations wall, rebuilt as software**

Before software, an operations lead ran the week on a wall: index cards, magnets, colored tape
zones, a marker. That wall had three properties this product inherits wholesale. Every item was
a physical object you could pick up — moving work felt like moving matter, and when a colleague
moved a card you saw the hand do it. Position and color carried meaning before any text was
read. And the wall itself was calm — paper, daylight, one wall color — so the drama came from
the work, never from the surface.

The register is a modern, light, friendly product surface with the wall's physics grafted in:
white cards with true elevation on a warm paper ground, one confident cobalt accent, and a
motion system in which cards have mass — they lift, tilt, glide and settle as if a hand moved
them. Live updates over SSE are the signature: when someone else moves a card, the card visibly
travels across your screen with an attribution chip riding on it. The product's multiplayer
nature is shown, never implied.

Density serves comprehension, not compression. The old one-glance matrix is replaced by
navigable surfaces — an overview to land on, a ranked queue to decide with, a board to move
work on, a detail to act in — with explicit wayfinding: sidebar, breadcrumbs, back. A tool
people want to open every morning never makes them feel lost.

**Key characteristics:**
- Cards as physical objects: real elevation, spring motion, drag with weight and tilt
- Priority legible as color before text — washed card headers and rails, driven by taxonomy data
- One brand accent (Cobalto) that never means a state; states keep their own semantic set
- Warm paper ground, white surfaces, generous radius, soft layered shadows
- Persistent shell: collapsible sidebar, topbar with breadcrumbs and back
- Every action answers: hover, press, in-flight, toast, live flash
- Desktop-first and fully responsive; drag always has a keyboard/menu twin

## Colors

A daylit room: warm paper walls, white paper cards, ink text. Exactly one brand accent, and a
semantic set that belongs to the data.

### Ground & surfaces
- **Papel** `#F4F3EF` — page ground. Warm, never lab-grey.
- **Carta** `#FFFFFF` — cards and raised surfaces.
- **Pozo** `#ECEAE4` — sunken wells: board column bodies, input fills, code chips.
- **Hairline** `#E4E2DB` — 1px borders and dividers.

### Ink
- **Tinta** `#1B1D22` — primary text.
- **Tinta media** `#565B64` — secondary text, descriptions, meta.
- **Tinta suave** `#8A8F99` — tertiary: hints, placeholders, disabled.

### Brand
- **Cobalto** `#2545DE` — the interface's own voice: primary actions, active navigation, links,
  focus rings, selection, drop targets. Hover **Cobalto profundo** `#1B33AE`; background wash
  **Cobalto papel** `#EAEDFC`.

### Semantic set (data-facing)
Each semantic color ships in three tones — **ink** (text-grade, AA on its wash), **solid**
(dots, rails, small marks), **wash** (chip and band backgrounds). Solids listed; exact ink/wash
tones are established by the build under contrast checks.

- **Rojo** `#D8433B` (wash `#FBEAE8`) — urgent priority, blocked, overdue, critical risk.
- **Ámbar** `#DE8F13` (wash `#FBF2DF`) — high priority, warning-level risk, stale, named absence.
- **Verde** `#2E9E5B` (wash `#E7F5EC`) — low priority, done category, healthy, resolved.
- **Cielo** `#3B87DC` (wash `#E9F1FB`) — medium priority, in-progress category, informational.
- **Piedra** `#6E747E` (wash `#EFEFEB`) — todo category, neutral, not-yet-measured.

### Named rules

**The One-Voice Rule.** Cobalto is the interface speaking — act here, you are here, this is
selected. It is never a state, a priority, or a chart category. Cobalto on a status chip means
the chip is wrong.

**The Data-Owns-Color Rule.** Priority, state and risk colors render from what the API and
taxonomies return, mapped onto the semantic set. Code never hardcodes "urgent is red"; fixtures
do. A new state arriving from the admin renders correctly with zero frontend changes.

**The Wash Rule.** Semantic color covers area (card headers, zone bands, chips) only as a wash
with same-hue ink text. Solid semantic fills are reserved for small marks: dots, rails, badges.
A screen of solid red cards is an alarm, not a tool.

**The Present-Absence Rule.** Missing data — no due date, no next step, no recent activity —
renders as an Ámbar-washed chip naming the absence ("sin fecha", "sin próximo paso"), never as
an empty cell. The old world's Ghost Legend, translated: absence stays a first-class signal.

## Typography

**UI / Display:** Schibsted Grotesk (fallback system-ui, sans-serif)
**Numeric / Code:** Spline Sans Mono (fallback ui-monospace, monospace)

Schibsted Grotesk was commissioned for a news group's morning products: compact, high-clarity,
slightly sharp — pragmatic warmth rather than startup-neutral. One family carries display
figures through body copy, so the surface speaks with one voice. Spline Sans Mono owns whatever
must align or count: scores, ranks, codes, dates, deltas.

### Hierarchy
- **Display** — Schibsted 700, 40–48px / 1.05, −0.02em: KPI figures, the score dial.
- **H1** — Schibsted 700, 26–30px / 1.15: page titles.
- **H2** — Schibsted 600, 19px / 1.25: section and card-group titles.
- **Title** — Schibsted 600, 15.5px / 1.35: card titles, project names.
- **Body** — Schibsted 400, 14.5px / 1.55, max 68ch: descriptions, reasons, notes.
- **Small** — Schibsted 400–500, 13px / 1.45: meta lines, timestamps.
- **Label** — Schibsted 600, 11.5px / 1.2, +0.05em, uppercase: column headers, chips, nav groups.
- **Figure** — Spline Sans Mono 500, 13px, tabular: table numerics, codes, deltas.

### Named rules

**The Tabular Figures Rule** (kept from the old world). Every numeral in a repeated position
sets `font-variant-numeric: tabular-nums`. Misaligned number columns are a defect.

**The One-Voice, Two-Hands Rule.** Schibsted speaks for the interface and its humans; the mono
appears only where alignment or count is the point — scores, ranks, codes, timestamps. Mono
prose is forbidden, and so is grotesk where a column of figures must align.

## Layout

Persistent shell. Sidebar 264px — Carta surface, hairline edge — collapsible to a 72px icon
rail with tooltips; below 1024px it becomes an overlay drawer. Topbar 64px: back button and
breadcrumbs on the left; live-connection indicator, search and the operator's avatar menu on
the right. Content region on Papel, max-width 1360px, 24–32px gutters, on an 8px spacing base
(4px permitted inside chips).

Surfaces. Routes are English (CLAUDE.md §Language); every label on them is Spanish.

- **/login** — the door: credentials form on the wall motif. The guard redirects here without a session.
- **/** ("Resumen") — the day in tiers: what to attend today, this week, the radar; owner load and
  latest activity beside them.
- **/priorities** ("Prioridades") — the ranked queue: rank plate, project, owner, risk strip, due
  chip, score, with the breakdown one gesture away, never a page away. The product's heart.
- **/projects** ("Proyectos") — card grid ↔ table toggle, filters as a pill bar.
- **/projects/{code}** — detail: header card with state and legal transitions, score with
  breakdown, tabs (tareas / bloqueos / notas / actividad).
- **/board** ("Tablero") — board by workflow state; columns are data, one engagement type at a time.
- **/team** ("Equipo") — load per person, bars plus task lists.
- **/activity** ("Actividad") — portfolio-wide feed.

Responsive: ≥1440 full; 1024–1439 sidebar rail; 768–1023 drawer, tables shed tertiary columns;
<768 rows become stacked cards, the board becomes horizontally snap-scrolled columns, detail
tabs stack. Hit targets ≥40px on touch. Nothing disappears without a way back in: every dropped
column reappears inside the card form.

## Brand & illustration

**The mark** is three cards climbing the wall (`public/brand/mark.svg`): equal rounded squares
ascending left to right, the bottom in Cobalto papel, the middle in a mid tint, the top solid
Cobalto — the portfolio ordered, with the top one decided. The favicon drops to two cards,
because at 16px a third collapses into a smudge. The wordmark is not an asset: "Aztec Ops" is
set live in Schibsted Grotesk 700 beside the mark, so it inherits the type system instead of
freezing a traced outline of it.

**Illustrations** are inline SVG components (`components/ui/Illustration.astro`), never files in
`public/`. Three reasons, all load-bearing: their strokes read design tokens, so an illustration
cannot drift from the palette; their pieces carry handles the motion system animates; and each
scene costs a line of markup instead of the tens of kilobytes of traced béziers a generated file
spends on the same four rectangles. Generation art-directs the composition; the shipped asset is
authored.

The grammar is the world's own: hairline card outlines on the wall, one real paper card, exactly
one accent per scene. Four scenes exist — `clear` (nothing pending), `quiet` (an empty timeline),
`nothing-found` (a search that returned blank cards), `lost` (a wall with one card knocked out).
Every empty state pairs one with a sentence naming the absence and the action that fills it
(`components/ui/EmptyState.astro`), and its pieces settle in sequence on the `carta` spring — the
invisible hand placing them.

**No stock photography, no 3D renders, no gradient meshes.** This product's imagery is the same
paper and ink its interface is made of.

## Elevation & Depth

Cards are paper above the wall: real, soft, layered shadows, with hairline edges keeping them
crisp on a light ground.

- **Resting** — `0 1px 2px rgba(27,29,34,.05), 0 4px 12px rgba(27,29,34,.06)` + hairline.
- **Raised** (hover, open menus) — `0 2px 6px rgba(27,29,34,.07), 0 12px 28px rgba(27,29,34,.10)`.
- **Lifted** (dragged card, dialogs) — `0 8px 18px rgba(27,29,34,.12), 0 32px 64px rgba(27,29,34,.16)`.

**The Physical Shadow Rule.** Shadow means exactly one thing: distance from the wall, and
therefore closeness to your hand. Resting → raised → lifted tracks interaction. A decorative
shadow on something that cannot be touched is foreign here.

## Shapes

- Cards and panels 16px radius; controls and inputs 10px; small chips 8px; pills and avatars full.
- Hairline borders 1px. Selection and drop targets 1.5–2px Cobalto; drop slots dash.
- A 3px rounded left rail on a row or card may carry a semantic color — the marker stripe on
  the wall card. Rails mark, washes cover, solids dot: three sizes of semantic presence.

## Motion — "La mano invisible"

The wall's physics, implemented. Objects have mass; the interface never teleports. FLIP powers
every reorder; springs power every settle; the Web Animations API and Astro view transitions
carry both.

### Springs
- **firme** — stiffness 420, damping 34 (~200ms settle): chips, buttons, small elements.
- **carta** — stiffness 320, damping 26, slight overshoot (~450ms): card lift, travel, drop.
- **panel** — stiffness 240, damping 30 (~500ms): drawers, accordions, page-level shifts.

### The card lifecycle (drag)
1. **Grab** — cursor closes; the card scales to 1.03, tilts up to ±3° toward pointer velocity,
   shadow goes lifted; the board announces legality.
2. **Carry** — the card follows with spring lag (it trails the hand slightly: mass). Legal
   columns open a Cobalto dashed drop slot that grows to card height; illegal columns dim to
   55% and show a lock chip naming why ("sin transición desde En progreso").
3. **Drop** — one overshoot, then settle; neighbors make room via FLIP on `carta`; the state
   chip snaps to its new color; a toast confirms; undo appears when the reverse transition is legal.
4. **Reject** — an illegal drop or API error springs the card back along its path with one
   ±4px shake; the toast names the domain rule that refused it.

### The remote move (SSE) — signature moment
When someone else changes state or rank: the affected card pulses a Cobalto ring, lifts,
travels to its new position along a slight arc on `carta`, and settles; a small attribution
chip ("Valentina → En progreso") rides with it and fades. In a table view the same event is a
FLIP reorder plus a wash flash on the moved row. The tool is visibly multiplayer.

### Feedback inventory — every interactive element answers
- **Hover:** raise (cards) or wash (rows, nav) within 120ms.
- **Press:** scale 0.97 on `firme`.
- **Focus:** 2px Cobalto ring, 2px offset, always visible for keyboard users.
- **In-flight:** the pressed control hosts an inline spinner and disables; surfaces never
  freeze silently.
- **Result:** toast bottom-right, springs in; failures state the domain rule in words.
- **Numbers:** count up/down over 300ms in mono; deltas pop a ▲/▼ chip on `firme`.
- **Loading:** skeletons in the exact geometry of the loaded state, shimmering. Whole-page
  spinners are forbidden.
- **Navigation:** view transitions — a project card morphs into its detail header (shared
  element); the rest crossfades. Back always reverses the morph.

### Reduced motion
Under `prefers-reduced-motion`: travel and morphs become 120ms crossfades, count-ups render
final values, springs collapse to opacity — color and toast feedback stay whole.

## Do's and Don'ts

### Do
- **Do** give every state change a visible cause and a visible effect: motion + toast + a line
  in the activity trail.
- **Do** render taxonomy colors from data through the semantic set (Data-Owns-Color).
- **Do** keep a score and its breakdown one gesture apart — expand in place, never a page away.
- **Do** name absences with chips: "sin fecha", "sin próximo paso" (Present-Absence Rule).
- **Do** keep a back path from every screen; breadcrumbs on every level below the top.
- **Do** ship the keyboard/touch twin of every drag: a "Mover a…" menu on the card.
- **Do** keep `tabular-nums` on every repeated numeral.

### Don't
- **Don't** use Cobalto for a state, a priority, or a chart series (One-Voice Rule).
- **Don't** fill areas with solid semantic color; wash + same-hue ink (Wash Rule).
- **Don't** teleport anything the user can see move, and don't animate what carries no meaning.
- **Don't** build modal walls: expansions happen in place; modals are for confirmation and
  destructive intent only.
- **Don't** reintroduce the control-room grammar: no zero-radius plates, no engraved-caps
  matrices, no countdown-clock typography as decoration, no dark console surfaces.
- **Don't** leave an action unanswered: silent success is a bug, silent failure is a defect.
