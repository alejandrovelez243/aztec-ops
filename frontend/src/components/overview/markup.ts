/**
 * The single renderer of the Resumen surface.
 *
 * One module produces the markup for both callers: the page frontmatter (first
 * paint, through `set:html`) and the live island (after a client read or an SSE
 * reshuffle). Two renderers for one surface is how a server render and a live
 * render start disagreeing about a card, which is the specific failure
 * `docs/standards/PATTERNS_FRONTEND.md` §6 exists to prevent.
 *
 * Because these are strings, Astro's scoped styles cannot reach them; the
 * surface's stylesheet is `./overview.css`, imported once by the page.
 *
 * Security invariant: **every** interpolated value passes through
 * {@link escapeHtml}, and the one value that lands in a `style` attribute — a
 * taxonomy colour an administrator typed — additionally has to match
 * {@link HEX_COLOR} or it is dropped. A colour that does not parse falls back
 * to the neutral tone instead of being trusted into a CSS declaration.
 *
 * Density invariant: every card in every zone renders the **same** DOM. The
 * zone's class decides how much of it is visible, which is what lets a card
 * travel between zones with `flip()` instead of being destroyed and rebuilt —
 * the signature moment of this surface.
 */

import { formatScore } from "../../lib/format/score";
import type { QueuePage, TeamLoad, TeamLoadEntry } from "../../lib/api/domain";
import { avatarHue, initials } from "../../lib/auth/session";
import { assertNever, type ViewState } from "../../lib/view-state";
import {
  toRows,
  zoneOfRank,
  type BreakdownBar,
  type OverviewRow,
  type OwnerBadge,
  type ZoneKey,
} from "./model";
import {
  blockersText,
  CARD_COPY,
  dueText,
  dueTone,
  failureText,
  openTasksText,
  ownedProjectsText,
  QUEUE_COPY,
  TEAM_COPY,
  ZONES,
} from "./copy";

/** The only colour shape an API taxonomy value may inject into a style attribute. */
const HEX_COLOR = /^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

/** Skeleton counts per zone: the loading state has the geometry it will become. */
const SKELETON_ROWS: Readonly<Record<ZoneKey, number>> = {
  today: 3,
  week: 4,
  radar: 5,
};

/** Utilization bars stop growing at the track's width; the figure still tells the truth. */
const FULL_BAR_PERCENT = 100;

/**
 * Escapes a value for both element text and double-quoted attribute values.
 *
 * Applied without exception, including to values that "cannot" contain markup:
 * project names, client aliases and override reasons are operator-written text,
 * and the day one of them contains a `<` is not the day to discover that this
 * renderer trusted it.
 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Renders the whole queue region — all five arms of the view state.
 *
 * Called from frontmatter for the first paint and from the island after a
 * client-side read, so `loading`, `empty`, `error`, `ready` and `disconnected`
 * all have real, styled markup on both paths (`ARCHITECTURE.md` §9).
 *
 * `disconnected` renders exactly the rows `ready` renders — stale content stays
 * on screen — and marks the region; the staleness banner and its reconnect
 * control belong to the island, which is the only thing that knows when the
 * stream dropped.
 */
export function renderQueueRegion(
  state: ViewState<QueuePage>,
  now: Date,
): string {
  switch (state.kind) {
    case "loading":
      return renderQueueSkeleton();
    case "empty":
      return renderNote({
        title: QUEUE_COPY.emptyTitle,
        body: QUEUE_COPY.emptyBody,
        tone: "tone-cielo",
        action: {
          kind: "link",
          label: QUEUE_COPY.emptyAction,
          href: "/projects",
        },
      });
    case "error":
      return renderNote({
        title: QUEUE_COPY.errorTitle,
        body: failureText(state.code),
        tone: "tone-rojo",
        action: {
          kind: "retry",
          label: QUEUE_COPY.errorAction,
          region: "queue",
        },
      });
    case "ready":
    case "disconnected":
      return renderZones(toRows(state.data.items, now));
    default:
      return assertNever(state);
  }
}

/**
 * Renders the owner-load teaser's body — all five arms.
 *
 * The teaser deliberately shows the whole roster rather than a "top N": a person
 * at 40% is the answer to "who can take this", and cutting the list to the
 * overloaded few would hide exactly that.
 */
export function renderTeamRegion(state: ViewState<TeamLoad>): string {
  switch (state.kind) {
    case "loading":
      return renderTeamSkeleton();
    case "empty":
      return renderNote({
        title: TEAM_COPY.emptyTitle,
        body: TEAM_COPY.emptyBody,
        tone: "tone-cielo",
        action: { kind: "none" },
      });
    case "error":
      return renderNote({
        title: TEAM_COPY.errorTitle,
        body: failureText(state.code),
        tone: "tone-rojo",
        action: { kind: "retry", label: TEAM_COPY.errorAction, region: "team" },
      });
    case "ready":
    case "disconnected":
      return renderOwners(state.data.items);
    default:
      return assertNever(state);
  }
}

