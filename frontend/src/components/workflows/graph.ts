/**
 * The `/workflows` view model: a configured state graph turned into nodes, arcs
 * and table rows. Pure TypeScript — no DOM, no Astro, no network.
 *
 * The surface draws every workflow twice, and both drawings come from here so
 * they cannot disagree: an **arc diagram** (a column per state in the operator's
 * order, arrows arching over the row when they advance and under it when they
 * go back) and a **table** of the same edges, which is the accessible one — the
 * diagram is `aria-hidden`, so a reader who never sees it still gets the whole
 * graph.
 *
 * Two properties the layout must survive, because the configuration really has
 * them: a **back edge** (`en_progreso → por_hacer`) and a **state nobody can
 * leave or reach**. Back edges are routed under the row rather than crossed
 * through it, and an unattached state keeps its column and is named — a state
 * missing from the picture is exactly the question this screen exists to answer.
 *
 * Geometry is computed here rather than measured in the browser: the page is
 * server-rendered with no island, so the arcs have to line up with the node
 * cards on the first paint. {@link GRAPH} is the single source of those numbers
 * — the components emit them as custom properties and the CSS reads them back,
 * so an arc can never point at where a node used to be.
 *
 * Nothing in this module is keyed by a state `code`
 * (`docs/standards/PATTERNS_FRONTEND.md` §7): order comes from the wire, colour
 * from the state's own `color` falling back to its `category`, and a state added
 * in the admin this morning draws itself.
 */
import type {
  StateRef,
  TaxonomyRef,
  WorkflowCatalog,
  WorkflowShape,
} from "../../lib/api/domain";
import { stateTone, toneStyle } from "../projects/tone";

/**
 * One configured arrow, derived from the generated tree rather than restated, so
 * a field added to `WorkflowEdgeView` on the backend arrives here typed.
 */
type WorkflowEdge = WorkflowShape["transitions"][number];

/**
 * Diagram geometry, in CSS pixels, on the 8px rhythm.
 *
 * These are structural measurements of a drawing, not design tokens: they say
 * how big a node box is and how far apart two arcs may sit, which no palette or
 * spacing scale can answer. They live in one object because the arc paths and
 * the node cards are laid out by two different mechanisms — SVG coordinates and
 * absolutely positioned HTML — and the only thing keeping those two in register
 * is that both read these numbers.
 */
export const GRAPH = {
  /** Width of a state card, wide enough for a two-line label at title size. */
  nodeWidth: 176,
  /** Height of a state card: label, code, and the optional "sin salida" chip. */
  nodeHeight: 96,
  /** Horizontal room between two cards; the short arcs live in it. */
  columnGap: 80,
  /** Vertical pitch between two stacked arcs, sized for a label chip plus air. */
  laneHeight: 40,
  /** Clearance between the row of cards and the nearest arc. */
  arcMargin: 24,
  /** Breathing room around the whole drawing. */
  padding: 16,
  /** Height of an arc's label chip; half of it overhangs the topmost arc. */
  labelHeight: 24,
  /** Inset of the outermost anchor from a card's corner. */
  anchorInset: 20,
  /**
   * Average advance per character at label size, used to reserve horizontal room
   * for an arc's caption while packing arcs into lanes.
   *
   * An estimate is enough and a measurement is impossible: the server has no
   * font metrics. It is deliberately generous — over-reserving costs one extra
   * lane, under-reserving overlaps two captions.
   */
  labelCharWidth: 7.2,
  /** Chip padding either side of the caption text. */
  labelPadding: 28,
  /** Clear space demanded between two captions sharing a lane. */
  labelGap: 16,
  /** Extra room the reason mark takes on a caption. */
  reasonMarkWidth: 10,
} as const;

/** Which way an arrow runs relative to the operator's ordering of the states. */
export type ArcDirection = "forward" | "back";

/**
 * What the edge set says about a state, beyond the state's own row.
 *
 * This is the screen's payload for somebody who was refused a move: `terminal`
 * means nothing leaves here, `isolated` means nothing leaves *or* arrives, and
 * both are conclusions about the configuration rather than about a record.
 */
