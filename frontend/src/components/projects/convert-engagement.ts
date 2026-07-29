/**
 * Moving a project from one engagement type to another: what that would do, said as a
 * sentence, and then doing it.
 *
 * **Why a sentence and not a diagram.** The act is opaque from the outside — an operator who
 * changes "Diagnóstico" to "Mantenimiento o recurrente" is changing which lifecycle governs the
 * project and how heavily the queue weighs it, and neither of those is visible in the chip they
 * pressed. The authoring surface's state graph (`components/workflows/StateGraph.astro`) is the
 * wrong answer here: handing an operator a state machine is precision, not clarity. So this
 * module composes what the conversion *means* into paragraphs, and the operator confirms them.
 *
 * **The rule the copy must never contradict** (`apps/workflow/services/reassignment.py`):
 * repointing a record at another lifecycle *never moves it*. It keeps the state `code` it stands
 * on and lands on the equivalent node of the target graph; when the target has no active state
 * with that code the server refuses with `409 conflicting_state`, naming the states the target
 * does offer. {@link buildConversions} asks that same question against the same rows before the
 * operator presses, so the refusal is read rather than provoked — and it stays a refusal: there
 * is deliberately no landing-state picker, because choosing one would be an unvalidated
 * `workflow_state` write (CLAUDE.md rule 2).
 *
 * **Nothing here is keyed by an engagement code.** The menu, the paragraphs and the weights are
 * all derived from the taxonomy the API published, so a fourth type added in the admin gets its
 * own "Convertir en …" entry with no frontend change (`docs/standards/PATTERNS_FRONTEND.md` §7).
 */

import { patchProject } from "../../lib/api/client";
import { cloneTemplate, setField } from "../../lib/dom/patch";
import { shake } from "../../lib/motion/spring";
import { toast } from "../../lib/toast";
import { mountMenus } from "../../lib/ui/menu";
import { assertNever } from "../../lib/view-state";
import { resolveWorkflow, spanishList } from "../board/board-model";
import { setPending } from "./dom";
import { failureCopy } from "./messages";
import type {
  TaxonomyRef,
  WorkflowCatalog,
  WorkflowRef,
  WorkflowShape,
} from "../../lib/api/domain";

/** `Workflow.applies_to` for the graphs that govern projects; compared, never rendered. */
const PROJECT_WORKFLOW = "PROJECT";

/** One stretch of a sentence; `emphasis` is the name the eye should land on. */
export interface Run {
  readonly text: string;
  readonly emphasis: boolean;
}

/**
 * One paragraph of the confirmation.
 *
 * `tone` is `warning` for the arm that refuses: the operator has to see, before they reach the
 * button, that this conversion is the one that cannot be made yet.
 */
export interface Paragraph {
  readonly tone: "body" | "warning";
  readonly runs: readonly Run[];
}

/**
 * Everything the dialog says about converting into one engagement type.
 *
 * Rendered entirely by the server: the project read and the workflow catalog are both already on
 * the page, so the dialog has no loading arm and no request of its own to make before it can
 * explain itself.
 */
export interface ConversionPlan {
  /** `EngagementType.code` — what the PATCH sends, and the key of this plan's template. */
  readonly code: string;
  /** The operator's own label for the destination type; every sentence names this. */
  readonly label: string;
  /** The explanation, in reading order. */
  readonly paragraphs: readonly Paragraph[];
  /**
   * Whether the server could accept this conversion.
   *
   * `false` only for the refusal this client can predict — the destination lifecycle has no
   * active state carrying the code the project stands on. The confirm renders visibly dead with
   * the reason above it, rather than sending a request whose 409 is already knowable.
   */
  readonly isAllowed: boolean;
}

/** The project as its own read describes it, plus the vocabularies the plan is built from. */
export interface ConversionContext {
  /** Rendered in the first sentence, so the operator is converting a named project. */
  readonly projectName: string;
  /** The engagement type in force; it is the one type the menu does not offer. */
  readonly current: TaxonomyRef;
  /** Every engagement type the catalog publishes, in the catalog's order. */
  readonly engagementTypes: readonly TaxonomyRef[];
  /** The lifecycle in force and whether somebody chose it for this project. */
  readonly workflow: WorkflowRef;
  /** The state the project stands on: the code the compatibility check compares. */
  readonly stateCode: string;
  /** The same state as the operator reads it; every sentence names this, never the code. */
  readonly stateLabel: string;
  /** `GET /api/v1/workflows`, or `null` when that read failed — an honest `unknown` arm. */
  readonly catalog: WorkflowCatalog | null;
}

