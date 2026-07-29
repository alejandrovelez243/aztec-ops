/**
 * The live behaviour of the Resumen surface: the signature moment, the stream
 * banner, and the two reads the island can re-run on its own.
 *
 * **The moment.** When a colleague's recompute lands, the affected card does not
 * blink into a new number — it counts up, flashes its wash, and *travels* to its
 * new zone while its neighbours make room, all on the `carta` spring. Because
 * every card in every zone is the same DOM (see `./markup.ts`), a card promoted
 * from Radar to Atender hoy is a `flip()` of an existing node rather than a
 * destroy-and-rebuild, which is the only reason the journey can be seen at all.
 *
 * **Reconciliation.** Patches are field-level and are dropped when the envelope
 * is older than the `updated_at` the card was rendered with
 * (`docs/standards/PATTERNS_FRONTEND.md` §6). Two cases re-read instead of
 * patching, because they mean the local view is wrong rather than merely behind:
 * a `stream.reset` frame, and a `project.state_changed` naming a workflow state
 * no card on the page has rendered — its Spanish label is not in the payload, so
 * inventing one would put words in the operator's mouth.
 *
 * **Why this module also reads.** The first paint is rendered in page
 * frontmatter, which runs without the operator's session. When that read comes
 * back refused, the region ships its error arm and this module re-runs exactly
 * the same call from the browser, where the session exists. It is the error
 * arm's own retry, fired automatically instead of waiting to be clicked; nothing
 * else re-fetches, and a region that painted fine on the server is never
 * re-read.
 */

import { getQueue, getTeamLoad } from "../../lib/api/client";
import type { ApiError } from "../../lib/api/errors";
import { formatScore } from "../../lib/format/score";
import { countUp, flip, shake } from "../../lib/motion/spring";
import {
  onReset,
  onStatus,
  retry,
  subscribe,
  type Envelope,
  type Status,
} from "../../lib/stream/store";
import type { Topic } from "../../lib/stream/topics";
import { toast } from "../../lib/toast";
import { toViewState } from "../../lib/view-state";
import {
  CARD_COPY,
  failureText,
  QUEUE_COPY,
  STREAM_COPY,
  TEAM_COPY,
} from "./copy";
import { parsePriorityRecalculated, parseStateChanged } from "./envelope";
import {
  renderBars,
  renderQueueRegion,
  renderTeamRegion,
  rankText,
  toneChoice,
  type RegionKey,
} from "./markup";
import { QUEUE_PAGE_SIZE, toBars, zoneOfRank } from "./model";

/** Topics named once, so a renamed backend topic breaks the build, not the page. */
const PRIORITY_RECALCULATED: Topic = "project.priority.recalculated";
const STATE_CHANGED: Topic = "project.state_changed";

/** How long the attribution chip rides on a card after a remote move. */
const ATTRIBUTION_MS = 4_000;

/** How long the wash flash marks the card a colleague just moved. */
const FLASH_MS = 1_400;

/** The actor a derived event carries; the engine, not a person. */
const SYSTEM_ACTOR = "system";

const SELECTOR = {
  root: "[data-ov-root]",
  card: "[data-ov-card]",
  zoneList: "[data-ov-zone-list]",
  zone: "[data-ov-zone]",
  zoneCount: "[data-ov-zone-count]",
  rankText: "[data-ov-rank-text]",
  scoreText: "[data-ov-score-text]",
  stateChip: "[data-ov-state-chip]",
  bars: "[data-ov-bars]",
  attribution: "[data-ov-attribution]",
  stale: "[data-ov-stale]",
  staleTime: "[data-ov-stale-time]",
  reconnect: "[data-ov-reconnect]",
  retry: "[data-ov-retry]",
} as const;

let cleanups: (() => void)[] = [];
let streamStatus: Status = "connecting";
let lastEventAt: string | null = null;

/** Serializes reshuffles so two envelopes in the same tick cannot fight over FLIP. */
let pending: Promise<void> = Promise.resolve();

/**
 * Wires the surface. Idempotent per navigation: call from `astro:page-load`,
 * and call {@link unmount} from `astro:before-swap`.
 *
 * Does nothing when the current page has no overview root, which is what lets
 * the island's module run once per app while the surface comes and goes.
 */