export type StateRole = "connected" | "terminal" | "isolated";

/**
 * What a card says about itself when the edge set leaves it stranded; `connected` says nothing,
 * because the arrows on the card already speak for it.
 *
 * Here rather than in the component because both the server's first paint and the editor's
 * repaint write it, and two copies of the same three words is how a diagram ends up saying "sin
 * salida" in one half and "terminal" in the other.
 */
export const ROLE_NOTE: Readonly<Record<StateRole, string | null>> = {
  connected: null,
  terminal: "sin salida",
  isolated: "sin movimientos",
};

/** A state ready to paint: the ref plus the tone binding it renders through. */
export interface StateChip {
  readonly state: StateRef;
  /** Tone class from `styles/base.css`, or `tone-data` when the colour is the operator's. */
  readonly toneClass: string;
  /** Inline `--tone-solid` for `tone-data`; `null` when the class carries its own colours. */
  readonly toneStyle: string | null;
  /**
   * The operator's colour behind that inline style, or `null` for a semantic tone.
   *
   * The same fact as {@link toneStyle} in the form the *editor* needs it: an Astro attribute
   * takes the declaration, while repainting a cloned row takes the value. Deriving one from the
   * other by parsing a CSS string would be a second spelling of the same rule.
   */
  readonly toneSolid: string | null;
}

/** One state as a column of the diagram: where its card sits and what it is attached to. */
export interface NodeView extends StateChip {
  /** Left edge of the card, in diagram coordinates. */
  readonly x: number;
  /** Top edge of the card, in diagram coordinates. */
  readonly y: number;
  readonly role: StateRole;
}

/**
 * One state as the editor reads it: the chip, plus what shaping it needs.
 *
 * Wider than {@link NodeView} and narrower in a different direction: it carries no geometry
 * because it is a row rather than a column, and it carries the authoring facts the diagram has
 * no way to draw — where the operator put it, whether it is the entry node, whether it is still
 * part of the graph, how many records are standing on it and whether it may be retired **right
 * now**. `canRetire` is the server's answer, never derived here: a client that computed it
 * would offer an action the API then refuses.
 */
export interface StateRow extends StateChip {
  /** The operator's arrangement; what the reorder field is pre-filled with. */
  readonly order: number;
  /** The entry node new work is placed on; at most one per graph. */
  readonly isInitial: boolean;
  /** The operator marked this state as an end of the lifecycle. Descriptive. */
  readonly isTerminal: boolean;
  /** `false` means retired: out of the graph for new work, restorable from its row. */
  readonly isActive: boolean;
  /** Projects and tasks currently standing here — the reason a retirement is refused. */
  readonly recordCount: number;
  /** Whether `DELETE` on this state would succeed right now. */
  readonly canRetire: boolean;
  /** What the edge set says about it: `terminal` and `isolated` are conclusions, not flags. */
  readonly role: StateRole;
}

/** One arrow as drawn: an SVG path plus the position of its caption. */
export interface ArcView {
  readonly key: string;
  readonly label: string;
  readonly requiresReason: boolean;
  readonly direction: ArcDirection;
  /** `d` of the cubic that leaves the source vertically and arrives vertically. */
  readonly path: string;
  readonly labelX: number;
  readonly labelY: number;
}

/** One arrow as a table row — the accessible representation of the same edge. */
export interface EdgeRow {
  readonly key: string;
  readonly from: StateChip;
  readonly to: StateChip;
  /** Source state code: half of the edge's address, which is what the editor writes to. */
  readonly fromCode: string;
  /** Target state code: the other half. An edge *is* its endpoints, so neither is editable. */
  readonly toCode: string;
  /** The operator's own wording for the move; rendered, never compared against. */
  readonly label: string;
  readonly requiresReason: boolean;
  /** Field names that must be filled on the record before the move is offered. */
  readonly requiresFields: readonly string[];
}

