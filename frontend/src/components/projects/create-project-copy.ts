/**
 * Spanish copy for the create-project surface, in one module because three files
 * say it: the page renders the trigger (dead or alive), the dialog renders the
 * fields and their consequences, and the island renders the failures.
 *
 * The same rule as `messages.ts` decides what may live here: nothing keyed by an
 * operator-editable vocabulary. Clients, engagement types, project types, stages
 * and currencies arrive with their own `label` and are rendered as they came.
 * What is written here is the product's own voice about *its* rules — that a
 * project needs a counterparty, that an undated project is born in risk — and
 * those change by decision, not by a row somebody added this morning.
 */

/**
 * Why "Nuevo proyecto" is dead when the directory came back empty.
 *
 * The sentence names the cause and the way out, because a dead button with no
 * explanation reads as a broken screen and the operator keeps pressing it
 * (`docs/standards/FRONTEND.md` §10). It is never hidden: a control that
 * disappears teaches that the capability does not exist.
 */
export const CREATE_PROJECT_NO_CLIENTS =
  "No hay clientes registrados. Un proyecto necesita un cliente; primero hay que dar de alta uno.";

/** Why it is dead when `GET /api/v1/clients` itself failed. */
export const CREATE_PROJECT_CLIENTS_FAILED =
  "No pudimos leer los clientes, y un proyecto necesita uno. Vuelve a cargar la página.";

/**
 * Why it is dead when the catalog failed, or offers no engagement type.
 *
 * One sentence for both, because the operator's next move is the same and the
 * difference — nothing configured versus nothing readable — is a distinction the
 * screen cannot make honestly from a failed read.
 */
export const CREATE_PROJECT_ENGAGEMENT_FAILED =
  "No pudimos leer los tipos de encargo, y el tipo de encargo decide el flujo del proyecto. Vuelve a cargar la página.";

/**
 * Why the owner picker offers only "Sin responsable".
 *
 * A failed roster does **not** block creation: the owner is optional on the wire,
 * and refusing to register a project because a second read failed would cost the
 * operation a project to save a field that can be filled in ten seconds later.
 */
export const CREATE_PROJECT_TEAM_FAILED =
  "No pudimos cargar el equipo. Podrás asignar responsable desde el proyecto.";

/** Why the form refuses to submit: the four answers the operator owes it. */
export const CREATE_PROJECT_INCOMPLETE =
  "Escribe un nombre y elige cliente, tipo de encargo y responsable.";

/** Why a typed business value was refused before any request left. */
export const CREATE_PROJECT_VALUE_INVALID =
  "El valor de negocio tiene que ser un número positivo. Déjalo vacío si todavía no se sabe.";

/**
 * Why the creation failed after the client already retried it once.
 *
 * The code is allocated inside the creating transaction, so two people pressing
 * Crear in the same instant is a real race and nothing was written by the loser.
 * Pressing again is therefore the whole recovery, and saying so beats a generic
 * "algo salió mal" that leaves the operator wondering whether it half-worked.
 */
export const CREATE_PROJECT_CODE_RACE =
  "Otro miembro creó un proyecto en el mismo instante. Vuelve a pulsar Crear.";

/** What an undated project costs, said next to the field that avoids it. */
export const CREATE_PROJECT_NO_TARGET_HINT =
  "Sin fecha objetivo el proyecto entra marcado en riesgo.";

/** What a project with no next step costs, said next to the field. */
export const CREATE_PROJECT_NO_NEXT_STEP_HINT =
  "Sin próximo paso el proyecto aparece hoy mismo en «sin próximo paso claro».";

/** What an unvalued project costs: one signal fewer, not a lower score. */
export const CREATE_PROJECT_NO_VALUE_HINT =
  "Sin valor de negocio, la prioridad se calcula sin esa señal.";

/**
 * The warning shown when the portfolio already holds this name for this client.
 *
 * `Project.name` is not unique and two engagements for the same counterparty can
 * legitimately share a name, so this warns and never blocks: the operator is the
 * only one who knows whether it is a duplicate or a second phase.
 *
 * @param name - What the operator typed, quoted back verbatim.
 */
export function createProjectDuplicateWarning(name: string): string {
  return `Ya existe «${name}» para ese cliente. ¿Es un encargo distinto?`;
}

/* --- Creating into a band of the rail ------------------------------------- */

/**
 * The sheet's heading when a band of the board asked for the project.
 *
 * The destination is named in the head rather than implied by where the operator pressed:
 * the sheet is a right-anchored surface that covers the rail, so by the time the form is on
 * screen the band that asked for it is no longer visible.
 *
 * @param stateLabel - The band's own label, as the workflow publishes it.
 */
export function createProjectDestinationTitle(stateLabel: string): string {
  return `Nuevo proyecto en «${stateLabel}»`;
}

/**
 * What will happen after the project exists, said before it does.
 *
 * Two acts, not one — a creation and a transition — and the operator is entitled to know that
 * before pressing, because the second one can fail on its own and leave the first one standing.
 */
export function createProjectPlacementNote(
  stateLabel: string,
  transitionLabel: string,
): string {
  return `Se creará y se moverá con «${transitionLabel}» hasta «${stateLabel}».`;
}

/** What that move demands be filled in, when the operator's graph declares requirements. */
export function createProjectPlacementFields(
  stateLabel: string,
  fields: string,
): string {
  return `Para llegar a «${stateLabel}» hace falta rellenar: ${fields}.`;
}

/** Why the form refuses to submit when the landing move demands a written motive. */
export const CREATE_PROJECT_MOVE_REASON_MISSING =
  "Escribe el motivo del movimiento: ese paso no se puede dar sin él.";

/** Where the project ended up, named in every outcome — success and failure alike. */
export function createProjectLandedDetail(
  code: string,
  stateLabel: string,
): string {
  return `${code} quedó en «${stateLabel}».`;
}

/**
 * The move was still configured but the created project does not offer it.
 *
 * The graph said the edge exists; the record — which is the only authority on what it may do
 * right now — did not list it. The project exists either way, so the notice names where it is
 * and how to finish the job by hand.
 */
export const CREATE_PROJECT_MOVE_ILLEGAL_TITLE =
  "Creamos el proyecto, pero ese movimiento ya no es legal";

export function createProjectMoveIllegalDetail(
  code: string,
  stateLabel: string,
): string {
  return `${code} quedó en «${stateLabel}». Arrástralo por el carril cuando quieras moverlo.`;
}

/** The server refused the move on its own terms: a motive or a field it wanted. */
export const CREATE_PROJECT_MOVE_REFUSED_TITLE =
  "Creamos el proyecto, pero no pudimos moverlo";

/**
 * @param missing - Already-Spanish enumeration of what the server named, or `""` when it
 *   named nothing and the reason has to come from the failure's own copy.
 */
export function createProjectMoveRefusedDetail(
  code: string,
  stateLabel: string,
  missing: string,
): string {
  const tail = missing === "" ? "" : ` Falta: ${missing}.`;
  return `${createProjectLandedDetail(code, stateLabel)}${tail}`;
}

/**
 * The transition request never came back.
 *
 * Deliberately does not assert an outcome: a request that failed in transit may still have
 * been applied, and telling the operator it was not is a lie the board would contradict.
 */
export const CREATE_PROJECT_MOVE_UNKNOWN_TITLE =
  "Creamos el proyecto, pero no sabemos si se movió";

export const CREATE_PROJECT_MOVE_UNKNOWN_DETAIL =
  "No sabemos si el movimiento se aplicó. Míralo en el tablero.";