export function mount(): void {
  unmount();
  const root = document.querySelector<HTMLElement>(SELECTOR.root);
  if (root === null) return;

  cleanups.push(
    subscribe(PRIORITY_RECALCULATED, (envelope) => {
      noteEvent(root, envelope);
      enqueue(() => applyPriority(root, envelope));
    }),
    subscribe(STATE_CHANGED, (envelope) => {
      noteEvent(root, envelope);
      enqueue(() => applyState(root, envelope));
    }),
    onReset(() => {
      enqueue(async () => {
        await refresh(root, "queue", { silent: true });
        await refresh(root, "team", { silent: true });
      });
    }),
    onStatus((status) => {
      streamStatus = status;
      paintStreamBanner(root);
    }),
  );

  const onClick = (event: MouseEvent): void => {
    handleClick(root, event);
  };
  root.addEventListener("click", onClick);
  cleanups.push(() => {
    root.removeEventListener("click", onClick);
  });

  paintStreamBanner(root);
  hydrateRefusedRegions(root);
}

/** Releases every subscription and listener. Safe to call when nothing is mounted. */
export function unmount(): void {
  for (const off of cleanups) off();
  cleanups = [];
}

/** Runs reshuffles one after another; a thrown handler never stalls the queue. */
function enqueue(work: () => Promise<void>): void {
  pending = pending.then(work).catch((error: unknown) => {
    console.error("aztec resumen: a live update failed and was isolated", error);
  });
}

/**
 * Re-reads the regions whose first paint was refused.
 *
 * The page marks them in `data-ov-hydrate`; a region that rendered rows on the
 * server is absent from that list and is left alone, so this is a recovery and
 * never a duplicate of the request the server already made.
 */
function hydrateRefusedRegions(root: HTMLElement): void {
  const requested = (root.dataset["ovHydrate"] ?? "").split(/\s+/);
  for (const region of requested) {
    if (region === "queue" || region === "team") {
      enqueue(async () => {
        await refresh(root, region, { silent: true });
      });
    }
  }
}

function handleClick(root: HTMLElement, event: MouseEvent): void {
  const target = event.target;
  if (!(target instanceof Element)) return;

  const reconnect = target.closest(SELECTOR.reconnect);
  if (reconnect !== null) {
    retry();
    return;
  }

  const button = target.closest<HTMLButtonElement>(SELECTOR.retry);
  if (button === null) return;
  const region = button.dataset["ovRetry"];
  if (region !== "queue" && region !== "team") return;
  enqueue(async () => {
    await runRetry(root, button, region);
  });
}

/**
 * The error arm's control: re-runs the same read with in-flight state on the
 * button itself, and answers a second failure where the operator is looking —
 * a shake on the control plus a toast naming the rule in Spanish.
 */
async function runRetry(
  root: HTMLElement,
  button: HTMLButtonElement,
  region: RegionKey,
): Promise<void> {
  button.classList.add("is-loading");
  button.disabled = true;
  const error = await refresh(root, region, { silent: true });
  // A successful read replaced the region, taking the button with it.
  if (!button.isConnected) return;
  button.classList.remove("is-loading");
  button.disabled = false;
  if (error === null) return;
  await shake(button);
  toast({
    kind: "error",
    title:
      region === "queue" ? QUEUE_COPY.errorTitle : TEAM_COPY.errorTitle,
    detail: failureText(error.code),
  });
}

interface RefreshOptions {
  /** Suppress the toast; the region's own error arm already says what failed. */
  readonly silent: boolean;
}

/**
 * Re-reads one region and re-renders it through the same function the server
 * used, so a client-rendered region and a server-rendered one are byte-for-byte
 * the same markup.
 *
 * @returns The failure, or `null` when the region now shows fresh data.
 */
async function refresh(
  root: HTMLElement,
  region: RegionKey,
  options: RefreshOptions,
): Promise<ApiError | null> {
  const host = root.querySelector<HTMLElement>(
    `[data-ov-region="${region}"]`,
  );
  if (host === null) return null;

  const error =
    region === "queue"
      ? await refreshQueue(host)
      : await refreshTeam(host);
  if (error !== null && !options.silent) {
    toast({
      kind: "error",
      title: region === "queue" ? QUEUE_COPY.errorTitle : TEAM_COPY.errorTitle,
      detail: failureText(error.code),
    });
  }
  return error;
}

async function refreshQueue(host: HTMLElement): Promise<ApiError | null> {
  const result = await getQueue({ page_size: QUEUE_PAGE_SIZE });
  const state = toViewState(result, streamStatus, {
    isEmpty: (page) => page.items.length === 0,
    emptyMessage: QUEUE_COPY.emptyBody,
    lastEventAt,
  });
  host.dataset["state"] = state.kind;
  host.innerHTML = renderQueueRegion(state, new Date());
  return result.ok ? null : result.error;
}