/** One configured workflow, ready for the section that renders it. */
export interface WorkflowView {
  readonly code: string;
  readonly name: string;
  /** `PROJECT` | `TASK`, as the wire spells it; the editor sends it back unchanged. */
  readonly appliesTo: string;
  /** Spanish name of what the graph governs; the raw value if it is unknown. */
  readonly appliesToLabel: string;
  readonly isDefault: boolean;
  readonly isActive: boolean;
  readonly engagementTypes: readonly TaxonomyRef[];
  /**
   * Every state the graph owns, retired ones included, in the operator's order.
   *
   * The editor's list, and deliberately wider than {@link nodes}: a retired state is not part
   * of the lifecycle and is therefore not drawn, but it is the only place its restoration can
   * be offered from — omitting it would make a retirement irreversible from the product.
   */
  readonly states: readonly StateRow[];
  /** The states the diagram draws: the active ones, in column order. */
  readonly nodes: readonly NodeView[];
  readonly arcs: readonly ArcView[];
  readonly rows: readonly EdgeRow[];
  /** Labels of states nothing leaves; the answer to "why can I not move from here". */
  readonly terminalLabels: readonly string[];
  /** Labels of states nothing leaves *or* reaches; invisible in a table of edges. */
  readonly isolatedLabels: readonly string[];
  readonly width: number;
  readonly height: number;
  /** Id of this graph's own arrowhead marker; ids are document-wide. */
  readonly arrowId: string;
}

/**
 * `Workflow.applies_to` is a closed structural vocabulary — adding a value is a
 * migration, not a fixture row — so naming it in Spanish is safe, unlike naming
 * a state code, which the operator owns.
 */
const APPLIES_TO_LABEL: Readonly<Record<string, string>> = {
  PROJECT: "Proyectos",
  TASK: "Tareas",
};

/** Spanish name of what a graph governs; an unknown value renders itself. */
function appliesToLabel(appliesTo: string): string {
  return APPLIES_TO_LABEL[appliesTo] ?? appliesTo;
}

/** A horizontal interval of the diagram, used to pack arcs into lanes. */
interface Footprint {
  readonly start: number;
  readonly end: number;
}

/** An arc that resolved to two distinct columns and can therefore be drawn. */
interface DrawableEdge {
  readonly edge: WorkflowEdge;
  readonly key: string;
  readonly fromIndex: number;
  readonly toIndex: number;
  readonly direction: ArcDirection;
}

/** One end of an arc waiting for its anchor along a card's edge. */
interface Attachment {
  readonly drawable: number;
  readonly other: number;
  readonly isStart: boolean;
}

/** Left edge of the card in column `index`. */
function columnX(index: number): number {
  return GRAPH.padding + index * (GRAPH.nodeWidth + GRAPH.columnGap);
}

/** Horizontal room an arc's caption needs, estimated from its character count. */
function captionWidth(edge: WorkflowEdge): number {
  const mark = edge.requires_reason ? GRAPH.reasonMarkWidth : 0;
  return edge.label.length * GRAPH.labelCharWidth + GRAPH.labelPadding + mark;
}

/**
 * Assigns each footprint the lowest lane in which it overlaps nothing already
 * placed, shortest first.
 *
 * Shortest first is what keeps the picture readable: a long arc that spans the
 * whole row is pushed to an outer lane instead of parking on top of the short
 * hops underneath it. Overlap is decided on the footprint, which is the arc's
 * span *unioned with its caption* — two arcs that never touch still collide if
 * their captions do, and a caption sitting on another caption is the one failure
 * a diagram cannot recover from.
 */
