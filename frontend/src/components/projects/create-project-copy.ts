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
