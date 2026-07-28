<!-- SEED: established with the user before implementation; re-run /impeccable document once there's code to capture the actual tokens and components. -->
---
name: Aztec Ops
description: A flight control room for a project portfolio — every project answers GO or NO GO, and shows why.
---

# Design System: Aztec Ops

## Overview

**Creative North Star: "The Flight Control Room"**

A mission control room exists to turn many simultaneous, partially-broken situations into one
decision, out loud, on a schedule. A flight director polls each console position and each answers
one word. Behind the answer sits a wall of instruments that make the word defensible, and a
chronology on the side that records who said what and when. That is exactly the job of this
product, so the interface is built as a control room rather than as a dashboard.

The register is institutional, not nostalgic. This is not a retro tribute: no CRT curvature, no
scanline filters, no photoreal brushed metal. What carries over is the grammar — a fixed status
matrix where position means more than color, one-word verdicts, a countdown clock as the primary
expression of time, and a chronology that only ever grows. The surface is a console body: a
desaturated green-grey painted panel, with numeric readouts sunk into darker inset plates.

Density is the point. This screen is read for six minutes each morning by someone who already
knows the layout, so it optimizes for the second visit, not the first. Twenty-two rows fit
without scrolling on a laptop. Nothing is centered, nothing is padded to feel calm, and there
is no sidebar taking a fifth of the width to hold four links.

**Key Characteristics:**
- Full-bleed status matrix; fixed column positions across every row
- One-word verdict per project, with its reasoning on the same reading line
- Countdown clocks, not date strings
- Unlit states are drawn, never blank
- Square plates, hairline rules, zero border radius, zero drop shadows
- Color is a legal code with three meanings, never decoration

## Colors

A painted console body with instruments sunk into it: desaturated green-greys carry every
surface, and the only saturated color on screen is a state declaration.

### Primary
- **Console Green-Grey** (`#2E3532`): the panel body. The default ground of every surface, chosen
  over black so the screen survives a daylit office across a whole workday.
- **Plotboard Black** (`#141917`): inset plates holding numeric readouts — scores, countdown
  clocks, load figures. Recessed regions only; never a page background.

### Secondary
- **Signal Green** (`#63A17A`): GO. Nominal state, live figures, a transition that is legal.
- **Caution Amber** (`#D9A441`): attention required, but not today. Risk flags below the
  intervention threshold, a clock inside its final week, a stale project.

### Tertiary
- **No-Go Red** (`#C4483C`): a human must intervene today. Blocked, overdue, NO GO.

### Neutral
- **Legend Ivory** (`#E8E4D9`): primary text and engraved legends. Warm, never pure white, so
  long reading sessions do not glare.
- **Legend Dim** (`#8A928D`): secondary text, units, column headers, and — critically — the
  unlit state of every status legend.
- **Rule Grey** (`#414A46`): hairline dividers, plate edges, table rules.

### Named Rules

**The Reserved Red Rule.** No-Go Red appears only where a person must act today. If more than a
quarter of rows show red, the risk thresholds are miscalibrated — the fix is the threshold, never
the palette. Red is never used for a brand accent, a hover, or a heading.

**The Ghost Legend Rule.** A status legend that is not firing still renders, in Legend Dim, in its
fixed position. Empty cells are forbidden. The operator must be able to see what could be wrong,
not only what is wrong. This is the visual form of the product's principle that absence is a signal.

**The Three Meanings Rule.** Green, amber and red mean exactly one thing each and are never
borrowed for a fourth purpose. Any other distinction is made with position, weight or rule, not
with a new hue.

## Typography

**Display / Legend Font:** Archivo Narrow (with Arial Narrow, sans-serif)
**Body Font:** Archivo (with system-ui, sans-serif)
**Numeric / Mono Font:** JetBrains Mono (with ui-monospace, monospace)

**Character:** Archivo Narrow set in letterspaced caps reads as an engraved instrument plate —
compressed, functional, machine-cut, with no editorial warmth. Archivo carries the prose that a
condensed face would punish: summaries, blocker descriptions, timeline entries. JetBrains Mono
owns everything that must align in a column or tick: scores, countdown clocks, load figures,
timestamps.

### Hierarchy
- **Display** (JetBrains Mono, 600, 40–56px, 1.0): the score and the countdown clock. The only
  large type on the surface. Tabular figures always.
- **Headline** (Archivo Narrow, 600, 20px, 1.2, +0.06em, uppercase): board titles and section
  plates.