async function refreshTeam(host: HTMLElement): Promise<ApiError | null> {
  const result = await getTeamLoad();
  const state = toViewState(result, streamStatus, {
    isEmpty: (page) => page.items.length === 0,
    emptyMessage: TEAM_COPY.emptyBody,
    lastEventAt,
  });
  host.dataset["state"] = state.kind;
  host.innerHTML = renderTeamRegion(state);
  return result.ok ? null : result.error;
}

/**
 * The signature moment: a colleague's recompute reshuffles the morning.
 *
 * Order matters and is the contract of `flip()` — the DOM is mutated into its
 * final state first (new score, new bars, new zone), and only then is the
 * journey animated from where the cards used to be. Reversing it leaves
 * reduced-motion users looking at stale positions rather than at a calmer page.
 */
async function applyPriority(
  root: HTMLElement,
  envelope: Envelope,
): Promise<void> {
  const card = cardFor(root, envelope.entity.id);
  if (card === null) return;
  if (!isNewer(envelope.occurred_at, card.dataset["ovUpdatedAt"])) return;
  const payload = parsePriorityRecalculated(envelope.payload);
  if (payload === null) return;

  const previous = scoreOf(card);
  const scoreText = card.querySelector<HTMLElement>(SELECTOR.scoreText);
  const cards = [...root.querySelectorAll<HTMLElement>(SELECTOR.card)];

  await flip(cards, () => {
    card.dataset["ovScore"] = String(payload.value);
    card.dataset["ovUpdatedAt"] = envelope.occurred_at;
    const bars = card.querySelector<HTMLElement>(SELECTOR.bars);
    if (bars !== null) bars.outerHTML = renderBars(toBars(payload.breakdown));
    reorder(root);
  });

  if (scoreText !== null) {
    countUp(scoreText, previous, payload.value, formatScore);
  }
  announce(card, `${actorLabel(envelope.actor)} · ${CARD_COPY.priorityMoved}`);
  flash(card);
}

/**
 * Patches the state chip a colleague just moved.
 *
 * The envelope carries the state `code`, never its Spanish label — labels are
 * operator-editable data. The label is therefore looked up among the states
 * already rendered on this page; a state nobody on screen holds triggers a
 * re-read instead of a guess, which is also the only path that can add a card
 * that was not in this page's slice of the queue.
 */
async function applyState(
  root: HTMLElement,
  envelope: Envelope,
): Promise<void> {
  const card = cardFor(root, envelope.entity.id);
  if (card === null) return;
  if (!isNewer(envelope.occurred_at, card.dataset["ovUpdatedAt"])) return;
  const payload = parseStateChanged(envelope.payload);
  if (payload === null) return;

  const known = knownState(root, payload.to);
  if (known === null) {
    await refresh(root, "queue", { silent: true });
    return;
  }

  card.dataset["ovUpdatedAt"] = envelope.occurred_at;
  card.dataset["ovStateCode"] = payload.to;
  card.dataset["ovStateLabel"] = known.label;
  card.dataset["ovStateColor"] = known.color ?? "";
  const chip = card.querySelector<HTMLElement>(SELECTOR.stateChip);
  if (chip !== null) chip.textContent = known.label;
  applyTone(card, known.color);

  announce(card, `${actorLabel(envelope.actor)} → ${known.label}`);
  flash(card);
}

/** A workflow state's rendered label and colour, harvested from the page itself. */
interface KnownState {
  readonly label: string;
  readonly color: string | null;
}

/**
 * Looks a state up among the ones currently on screen.
 *
 * Reading the vocabulary off the rendered cards rather than out of a constant is
 * what keeps the promise that a workflow state added from the admin needs no
 * frontend change: there is no list here to fall out of date.
 */
function knownState(root: HTMLElement, code: string): KnownState | null {
  const match = root.querySelector<HTMLElement>(
    `${SELECTOR.card}[data-ov-state-code="${CSS.escape(code)}"]`,
  );
  if (match === null) return null;
  const label = match.dataset["ovStateLabel"] ?? "";
  if (label === "") return null;
  const color = match.dataset["ovStateColor"] ?? "";
  return { label, color: color === "" ? null : color };
}

/**
 * Re-sorts every card and re-parents it into the zone its new rank belongs to.
 *
 * The sort reproduces the server's own ordering — score descending, then code —
 * so a locally reshuffled board and the next server read agree. Ranks are
 * renumbered continuously across all three zones: Radar starts where Esta semana
 * stopped, never at one.
 */