/**
 * The priority weight of one engagement type, or `null` when it is not knowable here.
 *
 * The seam stays a seam even though the wire now carries the number. `GET /api/v1/catalog` sends
 * engagement types as `EngagementTypeRef` — `TaxonomyRef` plus `weight` — but a conversion plan can
 * be built from a bare `TaxonomyRef` too (the project's own `engagement_type` is one), so the
 * reader is passed in rather than assumed. It also keeps the "we cannot say" arm reachable and
 * tested: when the catalog read fails there is no weight for anybody, and the sentence must go
 * unsaid rather than be guessed.
 *
 * `weight` arrives as a **string**, the way every decimal on this API does. Parsing belongs to the
 * lookup, not here.
 */
export type EngagementWeightLookup = (type: TaxonomyRef) => number | null;

/** The lookup to pass when the weight is not knowable: the sentence stays unsaid. */
export const NO_ENGAGEMENT_WEIGHTS: EngagementWeightLookup = () => null;

/**
 * Reads the multiplier off the catalog's own entries, by code.
 *
 * By code and not by identity because the type in force reaches the plan builder as the project's
 * `engagement_type` — a plain `TaxonomyRef` from a different read — and comparing objects would
 * silently return `null` for the one type whose weight the sentence needs most.
 *
 * A non-numeric or absent weight yields `null`, which reads as "we cannot say" rather than as
 * zero — a zero multiplier would announce that the project drops out of the ranking entirely.
 *
 * @param types - `engagement_types` as `GET /api/v1/catalog` published them.
 * @returns A lookup over those entries.
 */
export function engagementWeights(
  types: readonly { readonly code: string; readonly weight: string }[],
): EngagementWeightLookup {
  const byCode = new Map(types.map((type) => [type.code, Number(type.weight)]));
  return (type) => {
    const weight = byCode.get(type.code);
    return weight === undefined || !Number.isFinite(weight) ? null : weight;
  };
}

/**
 * Builds one plan per engagement type the project could be converted into.
 *
 * The type in force is left out — "Convertir en Diagnóstico" on a Diagnóstico is a no-op the
 * server answers by changing nothing — and so is nothing else: a type whose destination lifecycle
 * cannot receive this project is still offered, and still explains why it will not go through,
 * because a control that disappears teaches the operator the capability does not exist
 * (`docs/standards/FRONTEND.md` §8).
 *
 * @param context - The project's own read plus the two vocabularies.
 * @param weightOf - How to read an engagement type's priority multiplier; see
 *   {@link EngagementWeightLookup}. Defaults to "not knowable", which omits the sentence.
 * @returns One plan per other engagement type, in the catalog's order. Empty when the catalog
 *   published a single type or could not be read at all — the chip then renders visibly dead.
 */
export function buildConversions(
  context: ConversionContext,
  weightOf: EngagementWeightLookup = NO_ENGAGEMENT_WEIGHTS,
): readonly ConversionPlan[] {
  return context.engagementTypes
    .filter((type) => type.code !== context.current.code)
    .map((type) => planFor(type, context, weightOf));
}

function planFor(
  target: TaxonomyRef,
  context: ConversionContext,
  weightOf: EngagementWeightLookup,
): ConversionPlan {
  const destination = destinationShape(target, context);
  const paragraphs: Paragraph[] = [
    lifecycleParagraph(target, context, destination),
  ];

  const landing = landingParagraphs(context, destination);
  paragraphs.push(...landing.paragraphs);

  const weight = weightParagraph(target, context, weightOf);
  if (weight !== null) paragraphs.push(weight);

  return {
    code: target.code,
    label: target.label,
    paragraphs,
    isAllowed: landing.isAllowed,
  };
}

/**
 * Which lifecycle would govern the project after the conversion.
 *
 * `null` in three different situations that must not be conflated in the copy: the catalog could
 * not be read, this deployment publishes no project graph at all, or somebody pinned a graph to
 * this project — in which case the engagement type decides nothing about its lifecycle and the
 * conversion cannot change it. The caller tells them apart; this only resolves.
 */
function destinationShape(
  target: TaxonomyRef,
  context: ConversionContext,
): WorkflowShape | null {
  if (context.catalog === null) return null;
  if (context.workflow.source === "DIRECT") return null;
  return resolveWorkflow(context.catalog, PROJECT_WORKFLOW, target.code);
}