/** The three zone sections, in reading order, with continuous rank numbering. */
export function renderZones(rows: readonly OverviewRow[]): string {
  return ZONES.map((zone) => {
    const inZone = rows.filter((row) => zoneOfRank(row.rank) === zone.key);
    return renderZoneSection(zone.key, zone.label, zone.hint, inZone.length, {
      body: inZone.map(renderCard).join(""),
      isEmpty: inZone.length === 0,
    });
  }).join("");
}

/** The roster rows of the owner-load teaser. */
export function renderOwners(entries: readonly TeamLoadEntry[]): string {
  return `<ul class="ov-owners">${entries.map(renderOwnerRow).join("")}</ul>`;
}

interface ZoneBody {
  readonly body: string;
  readonly isEmpty: boolean;
}

function renderZoneSection(
  key: ZoneKey,
  label: string,
  hint: string,
  count: number,
  content: ZoneBody,
): string {
  const hidden = content.isEmpty ? " hidden" : "";
  return `<section class="ov-zone ov-zone--${key}" data-ov-zone="${key}"${hidden}>
  <header class="ov-zone-head">
    <h2 class="label ov-zone-label">${escapeHtml(label)}<span class="ov-zone-sep" aria-hidden="true">·</span><span class="figure ov-zone-count" data-ov-zone-count>${count}</span></h2>
    <p class="small ov-zone-hint">${escapeHtml(hint)}</p>
  </header>
  <ul class="ov-zone-list" data-ov-zone-list="${key}">${content.body}</ul>
</section>`;
}

/**
 * One project card: identical DOM in every zone, so a rank change is a move and
 * never a rebuild.
 *
 * The `data-ov-*` attributes are the island's contract with this markup — the
 * rendered `updated_at` an envelope is compared against, the score a reorder
 * sorts on, and the state label/colour dictionary a `project.state_changed`
 * patch reads to find the Spanish label its payload does not carry.
 */
function renderCard(row: OverviewRow): string {
  const tone = toneAttributes(row.state.color);
  const overrideAttrs =
    row.overrideReason === null
      ? ""
      : ` title="${escapeHtml(row.overrideReason)}"`;
  return `<li class="ov-card card card-hover ${tone.className}" ${tone.styleAttr}
  data-ov-card
  data-ov-code="${escapeHtml(row.code)}"
  data-ov-rank="${row.rank}"
  data-ov-score="${row.score}"
  data-ov-updated-at="${escapeHtml(row.updatedAt)}"
  data-ov-state-code="${escapeHtml(row.state.code)}"
  data-ov-state-label="${escapeHtml(row.state.label)}"
  data-ov-state-color="${escapeHtml(row.state.color ?? "")}">
  <div class="ov-band">
    <span class="figure ov-rank" data-ov-rank-text>${rankText(row.rank)}</span>
    <span class="chip ov-state" data-ov-state-chip>${escapeHtml(row.state.label)}</span>
    ${row.overrideReason === null ? "" : `<span class="chip tone-ambar ov-override"${overrideAttrs}>${CARD_COPY.manualOverride}</span>`}
    <span class="chip ov-attribution" data-ov-attribution role="status" hidden></span>
  </div>
  <div class="ov-main">
    <div class="ov-ident">
      <h3 class="title ov-name" style="view-transition-name:${viewTransitionName(row.code)}">${escapeHtml(row.name)}</h3>
      <p class="small ov-sub"><span class="chip chip-code">${escapeHtml(row.code)}</span><span class="ov-client">${escapeHtml(row.clientAlias)}</span></p>
    </div>
    <p class="ov-score">
      <span class="display ov-score-value" data-ov-score-text>${escapeHtml(formatScore(row.score))}</span>
      <span class="label ov-score-caption">${CARD_COPY.scoreCaption}</span>
    </p>
  </div>
  ${renderBars(row.bars)}
  <div class="ov-foot">
    ${renderOwnerBadge(row.owner)}
    <span class="chip ${dueTone(row.due)}">${escapeHtml(dueText(row.due))}</span>
    ${row.hasNextStep ? "" : `<span class="chip tone-ambar">${CARD_COPY.noNextStep}</span>`}
    ${row.openBlockers > 0 ? `<span class="chip tone-rojo">${escapeHtml(blockersText(row.openBlockers))}</span>` : ""}
    ${row.overrideReason === null ? "" : `<span class="small ov-override-reason">“${escapeHtml(row.overrideReason)}”</span>`}
    <a class="btn btn-secondary ov-open" href="/projects/${encodeURIComponent(row.code)}">${CARD_COPY.open}</a>
  </div>
</li>`;
}