- **Title** (Archivo, 600, 15px, 1.3): project names and task titles.
- **Body** (Archivo, 400, 14px, 1.5, max 70ch): summaries, blocker descriptions, reasons,
  timeline entries.
- **Label** (Archivo Narrow, 600, 11px, 1.1, +0.1em, uppercase): every column header, status
  legend, verdict, and control label.
- **Figure** (JetBrains Mono, 500, 13px, tabular): in-table numerics, task counts, dates.

### Named Rules

**The Engraved Legend Rule.** Anything that names a state, a column, or a control is Archivo
Narrow in uppercase with +0.1em tracking. Anything a human wrote — a summary, a reason, a note —
is Archivo in sentence case. Mixing the two registers inside one string is the tell of a
carelessly built screen.

**The Tabular Figures Rule.** Every numeral in a repeated position uses `font-variant-numeric:
tabular-nums`. Columns of numbers that do not align are a defect, not a preference.

## Layout

A full-bleed status board with no sidebar. Horizontal space belongs to the data; navigation is a
single top plate, one row tall, holding the mission clock and the board switcher.

The board is a fixed column matrix, and column position is load-bearing: the same information sits
at the same x-coordinate on every row, so a practiced operator scans coordinates rather than
reading labels. Column order on the command center: code, project and client, owner position, the
status legend block (six fixed cells), the countdown clock, the score, the verdict.

Density target: 22 rows visible without scrolling at 1440×900. Row height 40px, with an expanded
state that pushes the score breakdown into the same row rather than a modal or a drawer — the
reasoning must be readable in the same reading line as the number it explains.

Spacing rhythm is a strict 4px base, used in 4 / 8 / 12 / 16 / 24 / 32 steps. More space above a
heading plate than below it. No section is separated by more than 32px; whitespace is not the
device this system uses to create hierarchy — rules and plate edges are.

Below 1024px the matrix drops its lower-priority columns in a fixed order (client, then owner,
then countdown) rather than reflowing into cards. Below 640px each project becomes a stacked plate
that preserves the legend block intact, because the legend block is the one thing that must never
change shape.

## Elevation & Depth

No drop shadows anywhere. Depth is tonal and inset: instruments are recessed into the console, not
floating above it. A numeric plate is Plotboard Black with a 1px Rule Grey border and a 1px
lighter top edge, which reads as a milled recess without a single blur.

**The Inset-Not-Raised Rule.** Surfaces recess; they never lift. Any `box-shadow` that casts
outward is foreign to this world. Focus and hover are expressed by a border or a background shift,
never by elevation.

## Shapes

Zero radius, everywhere, without exception. Every plate, button, cell, input and legend is a
square-cornered rectangle, because this world is made of cut and bolted panels.

Borders are 1px hairlines in Rule Grey. Emphasis is achieved by a 2px left edge in a state color
on the row itself — the mark a controller makes against a line — not by a heavier box.

**The Square Plate Rule.** A rounded corner anywhere in this interface means the component was
imported from another world and has not been rebuilt yet.

## Do's and Don'ts

### Do:
- **Do** render every status legend in its fixed position at all times, lit in a state color or
  unlit in Legend Dim (The Ghost Legend Rule).
- **Do** express time until a deadline as a countdown that crosses into overrun in the same
  format, and give a project with no deadline a named condition rather than an empty cell.
- **Do** keep the score and its reason breakdown on the same reading line; a number whose
  justification lives in a modal has failed this system's purpose.
- **Do** render the workflow's illegal transitions as visibly present and visibly dead, so the
  operator learns the shape of the workflow by looking at it.
- **Do** use `tabular-nums` on every numeral in a repeated position.
- **Do** snap state changes instantly. An annunciator lamp does not fade in.

### Don't:
- **Don't** use a border radius, a drop shadow, or a gradient.
- **Don't** introduce a fourth semantic color, or reuse green, amber or red for anything other
  than their single assigned meaning.
- **Don't** build a card grid, a donut chart, a sparkline strip, or a sidebar navigation. This
  system's answer to "show the portfolio" is a matrix.
- **Don't** simulate CRT scanlines, screen curvature, phosphor bloom, brushed metal, rivets, or
  any photoreal console texture. The grammar carries over; the nostalgia does not.
- **Don't** collapse dense content into cards on small screens at the cost of the legend block.
- **Don't** center body text or pad rows for calm. This screen is read fast by someone who has
  read it a hundred times.