/** The opening sentence: what changes, and — truthfully — what does not. */
function lifecycleParagraph(
  target: TaxonomyRef,
  context: ConversionContext,
  destination: WorkflowShape | null,
): Paragraph {
  const name = `«${context.projectName}»`;

  if (context.workflow.source === "DIRECT") {
    // The binding ladder is not consulted for a project somebody pinned a graph to, so claiming
    // the cycle changes would be a lie the operator finds out about afterwards.
    return body([
      plain(`${name} seguirá el ciclo de `),
      strong(context.workflow.name),
      plain(
        `, porque alguien lo eligió para este proyecto. Cambia el tipo de encargo, no sus estados.`,
      ),
    ]);
  }

  if (destination === null) {
    return body([
      plain(`${name} pasa a ser un `),
      strong(target.label),
      plain(
        `. No pudimos leer los flujos de trabajo, así que aquí no podemos decirte si además cambia de ciclo: lo resuelve el servidor al guardar.`,
      ),
    ]);
  }

  if (destination.code === context.workflow.code) {
    return body([
      plain(`${name} pasa a ser un `),
      strong(target.label),
      plain(` y sigue con el mismo ciclo, `),
      strong(destination.name),
      plain(`: cambia cómo se contrata, no los estados por los que pasa.`),
    ]);
  }

  return body([
    plain(`${name} dejará de seguir el ciclo de `),
    strong(context.workflow.name),
    plain(` y pasará al de `),
    strong(destination.name),
    plain(`.`),
  ]);
}

/**
 * Where the project would stand afterwards, and what it could do from there.
 *
 * The same question `validate_reassignment` asks, against the same rows: only `is_active` states
 * of the destination count, because a retired node is not a landing site either.
 */
function landingParagraphs(
  context: ConversionContext,
  destination: WorkflowShape | null,
): { readonly paragraphs: readonly Paragraph[]; readonly isAllowed: boolean } {
  if (destination === null || destination.code === context.workflow.code) {
    return { paragraphs: [], isAllowed: true };
  }

  const active = destination.states.filter((state) => state.is_active);
  const landing = active.find((state) => state.code === context.stateCode);

  if (landing === undefined) {
    const offers = active.map((state) => state.label);
    return {
      paragraphs: [
        {
          tone: "warning",
          runs: [
            plain(`«${context.stateLabel}» no existe en ese ciclo. `),
            plain(moveFirstSentence(offers)),
          ],
        },
      ],
      isAllowed: false,
    };
  }

  const moves = active
    .filter((state) =>
      destination.transitions.some(
        (edge) =>
          edge.from_state === context.stateCode && edge.to_state === state.code,
      ),
    )
    .map((state) => state.label);

  return {
    paragraphs: [
      body([
        plain(`Hoy está en `),
        strong(landing.label),
        plain(`, que ese ciclo también tiene. Se queda ahí. `),
        plain(movesSentence(moves)),
      ]),
    ],
    isAllowed: true,
  };
}

/**
 * The way out of a refusal, naming states the destination actually offers.
 *
 * Two ways forward exist and only one of them belongs in a confirmation dialog: move the project
 * first. Adding the missing state to that graph is an authoring act on `/workflows`, and pointing
 * at it from here would send an operator who wanted to convert a project into editing a lifecycle.
 */
function moveFirstSentence(offers: readonly string[]): string {
  if (offers.length === 0) {
    return "Ese ciclo todavía no tiene estados, así que no hay dónde ponerlo.";
  }
  if (offers.length === 1) {
    return `Muévelo a ${offers[0] ?? ""} y vuelve a intentarlo.`;
  }
  return `Muévelo a uno de los suyos — ${spanishList(offers)} — y vuelve a intentarlo.`;
}

/** What the project could do from where it lands; an isolated state says so. */
function movesSentence(moves: readonly string[]): string {
  if (moves.length === 0) {
    return "Desde ahí, ese ciclo todavía no ofrece ningún movimiento.";
  }
  return `A partir de ahora sus movimientos serán: ${spanishList(moves)}.`;
}

/**
 * What the conversion does to the project's place in the queue.
 *
 * `null` when the two weights are equal — a sentence about a number that did not move is noise —
 * and when either is not knowable, which is every conversion until the catalog publishes the
 * multiplier (see {@link EngagementWeightLookup}).
 */
function weightParagraph(
  target: TaxonomyRef,
  context: ConversionContext,
  weightOf: EngagementWeightLookup,
): Paragraph | null {
  const from = weightOf(context.current);
  const to = weightOf(target);
  if (from === null || to === null || from === to) return null;
  const direction = to > from ? "subirá" : "bajará";
  return body([
    plain(`Su peso de prioridad pasa de `),
    strong(decimal(from)),
    plain(` a `),
    strong(decimal(to)),
    plain(`, así que ${direction} en la cola.`),
  ]);
}

