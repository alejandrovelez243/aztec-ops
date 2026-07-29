/**
 * What the rich-text field says, in the language a person reads.
 *
 * Separate from `rich-text.ts` so the Astro component can render the empty
 * state on the server without pulling the editor — and its 250 KB of
 * ProseMirror — into a page that may never mount one.
 */

/** The named absence, shown until somebody writes a description. */
export const RICH_TEXT_PLACEHOLDER =
  "Sin descripción. Escribe aquí el contexto que el próximo lector necesita, o pulsa «/» para insertar un bloque.";

/** Inside the editor, on an empty first line. */
export const RICH_TEXT_PROMPT = "Escribe algo, o pulsa «/» para los bloques";

/** The save states, in the order they happen. */
export const RICH_TEXT_STATUS = {
  saving: "Guardando…",
  saved: "Guardado",
  failed:
    "No se pudo guardar. Sigue escribiendo: se reintenta al próximo cambio.",
} as const;