function packLanes(footprints: readonly Footprint[]): number[] {
  const order = footprints
    .map((footprint, index) => ({ footprint, index }))
    .sort((a, b) => {
      const widthA = a.footprint.end - a.footprint.start;
      const widthB = b.footprint.end - b.footprint.start;
      if (widthA !== widthB) return widthA - widthB;
      return a.footprint.start - b.footprint.start;
    });

  const lanes: Footprint[][] = [];
  const assigned: number[] = footprints.map(() => 0);

  for (const { footprint, index } of order) {
    let lane = 0;
    for (;;) {
      const occupants = lanes[lane];
      if (occupants === undefined) {
        lanes[lane] = [footprint];
        break;
      }
      const fits = occupants.every(
        (taken) => taken.end <= footprint.start || taken.start >= footprint.end,
      );
      if (fits) {
        occupants.push(footprint);
        break;
      }
      lane += 1;
    }
    assigned[index] = lane;
  }

  return assigned;
}

/**
 * Spreads the arcs attached to one edge of one card across that edge.
 *
 * Ordered by where the other end sits, so an incoming arc from the left lands on
 * the left of the card and an outgoing arc to the right leaves from the right —
 * which is what stops two arrows from crossing for no reason other than the
 * order they arrived in.
 */
function assignAnchors(
  attachments: readonly Attachment[],
  nodeIndex: number,
  startX: number[],
  endX: number[],
): void {
  const ordered = [...attachments].sort((a, b) => a.other - b.other);
  const band = GRAPH.nodeWidth - GRAPH.anchorInset * 2;
  const left = columnX(nodeIndex) + GRAPH.anchorInset;

  ordered.forEach((attachment, position) => {
    const x = left + (band * (position + 1)) / (ordered.length + 1);
    if (attachment.isStart) {
      startX[attachment.drawable] = x;
      return;
    }
    endX[attachment.drawable] = x;
  });
}

/** Trims a float to two decimals so the emitted path stays readable. */
function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * The cubic whose visible apex sits exactly on `apexY`.
 *
 * Both control points share one height, so the curve leaves the source straight
 * up and meets the target straight down — the arrowhead then reads as entering
 * the card rather than grazing it. A symmetric cubic peaks at three quarters of
 * its control height, which is why the control is placed past the lane rather
 * than on it.
 */
function arcPath(
  fromX: number,
  toX: number,
  edgeY: number,
  apexY: number,
): string {
  const control = (4 * apexY - edgeY) / 3;
  return `M ${round(fromX)},${round(edgeY)} C ${round(fromX)},${round(control)} ${round(toX)},${round(control)} ${round(toX)},${round(edgeY)}`;
}

