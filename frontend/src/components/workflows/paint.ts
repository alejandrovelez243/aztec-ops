/**
 * Writing a workflow back onto the section that renders it.
 *
 * Every authoring write answers with the whole graph **re-read by the server**, so this is not
 * an optimistic patch: it is the same document the page would render on a reload, applied
 * without one. That is what keeps the promise the surface makes after every write — the diagram,
 * the states and the moves are three projections of one shape, so they cannot disagree, and the
 * numbers in the header are counted from the same arrays the tables enumerate.
 *
 * Nothing here builds markup. Rows, cards and arrows are cloned from `<template>`s emitted by
 * the components that own them, which is what keeps their Astro scope attribute — an element
 * created with `document.createElement` would arrive without it and render unstyled — and what
 * keeps the markup in exactly one home.
 *
 * The one thing this module decides is that a control cloned into a fresh row is **hidden unless
 * the reader is an ops lead**: the server cannot make that call (it renders from a cookie that
 * carries a token, not a capability), so the reveal travels with the paint rather than being
 * re-applied by somebody afterwards.
 */
import { cloneTemplate, setField } from "../../lib/dom/patch";
import { applyTone } from "../projects/dom";
import { categoryLabel } from "../projects/tone";
import { plural } from "./authoring";
import { GRAPH, ROLE_NOTE } from "./graph";
import type {
  ArcView,
  EdgeRow,
  NodeView,
  StateChip,
  StateRow,
  WorkflowView,
} from "./graph";

/** What a paint needs to know about the reader, beyond the graph itself. */
export interface PaintOptions {
  /** Whether the editing controls are revealed; the API refuses the writes regardless. */
  readonly opsLead: boolean;
}

/** Shows or hides an element, tolerating the ones a template happens not to carry. */
function toggle(scope: ParentNode, selector: string, visible: boolean): void {
  const element = scope.querySelector<HTMLElement>(selector);
  if (element !== null) element.hidden = !visible;
}

/** Repaints a `StateTag` in place: its tone, its colour and its word, together. */
function paintTag(scope: ParentNode, hook: string, chip: StateChip): void {
  const tag = scope.querySelector<HTMLElement>(`[data-state-tag="${hook}"]`);
  if (tag !== null) {
    applyTone(tag, { className: chip.toneClass, solid: chip.toneSolid });
  }
  setField(scope, hook, chip.state.label);
}

/**
 * Applies a whole workflow to its section.
 *
 * @param section - The `[data-workflow]` element, server-rendered or cloned from the page's
 *   blank template.
 * @param view - The graph as the model reads it; every field on screen comes from here.
 * @param options - What the reader may do.
 */
export function paintWorkflow(
  section: HTMLElement,
  view: WorkflowView,
  options: PaintOptions,
): void {
  section.dataset["code"] = view.code;
  section.dataset["appliesTo"] = view.appliesTo;
  section.dataset["name"] = view.name;
  section.dataset["active"] = String(view.isActive);

  // The heading owns the section's accessible name, so its id and the reference to it are set
  // together — a cloned section would otherwise inherit the blank template's empty id and two
  // sections would answer to the same name.
  const headingId = `wf-${view.code}`;
  const heading = section.querySelector<HTMLElement>("h2");
  if (heading !== null) heading.id = headingId;
  section.setAttribute("aria-labelledby", headingId);

  setField(section, "wf-name", view.name);
  setField(section, "wf-applies", view.appliesToLabel.toLowerCase());
  setField(section, "wf-code", view.code);
  setField(section, "wf-state-count", String(view.nodes.length));
  setField(section, "wf-state-noun", plural(view.nodes.length, "estado"));
  setField(section, "wf-move-count", String(view.rows.length));
  setField(section, "wf-move-noun", plural(view.rows.length, "movimiento"));

  paintMarks(section, view);
  paintActions(section, view, options);
  paintGraph(section, view);
  paintStates(section, view, options);
  paintMoves(section, view, options);
}