/**
 * The mini breakdown bars.
 *
 * A score with no argument behind it is not rendered as a bare number: an empty
 * breakdown names its own absence with an ámbar chip (Present-Absence Rule)
 * rather than leaving the figure unexplained.
 */
export function renderBars(bars: readonly BreakdownBar[]): string {
  if (bars.length === 0) {
    return `<ul class="ov-bars" data-ov-bars><li class="ov-bars-absent"><span class="chip tone-ambar">${CARD_COPY.noBreakdown}</span></li></ul>`;
  }
  return `<ul class="ov-bars" data-ov-bars>${bars.map(renderBar).join("")}</ul>`;
}

function renderBar(bar: BreakdownBar): string {
  const width = Math.round(bar.share * FULL_BAR_PERCENT);
  return `<li class="ov-bar">
  <span class="label ov-bar-label">${escapeHtml(bar.label)}</span>
  <span class="ov-bar-track" aria-hidden="true"><span class="ov-bar-fill" style="width:${width}%"></span></span>
  <span class="figure ov-bar-value">${escapeHtml(formatScore(bar.contribution))}</span>
</li>`;
}

function renderOwnerBadge(owner: OwnerBadge | null): string {
  if (owner === null) {
    return `<span class="chip tone-ambar">${CARD_COPY.unassigned}</span>`;
  }
  return `<span class="ov-owner-badge"><span class="avatar" style="--avatar-h:${owner.hue}" aria-hidden="true">${escapeHtml(owner.initials)}</span><span class="small ov-owner-name">${escapeHtml(owner.label)}</span></span>`;
}

function renderOwnerRow(entry: TeamLoadEntry): string {
  const percent = Math.round(entry.utilization * FULL_BAR_PERCENT);
  const width = Math.min(FULL_BAR_PERCENT, Math.max(0, percent));
  const tone = entry.is_overloaded ? "tone-ambar is-overloaded" : "tone-cielo";
  return `<li class="ov-owner ${tone}" data-ov-owner="${escapeHtml(entry.alias)}">
  <span class="avatar" style="--avatar-h:${avatarHue(entry.alias)}" aria-hidden="true">${escapeHtml(initials(entry.label))}</span>
  <span class="ov-owner-ident">
    <span class="title ov-owner-label">${escapeHtml(entry.label)}</span>
    <span class="small ov-owner-meta">${escapeHtml(openTasksText(entry.open_tasks))} · ${escapeHtml(ownedProjectsText(entry.projects_owned))}</span>
  </span>
  <span class="ov-owner-load">
    <span class="ov-util" aria-hidden="true"><span class="ov-util-fill" style="width:${width}%"></span></span>
    <span class="figure ov-owner-percent">${percent}%</span>
  </span>
  ${entry.is_overloaded ? `<span class="chip tone-ambar ov-owner-flag">${TEAM_COPY.overloaded}</span>` : ""}
</li>`;
}

/** Skeletons in the exact geometry of the loaded zones — never a page spinner. */
function renderQueueSkeleton(): string {
  return ZONES.map((zone) => {
    const cards = Array.from({ length: SKELETON_ROWS[zone.key] }, () =>
      renderCardSkeleton(),
    ).join("");
    return `<section class="ov-zone ov-zone--${zone.key}" data-ov-zone="${zone.key}" aria-busy="true" aria-label="${escapeHtml(QUEUE_COPY.loadingLabel)}">
  <header class="ov-zone-head">
    <h2 class="label ov-zone-label">${escapeHtml(zone.label)}</h2>
  </header>
  <ul class="ov-zone-list" data-ov-zone-list="${zone.key}">${cards}</ul>
</section>`;
  }).join("");
}

function renderCardSkeleton(): string {
  return `<li class="ov-card ov-card--skeleton card tone-piedra" aria-hidden="true">
  <div class="ov-band"><span class="skeleton ov-sk-rank"></span><span class="skeleton ov-sk-chip"></span></div>
  <div class="ov-main">
    <div class="ov-ident"><span class="skeleton ov-sk-name"></span><span class="skeleton ov-sk-sub"></span></div>
    <p class="ov-score"><span class="skeleton ov-sk-score"></span></p>
  </div>
  <ul class="ov-bars"><li class="ov-bar"><span class="skeleton ov-sk-bar"></span></li><li class="ov-bar"><span class="skeleton ov-sk-bar"></span></li><li class="ov-bar"><span class="skeleton ov-sk-bar"></span></li></ul>
  <div class="ov-foot"><span class="skeleton ov-sk-chip"></span><span class="skeleton ov-sk-chip"></span></div>
</li>`;
}