/** "1,10" — the multiplier as this interface writes numbers. */
function decimal(value: number): string {
  return new Intl.NumberFormat("es", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function plain(text: string): Run {
  return { text, emphasis: false };
}

function strong(text: string): Run {
  return { text, emphasis: true };
}

function body(runs: readonly Run[]): Paragraph {
  return { tone: "body", runs };
}

// --- The control ------------------------------------------------------------

/** Why the chip cannot be pressed, when the catalog left nothing to convert into. */
export const NO_CONVERSIONS =
  "No hay otro tipo de encargo al que convertirlo. Si crees que debería haberlo, vuelve a cargar la página.";

/**
 * Mounts the engagement chip, its menu and the dialog it opens.
 *
 * Nothing is painted before the server has answered, and the answer is not painted at all: a
 * conversion changes the lifecycle in force, the transition bar, the score and the risk flags at
 * once, so the page is re-read (`navigate`) instead of being patched from a guess. A refusal
 * stays inside the dialog, where the operator is looking.
 *
 * @param root - The element carrying `data-convert-engagement`.
 * @returns The teardown; drops every listener and closes the dialog.
 */
export function mountConvertEngagement(root: HTMLElement): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  const dialog = root.querySelector<HTMLDialogElement>("[data-convert-dialog]");
  const form = root.querySelector<HTMLFormElement>("[data-convert-form]");
  const bodyNode = root.querySelector<HTMLElement>("[data-convert-body]");
  const submit = root.querySelector<HTMLButtonElement>(
    "[data-action='submit-convert']",
  );
  const projectCode = root.dataset["projectCode"] ?? "";

  /** The type the open dialog is confirming; `null` while it is closed. */
  let chosen: { readonly code: string; readonly label: string } | null = null;

  const open = (code: string, label: string): void => {
    if (dialog === null || bodyNode === null) return;
    const plan = cloneTemplate(root, `conversion-${code}`);
    if (plan === null) return;

    chosen = { code, label };
    hideRefusal();
    bodyNode.replaceChildren(plan);
    setField(root, "convert-target", label);
    if (submit !== null) {
      // The refusal this client predicted is already in the body; the button agrees with it
      // rather than inviting a press the server would answer with a 409.
      submit.disabled = plan.dataset["allowed"] !== "true";
    }
    dialog.showModal();
  };

  const hideRefusal = (): void => {
    const box = root.querySelector<HTMLElement>("[data-convert-refusal]");
    if (box === null) return;
    box.hidden = true;
    box.removeAttribute("role");
  };

  const showRefusal = (title: string, detail: string): void => {
    const box = root.querySelector<HTMLElement>("[data-convert-refusal]");
    if (box === null) return;
    setField(box, "convert-error-title", title);
    setField(box, "convert-error-detail", detail);
    box.hidden = false;
    // Announced only once it carries words: a live region revealed empty reads as an alert
    // with nothing in it.
    box.setAttribute("role", "alert");
  };

  const send = async (): Promise<void> => {
    const target = chosen;
    if (target === null || submit === null || projectCode === "") return;

    hideRefusal();
    setPending(submit, true);
    // Built key by key: a PATCH distinguishes an absent key from an explicit `null`, and
    // spreading a form over the body is how an untouched field gets cleared.
    const result = await patchProject(projectCode, {
      engagement_type: target.code,
    });
    setPending(submit, false);

    if (!result.ok) {
      void shake(submit);
      const copy = failureCopy(result.error);
      showRefusal(copy.title, copy.detail);
      return;
    }

    dialog?.close();
    // Reconciled with the server rather than patched: the conversion can change the lifecycle in
    // force, and with it the transition bar, the score and the risk flags. The client router
    // module is imported here and not at the top because this file is also evaluated during the
    // server render, where a browser-only module has no business being loaded.
    const landed = result.data.engagement_type.label;
    const { navigate } = await import("astro:transitions/client");
    await navigate(location.href).catch(() => undefined);
    toast({
      kind: "success",
      title: "Tipo de encargo actualizado",
      detail: `«${result.data.name}» es ahora un ${landed}.`,
    });
  };

  const offMenu = mountMenus(root, "[data-engagement-control]", {
    fill: async () => {
      // The options are the taxonomy the server already rendered; there is nothing to read.
    },
    choose: async (_control, item) => {
      open(item.dataset["value"] ?? "", item.dataset["label"] ?? "");
    },
  });

  root.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (target.closest("[data-action='close-convert']") !== null) {
        dialog?.close();
      }
    },
    { signal },
  );

  form?.addEventListener(
    "submit",
    (event) => {
      // Without this, `<form method="dialog">` closes on submit and a refusal would have
      // nowhere to be read.
      event.preventDefault();
      void send();
    },
    { signal },
  );

  dialog?.addEventListener(
    "close",
    () => {
      chosen = null;
    },
    { signal },
  );

  return () => {
    controller.abort();
    offMenu();
    dialog?.close();
  };
}

/**
 * Narrows a paragraph's tone onto the class the stylesheet knows.
 *
 * Exhaustive on purpose: a tone added to {@link Paragraph} without a class here is a build
 * failure rather than an unstyled sentence.
 */
export function paragraphClass(tone: Paragraph["tone"]): string {
  switch (tone) {
    case "body":
      return "body-text";
    case "warning":
      return "body-text warning";
    default:
      return assertNever(tone);
  }
}