/** The header chips: what this graph is, and what work resolves to it. */
function paintMarks(section: HTMLElement, view: WorkflowView): void {
  toggle(section, '[data-mark="default"]', view.isDefault);
  toggle(section, '[data-mark="inactive"]', !view.isActive);
  toggle(section, '[data-mark="no-types"]', view.engagementTypes.length === 0);

  const marks = section.querySelector<HTMLElement>(".marks");
  if (marks === null) return;
  for (const item of marks.querySelectorAll("[data-engagement-type]")) {
    item.remove();
  }
  // Before the "sin tipo asignado" chip, which is the marks list's last entry: appending would
  // read as "sin tipo asignado, comercial, interno".
  const anchor = marks.querySelector('[data-mark="no-types"]')?.closest("li");
  for (const type of view.engagementTypes) {
    const item = cloneTemplate(section, "engagement-chip");
    if (item === null) continue;
    setField(item, "type-label", type.label);
    const chip = item.querySelector<HTMLElement>(".chip");
    if (chip !== null && type.color !== null && type.color !== undefined) {
      applyTone(chip, { className: "tone-data", solid: type.color });
    }
    marks.insertBefore(item, anchor ?? null);
  }
}

/** The header's controls: what they say, what they are for, and when they are dead. */
function paintActions(
  section: HTMLElement,
  view: WorkflowView,
  options: PaintOptions,
): void {
  setField(section, "wf-toggle", view.isActive ? "Desactivar" : "Activar");
  toggle(section, '[data-icon="deactivate"]', view.isActive);
  toggle(section, '[data-icon="activate"]', !view.isActive);

  const canAddTransition = view.nodes.length >= 2;
  const addMove = section.querySelector<HTMLButtonElement>(
    '[data-wf-action="add-transition"]',
  );
  const hint = section.querySelector<HTMLElement>("[data-move-hint]");
  if (addMove !== null) {
    addMove.disabled = !canAddTransition;
    const hintId = `wf-${view.code}-no-moves`;
    if (hint !== null) hint.id = hintId;
    if (canAddTransition) addMove.removeAttribute("aria-describedby");
    else addMove.setAttribute("aria-describedby", hintId);
  }
  if (hint !== null) hint.hidden = canAddTransition;

  // The empty-lifecycle invitation carries a second `add-state` button, so the
  // focus keys are rewritten only inside the actions group: two controls sharing
  // one key would restore focus to whichever the DOM found first, which after
  // adding the first state is the one that just got hidden.
  toggle(section, "[data-empty-arm]", view.nodes.length === 0);
  for (const action of section.querySelectorAll<HTMLElement>(
    "[data-wf-actions] [data-wf-action]",
  )) {
    const name = action.dataset["wfAction"] ?? "";
    action.dataset["focusKey"] = `wf:${view.code}:${name}`;
  }
  revealControls(section, options.opsLead);
}

/**
 * Reveals or hides every ops-lead control inside `scope`, cloned rows included.
 *
 * Two hooks rather than one because they mark different things: `[data-wf-actions]` is a group
 * of controls inside a repainted row, and `[data-ops-lead]` is a single standing control the
 * page rendered once. Both are hidden in the markup, so a member who is not an ops lead never
 * sees a control flash into view and disappear.
 */
export function revealControls(scope: ParentNode, opsLead: boolean): void {
  for (const group of scope.querySelectorAll<HTMLElement>(
    "[data-wf-actions], [data-ops-lead]",
  )) {
    group.hidden = !opsLead;
  }
}

/** The drawing: the canvas box, the arrows and the cards, all from the same model. */
function paintGraph(section: HTMLElement, view: WorkflowView): void {
  toggle(section, "[data-graph-arm]", view.nodes.length > 0);

  const canvas = section.querySelector<HTMLElement>("[data-canvas]");
  if (canvas !== null) {
    canvas.style.setProperty("--graph-w", `${view.width}px`);
    canvas.style.setProperty("--graph-h", `${view.height}px`);
    canvas.style.setProperty("--node-w", `${GRAPH.nodeWidth}px`);
    canvas.style.setProperty("--node-h", `${GRAPH.nodeHeight}px`);
  }

  paintArcs(section, view);

  const list = section.querySelector<HTMLElement>("[data-nodes]");
  if (list !== null) {
    const fragment = document.createDocumentFragment();
    for (const node of view.nodes) fragment.append(...nodeCard(section, node));
    list.replaceChildren(fragment);
  }

  const captions = section.querySelector<HTMLElement>("[data-captions]");
  if (captions !== null) {
    const fragment = document.createDocumentFragment();
    for (const arc of view.arcs) fragment.append(...caption(section, arc));
    captions.replaceChildren(fragment);
  }
}