function renderTeamSkeleton(): string {
  const rows = Array.from(
    { length: 4 },
    () =>
      `<li class="ov-owner ov-owner--skeleton" aria-hidden="true"><span class="skeleton ov-sk-avatar"></span><span class="skeleton ov-sk-name"></span><span class="skeleton ov-sk-bar"></span></li>`,
  ).join("");
  return `<ul class="ov-owners" aria-busy="true" aria-label="${escapeHtml(TEAM_COPY.loadingLabel)}">${rows}</ul>`;
}

/** What a note offers the operator once it has explained itself. */
type NoteAction =
  | { kind: "none" }
  | { kind: "link"; label: string; href: string }
  | { kind: "retry"; label: string; region: RegionKey };

/** The two regions the island can re-read on its own. */
export type RegionKey = "queue" | "team";

interface NoteOptions {
  readonly title: string;
  readonly body: string;
  /** Tone class of the leading dot; rojo for a failure, cielo for an absence. */
  readonly tone: string;
  readonly action: NoteAction;
}

/**
 * The empty and error arms, as one designed card rather than a bare sentence.
 *
 * An empty state names what is empty *and* the action that fills it; an error
 * state names what failed *and* offers a control that re-runs the same read.
 * Both are markup with the surface's own styling, never an unstyled fallback
 * string (`docs/standards/FRONTEND.md` §7).
 */
function renderNote(options: NoteOptions): string {
  const role = options.action.kind === "retry" ? ' role="alert"' : "";
  return `<div class="ov-note card ${escapeHtml(options.tone)}"${role}>
  <span class="dot ov-note-dot" aria-hidden="true"></span>
  <div class="ov-note-text">
    <p class="h2">${escapeHtml(options.title)}</p>
    <p class="body-text">${escapeHtml(options.body)}</p>
  </div>
  ${renderNoteAction(options.action)}
</div>`;
}

function renderNoteAction(action: NoteAction): string {
  switch (action.kind) {
    case "none":
      return "";
    case "link":
      return `<a class="btn btn-secondary ov-note-action" href="${escapeHtml(action.href)}">${escapeHtml(action.label)}</a>`;
    case "retry":
      return `<button type="button" class="btn btn-primary ov-note-action" data-ov-retry="${action.region}">${escapeHtml(action.label)}</button>`;
    default:
      return assertNever(action);
  }
}

/** `1` → `01`; ranks are read as a column, so they keep their width. */
export function rankText(rank: number): string {
  return String(rank).padStart(2, "0");
}

/** A tone resolved from data: the class to set, and the solid to set with it. */
export interface ToneChoice {
  readonly className: string;
  /** `null` when the taxonomy carries no usable colour and the neutral tone wins. */
  readonly solid: string | null;
}

/** The same choice, pre-rendered as the attributes a string template needs. */
export interface ToneAttributes {
  readonly className: string;
  readonly styleAttr: string;
}

/**
 * Maps an API taxonomy colour onto the tone system.
 *
 * A valid colour becomes `.tone-data` with `--tone-solid` set on the element,
 * from which base.css derives a readable ink and a wash — so a state recoloured
 * in the admin renders correctly with zero frontend changes (Data-Owns-Color
 * Rule). A missing or unparseable colour falls back to the neutral tone rather
 * than pushing an untrusted string into a style declaration.
 */
export function toneChoice(color: string | null): ToneChoice {
  if (color !== null && HEX_COLOR.test(color)) {
    return { className: "tone-data", solid: color };
  }
  return { className: "tone-piedra", solid: null };
}

/** {@link toneChoice} rendered for a string template. */
export function toneAttributes(color: string | null): ToneAttributes {
  const choice = toneChoice(color);
  return {
    className: choice.className,
    styleAttr:
      choice.solid === null ? "" : `style="--tone-solid:${choice.solid}"`,
  };
}

/**
 * The shared-element name that morphs a card's title into the detail header.
 *
 * Written as the CSS property directly because this markup is a string and
 * cannot carry Astro's `transition:name` directive; the directive compiles to
 * the same `view-transition-name`, so a detail page declaring
 * `transition:name={"proj-" + code}` pairs with what is emitted here.
 * Non-ident characters are folded to `-` so any project code yields a legal
 * custom-ident.
 */
export function viewTransitionName(code: string): string {
  return `proj-${code.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}