/** Ids are document-wide and a page holds several graphs; keep them selector-safe. */
function arrowMarkerId(code: string): string {
  return `wf-arrow-${code.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

/** The tone binding for one state: its own colour, or its category's semantic tone. */
function chipOf(state: StateRef): StateChip {
  const tone = stateTone(state);
  return {
    state,
    toneClass: tone.className,
    toneStyle: toneStyle(tone),
    toneSolid: tone.solid,
  };
}

/**
 * A state named by an edge but absent from `states`.
 *
 * The backend guarantees both endpoints belong to the same graph, so this is
 * unreachable in practice — but the lookup is still fallible to the compiler,
 * and rendering the raw code beats dropping a configured move from the one
 * representation that is supposed to be complete.
 */
function unknownState(code: string): StateRef {
  return { code, label: code, category: "UNKNOWN", color: null };
}

/**
 * Turns one workflow document into everything the section renders.
 *
 * The passes are ordered by what depends on what: columns fix every `x`, anchors
 * need the columns, lane packing needs the anchors and the captions, and only
 * then is the height of the drawing — and therefore every `y` — known.
 *
 * A self-referencing edge is kept in the table and left out of the picture: an
 * arrow from a card to itself has no second anchor to run to, and inventing a
 * loop for a shape the configuration has never produced would be geometry
 * written for nobody.
 */
export function buildWorkflowView(shape: WorkflowShape): WorkflowView {
  // Only the active states are columns. A retired state is out of the lifecycle — every arrow
  // touching it was withdrawn when it left, and it cannot be occupied, since the retirement is
  // refused while a record stands there — so drawing it would put a column in the picture that
  // nothing can enter, leave or sit in. It keeps its row in the states table, which is where it
  // is restored from.
  const drawn = shape.states.filter((state) => state.is_active);

  const indexByCode = new Map<string, number>();
  const chipByCode = new Map<string, StateChip>();
  drawn.forEach((state, index) => {
    indexByCode.set(state.code, index);
    chipByCode.set(state.code, chipOf(state));
  });
  for (const state of shape.states) {
    if (!chipByCode.has(state.code)) chipByCode.set(state.code, chipOf(state));
  }

  const outgoing = drawn.map(() => 0);
  const incoming = drawn.map(() => 0);

  const drawables: DrawableEdge[] = [];
  const rows: EdgeRow[] = [];

  shape.transitions.forEach((edge, position) => {
    const key = `${edge.from_state}->${edge.to_state}#${position}`;
    const fromIndex = indexByCode.get(edge.from_state);
    const toIndex = indexByCode.get(edge.to_state);

    rows.push({
      key,
      from:
        chipByCode.get(edge.from_state) ??
        chipOf(unknownState(edge.from_state)),
      to: chipByCode.get(edge.to_state) ?? chipOf(unknownState(edge.to_state)),
      fromCode: edge.from_state,
      toCode: edge.to_state,
      label: edge.label,
      requiresReason: edge.requires_reason,
      requiresFields: edge.requires_fields,
    });

    if (fromIndex === undefined || toIndex === undefined) return;
    outgoing[fromIndex] = (outgoing[fromIndex] ?? 0) + 1;
    incoming[toIndex] = (incoming[toIndex] ?? 0) + 1;
    if (fromIndex === toIndex) return;

    drawables.push({
      edge,
      key,
      fromIndex,
      toIndex,
      direction: toIndex > fromIndex ? "forward" : "back",
    });
  });

  // Anchors: every arc gets one point on each of the two cards it joins.
  const startX: number[] = drawables.map(() => 0);
  const endX: number[] = drawables.map(() => 0);
  const attachments = new Map<
    string,
    { nodeIndex: number; items: Attachment[] }
  >();

  const push = (
    nodeIndex: number,
    side: ArcDirection,
    item: Attachment,
  ): void => {
    const slot = `${nodeIndex}:${side}`;
    const current = attachments.get(slot);
    if (current === undefined) {
      attachments.set(slot, { nodeIndex, items: [item] });
      return;
    }
    current.items.push(item);
  };

  drawables.forEach((drawable, index) => {
    push(drawable.fromIndex, drawable.direction, {
      drawable: index,
      other: drawable.toIndex,
      isStart: true,
    });
    push(drawable.toIndex, drawable.direction, {
      drawable: index,
      other: drawable.fromIndex,
      isStart: false,
    });
  });

  for (const { nodeIndex, items } of attachments.values()) {
    assignAnchors(items, nodeIndex, startX, endX);
  }

  // Lanes: one packing per side, so the arcs above and below never share a level.
  const above: number[] = [];
  const below: number[] = [];
  drawables.forEach((drawable, index) => {
    (drawable.direction === "forward" ? above : below).push(index);
  });

  const footprintOf = (index: number): Footprint => {
    const from = startX[index] ?? 0;
    const to = endX[index] ?? 0;
    const drawable = drawables[index];
    const half =
      drawable === undefined
        ? 0
        : captionWidth(drawable.edge) / 2 + GRAPH.labelGap;
    const middle = (from + to) / 2;
    return {
      start: Math.min(Math.min(from, to), middle - half),
      end: Math.max(Math.max(from, to), middle + half),
    };
  };

  const aboveLanes = packLanes(above.map(footprintOf));
  const belowLanes = packLanes(below.map(footprintOf));

  const laneCount = (lanes: readonly number[]): number =>
    lanes.length === 0 ? 0 : Math.max(...lanes) + 1;

  const extent = (lanes: readonly number[]): number => {
    const count = laneCount(lanes);
    if (count === 0) return 0;
    return (
      GRAPH.arcMargin + (count - 1) * GRAPH.laneHeight + GRAPH.labelHeight / 2
    );
  };

  const aboveExtent = extent(aboveLanes);
  const belowExtent = extent(belowLanes);
  const nodeY = GRAPH.padding + aboveExtent;
  const bottomY = nodeY + GRAPH.nodeHeight;

  const roleOf = (index: number): StateRole => {
    const out = outgoing[index] ?? 0;
    const into = incoming[index] ?? 0;
    if (out === 0 && into === 0) return "isolated";
    return out === 0 ? "terminal" : "connected";
  };

  const nodes: NodeView[] = drawn.map((state, index) => ({
    ...chipOf(state),
    x: columnX(index),
    y: nodeY,
    role: roleOf(index),
  }));

  const states: StateRow[] = shape.states.map((state) => {
    const index = indexByCode.get(state.code);
    return {
      ...chipOf(state),
      order: state.order,
      isInitial: state.is_initial,
      isTerminal: state.is_terminal,
      isActive: state.is_active,
      recordCount: state.record_count,
      canRetire: state.can_retire,
      // A retired state has no column and therefore no edges to conclude anything from.
      role: index === undefined ? "isolated" : roleOf(index),
    };
  });

  const arcs: ArcView[] = [];
  const emit = (
    indices: readonly number[],
    lanes: readonly number[],
    edgeY: number,
    sign: number,
  ): void => {
    indices.forEach((index, position) => {
      const drawable = drawables[index];
      const lane = lanes[position];
      if (drawable === undefined || lane === undefined) return;
      const apexY = edgeY + sign * (GRAPH.arcMargin + lane * GRAPH.laneHeight);
      const from = startX[index] ?? 0;
      const to = endX[index] ?? 0;
      arcs.push({
        key: drawable.key,
        label: drawable.edge.label,
        requiresReason: drawable.edge.requires_reason,
        direction: drawable.direction,
        path: arcPath(from, to, edgeY, apexY),
        labelX: round((from + to) / 2),
        labelY: round(apexY),
      });
    });
  };

  emit(above, aboveLanes, nodeY, -1);
  emit(below, belowLanes, bottomY, 1);

  const columns = drawn.length;
  const width =
    columns === 0
      ? GRAPH.padding * 2
      : GRAPH.padding * 2 +
        columns * GRAPH.nodeWidth +
        (columns - 1) * GRAPH.columnGap;

  return {
    code: shape.code,
    name: shape.name,
    appliesTo: shape.applies_to,
    appliesToLabel: appliesToLabel(shape.applies_to),
    isDefault: shape.is_default,
    isActive: shape.is_active,
    engagementTypes: shape.engagement_types,
    states,
    nodes,
    arcs,
    rows,
    terminalLabels: nodes
      .filter((node) => node.role === "terminal")
      .map((node) => node.state.label),
    isolatedLabels: nodes
      .filter((node) => node.role === "isolated")
      .map((node) => node.state.label),
    width,
    height: GRAPH.padding * 2 + aboveExtent + GRAPH.nodeHeight + belowExtent,
    arrowId: arrowMarkerId(shape.code),
  };
}

/** Every configured graph of the document, in the order the API published them. */
export function buildWorkflowViews(
  catalog: WorkflowCatalog,
): readonly WorkflowView[] {
  return catalog.workflows.map(buildWorkflowView);
}

/**
 * A graph with nothing in it, for the `<template>` the editor clones a new section from.
 *
 * Built through {@link buildWorkflowView} rather than written out as a literal, so the blank
 * scaffold is the same shape a real graph produces — a hand-written stand-in would stop
 * matching the moment a field was added, and the drift would only show up as a section that
 * renders wrong the first time somebody creates a workflow.
 */
export function emptyWorkflowView(): WorkflowView {
  return buildWorkflowView({
    code: "",
    name: "",
    applies_to: "PROJECT",
    is_default: false,
    is_active: true,
    engagement_types: [],
    states: [],
    transitions: [],
  });
}