/** One state card, positioned and toned; an empty list when the template is absent. */
function nodeCard(section: HTMLElement, node: NodeView): HTMLElement[] {
  const card = cloneTemplate(section, "graph-node");
  if (card === null) return [];
  card.dataset["role"] = node.role;
  card.style.setProperty("--x", `${node.x}px`);
  card.style.setProperty("--y", `${node.y}px`);
  applyTone(card, { className: node.toneClass, solid: node.toneSolid });
  // `applyTone` writes the tone class onto the card, which also carries `.node` and `.card`;
  // both survive because it only removes the tone set.
  setField(card, "node-label", node.state.label);
  setField(card, "node-code", node.state.code);
  const note = ROLE_NOTE[node.role];
  setField(card, "node-note", note ?? "");
  toggle(card, "[data-field='node-note']", note !== null);
  return [card];
}

/** One arrow caption, at the apex of its arc. */
function caption(section: HTMLElement, arc: ArcView): HTMLElement[] {
  const item = cloneTemplate(section, "graph-caption");
  if (item === null) return [];
  item.style.setProperty("--x", `${arc.labelX}px`);
  item.style.setProperty("--y", `${arc.labelY}px`);
  setField(item, "caption-label", arc.label);
  toggle(item, "[data-field='caption-mark']", arc.requiresReason);
  item
    .querySelector(".chip")
    ?.classList.toggle("tone-ambar", arc.requiresReason);
  return [item];
}

/**
 * The arrows themselves.
 *
 * The `<path>` is cloned out of a `<svg>` inside the template rather than created, for two
 * reasons that both bite: an element built with `createElement` would be an unknown HTML tag
 * instead of an SVG one, and one built with `createElementNS` would arrive without the
 * component's scope attribute and draw as an unstroked black blob.
 */
function paintArcs(section: HTMLElement, view: WorkflowView): void {
  const svg = section.querySelector("[data-arcs]");
  if (!(svg instanceof SVGSVGElement)) return;
  svg.setAttribute("viewBox", `0 0 ${view.width} ${view.height}`);
  svg.setAttribute("width", String(view.width));
  svg.setAttribute("height", String(view.height));

  const marker = svg.querySelector("[data-arrow-marker]");
  if (marker !== null) marker.id = view.arrowId;

  const group = svg.querySelector("[data-arc-group]");
  if (group === null) return;
  const fragment = document.createDocumentFragment();
  for (const arc of view.arcs) {
    const path = cloneArcPath(section);
    if (path === null) continue;
    path.setAttribute("d", arc.path);
    path.dataset["direction"] = arc.direction;
    path.setAttribute("marker-end", `url(#${view.arrowId})`);
    fragment.append(path);
  }
  group.replaceChildren(fragment);
}

/** The `<path>` of the arc template, or `null` when the section carries no template. */
function cloneArcPath(section: ParentNode): SVGPathElement | null {
  const template = section.querySelector('template[data-template="graph-arc"]');
  if (!(template instanceof HTMLTemplateElement)) return null;
  const path = template.content.querySelector("path");
  if (path === null) return null;
  const clone = path.cloneNode(true);
  return clone instanceof SVGPathElement ? clone : null;
}

/** The states table, and the empty card that stands in for it. */
function paintStates(
  section: HTMLElement,
  view: WorkflowView,
  options: PaintOptions,
): void {
  setField(section, "states-caption", view.name);
  toggle(section, "[data-states-card]", view.states.length > 0);
  toggle(section, "[data-states-empty]", view.states.length === 0);

  const body = section.querySelector<HTMLElement>("[data-state-rows]");
  if (body === null) return;
  const fragment = document.createDocumentFragment();
  for (const state of view.states) {
    const row = cloneTemplate(section, "state-row");
    if (row === null) continue;
    paintStateRow(row, state, options);
    fragment.append(row);
  }
  body.replaceChildren(fragment);
}