function reorder(root: HTMLElement): void {
  const cards = [...root.querySelectorAll<HTMLElement>(SELECTOR.card)];
  cards.sort(
    (left, right) =>
      scoreOf(right) - scoreOf(left) || codeOf(left).localeCompare(codeOf(right)),
  );
  for (const [index, card] of cards.entries()) {
    const rank = index + 1;
    card.dataset["ovRank"] = String(rank);
    const plate = card.querySelector<HTMLElement>(SELECTOR.rankText);
    if (plate !== null) plate.textContent = rankText(rank);
    const list = root.querySelector<HTMLElement>(
      `[data-ov-zone-list="${zoneOfRank(rank)}"]`,
    );
    // Appending in ascending rank order is what puts each list in order.
    if (list !== null) list.append(card);
  }
  paintZoneCounts(root);
}

/** Keeps each zone's heading count true, and hides a zone that emptied out. */
function paintZoneCounts(root: HTMLElement): void {
  for (const zone of root.querySelectorAll<HTMLElement>(SELECTOR.zone)) {
    const list = zone.querySelector<HTMLElement>(SELECTOR.zoneList);
    const count = list === null ? 0 : list.childElementCount;
    const readout = zone.querySelector<HTMLElement>(SELECTOR.zoneCount);
    if (readout !== null) readout.textContent = String(count);
    zone.hidden = count === 0;
  }
}

/** Reveals the staleness banner while the stream is down, with the last event. */
function paintStreamBanner(root: HTMLElement): void {
  const banner = root.querySelector<HTMLElement>(SELECTOR.stale);
  if (banner === null) return;
  banner.hidden = streamStatus !== "disconnected";
  const time = banner.querySelector<HTMLElement>(SELECTOR.staleTime);
  if (time === null) return;
  time.textContent =
    lastEventAt === null
      ? STREAM_COPY.staleNoEvents
      : `${STREAM_COPY.lastEventPrefix} ${clockText(lastEventAt)}`;
}

/** The attribution chip that rides on a card a colleague moved, then fades. */
function announce(card: HTMLElement, text: string): void {
  const chip = card.querySelector<HTMLElement>(SELECTOR.attribution);
  if (chip === null) return;
  chip.textContent = text;
  chip.hidden = false;
  window.setTimeout(() => {
    chip.hidden = true;
  }, ATTRIBUTION_MS);
}

/** The wash flash. Under reduced motion the class is inert; the chip still speaks. */
function flash(card: HTMLElement): void {
  card.classList.add("is-flashing");
  window.setTimeout(() => {
    card.classList.remove("is-flashing");
  }, FLASH_MS);
}

function applyTone(card: HTMLElement, color: string | null): void {
  const choice = toneChoice(color);
  card.classList.remove("tone-data", "tone-piedra");
  card.classList.add(choice.className);
  if (choice.solid === null) {
    card.style.removeProperty("--tone-solid");
    return;
  }
  card.style.setProperty("--tone-solid", choice.solid);
}

function cardFor(root: HTMLElement, code: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(
    `${SELECTOR.card}[data-ov-code="${CSS.escape(code)}"]`,
  );
}

function scoreOf(card: HTMLElement): number {
  const parsed = Number.parseFloat(card.dataset["ovScore"] ?? "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

function codeOf(card: HTMLElement): string {
  return card.dataset["ovCode"] ?? "";
}

/**
 * Whether an envelope is newer than what the card was rendered with.
 *
 * An unparseable timestamp on either side lets the patch through: the two
 * instants come from the same server, so a value neither side can read is a bug
 * to see rather than a reason to freeze a card on stale data.
 */
function isNewer(occurredAt: string, renderedAt: string | undefined): boolean {
  if (renderedAt === undefined || renderedAt === "") return true;
  const event = Date.parse(occurredAt);
  const rendered = Date.parse(renderedAt);
  if (Number.isNaN(event) || Number.isNaN(rendered)) return true;
  return event > rendered;
}

/** `system` is the engine, not a colleague; everyone else is named as they signed. */
function actorLabel(actor: string): string {
  return actor === SYSTEM_ACTOR ? CARD_COPY.systemActor : actor;
}

/** `2026-07-28T09:05:13Z` → `09:05`, in the reader's own clock. */
function clockText(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleTimeString("es", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Records when the stream last delivered anything, and refreshes the banner if
 * it is on screen.
 *
 * Taken from every envelope this surface subscribes to, including ones naming a
 * project it does not render: an event for a card outside this slice still
 * proves the connection is alive, which is exactly what the staleness readout
 * claims.
 */
function noteEvent(root: HTMLElement, envelope: Envelope): void {
  lastEventAt = envelope.occurred_at;
  paintStreamBanner(root);
}