/** One state as its row reads it, controls included. */
function paintStateRow(
  row: HTMLElement,
  state: StateRow,
  options: PaintOptions,
): void {
  const code = state.state.code;
  row.dataset["code"] = code;
  row.dataset["stateLabel"] = state.state.label;
  row.dataset["category"] = state.state.category;
  row.dataset["color"] = state.state.color ?? "";
  row.dataset["order"] = String(state.order);
  row.dataset["active"] = String(state.isActive);
  row.dataset["canRetire"] = String(state.canRetire);
  row.dataset["records"] = String(state.recordCount);

  setField(row, "state-order", String(state.order));
  setField(row, "state-code", code);
  setField(row, "state-category", categoryLabel(state.state.category));
  setField(row, "state-records", String(state.recordCount));
  setField(row, "state-records-noun", plural(state.recordCount, "registro"));
  paintTag(row, "state-label", state);

  toggle(row, '[data-mark="initial"]', state.isInitial);
  toggle(row, '[data-mark="final"]', state.isTerminal);
  toggle(row, '[data-mark="terminal"]', state.role === "terminal");
  toggle(row, '[data-mark="isolated"]', state.role === "isolated");
  toggle(row, '[data-mark="retired"]', !state.isActive);
  toggle(row, '[data-mark="records"]', state.recordCount > 0);

  const retire = row.querySelector<HTMLButtonElement>(
    '[data-state-action="retire"]',
  );
  if (retire !== null) {
    retire.hidden = !state.isActive;
    retire.disabled = !state.canRetire;
    // The refusal is on the control before it is pressed, and it is the count that makes it
    // actionable: "no se puede" is not something anybody can do anything about.
    if (state.canRetire) retire.removeAttribute("title");
    else {
      retire.title = `${state.recordCount} ${plural(state.recordCount, "registro")} ${state.recordCount === 1 ? "está" : "están"} en este estado`;
    }
  }
  toggle(row, '[data-state-action="restore"]', !state.isActive);

  for (const button of row.querySelectorAll<HTMLElement>(
    "[data-state-action]",
  )) {
    button.dataset["focusKey"] =
      `state:${code}:${button.dataset["stateAction"] ?? ""}`;
  }
  revealControls(row, options.opsLead);
}

/** The moves table, its empty card, and the two notes a table of edges cannot carry. */
function paintMoves(
  section: HTMLElement,
  view: WorkflowView,
  options: PaintOptions,
): void {
  setField(section, "moves-caption", view.name);
  toggle(section, "[data-moves-card]", view.rows.length > 0);
  toggle(section, "[data-moves-empty]", view.rows.length === 0);

  setField(section, "terminal-labels", view.terminalLabels.join(", "));
  setField(section, "isolated-labels", view.isolatedLabels.join(", "));
  toggle(section, '[data-note="terminal"]', view.terminalLabels.length > 0);
  toggle(section, '[data-note="isolated"]', view.isolatedLabels.length > 0);

  const body = section.querySelector<HTMLElement>("[data-move-rows]");
  if (body === null) return;
  const fragment = document.createDocumentFragment();
  for (const edge of view.rows) {
    const row = cloneTemplate(section, "move-row");
    if (row === null) continue;
    paintMoveRow(section, row, edge, options);
    fragment.append(row);
  }
  body.replaceChildren(fragment);
}

/** One declared move as its row reads it, controls included. */
function paintMoveRow(
  section: HTMLElement,
  row: HTMLElement,
  edge: EdgeRow,
  options: PaintOptions,
): void {
  row.dataset["from"] = edge.fromCode;
  row.dataset["to"] = edge.toCode;
  row.dataset["moveLabel"] = edge.label;
  row.dataset["requiresReason"] = String(edge.requiresReason);
  row.dataset["requiresFields"] = edge.requiresFields.join(",");

  paintTag(row, "row-from", edge.from);
  paintTag(row, "row-to", edge.to);
  setField(row, "row-label", edge.label);

  toggle(row, '[data-mark="reason"]', edge.requiresReason);
  toggle(row, '[data-mark="no-reason"]', !edge.requiresReason);
  toggle(row, '[data-mark="no-fields"]', edge.requiresFields.length === 0);

  const fields = row.querySelector<HTMLElement>("[data-fields]");
  if (fields !== null) {
    fields.hidden = edge.requiresFields.length === 0;
    const chips = document.createDocumentFragment();
    for (const name of edge.requiresFields) {
      const chip = cloneTemplate(section, "field-chip");
      if (chip === null) continue;
      chip.textContent = name;
      chips.append(chip);
    }
    fields.replaceChildren(chips);
  }

  for (const button of row.querySelectorAll<HTMLElement>(
    "[data-move-action]",
  )) {
    button.dataset["focusKey"] =
      `move:${edge.fromCode}->${edge.toCode}:${button.dataset["moveAction"] ?? ""}`;
  }
  revealControls(row, options.opsLead);
}
