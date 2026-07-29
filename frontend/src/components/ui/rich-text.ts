/**
 * The block editor behind {@link ./RichText.astro}: TipTap, Markdown in and out.
 *
 * **Why a library.** Everything else on this surface is hand-written vanilla,
 * and a block editor is the one place where that stops being a virtue: text
 * selection across nodes, undo that survives a paste, IME composition, and the
 * dozen ways a caret behaves at a block boundary are not incidental complexity,
 * they *are* the feature. ProseMirror has answered them; a `contenteditable`
 * written here would answer them worse and for longer.
 *
 * **The stored value is Markdown, and it is never HTML.** `html: false` on the
 * Markdown extension is the load-bearing line: with it on, a description saved
 * by one operator could carry `<script>` into another operator's session, since
 * this content is written and read by different people. With it off, the parser
 * emits ProseMirror nodes only, and ProseMirror renders from its schema — an
 * allowlist by construction. There is no `innerHTML` of user content anywhere in
 * this module, and there must never be one.
 *
 * **Saving.** Debounced, then reported. A document has no submit button, but a
 * document that saves invisibly is one nobody trusts — so every write ends in a
 * status line, including the failure, which says the work is still on screen.
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "tiptap-markdown";
import { RICH_TEXT_PROMPT, RICH_TEXT_STATUS } from "./rich-text-copy";
import { setField } from "../projects/dom";

/** How long the editor stays quiet before persisting a burst of typing. */
const SAVE_DEBOUNCE_MS = 900;

/** Performs the write; the caller owns which endpoint that is. */
export type RichTextSubmit = (
  scope: string,
  code: string,
  markdown: string,
) => Promise<boolean>;

/** One entry of the `/` menu. */
interface SlashCommand {
  readonly key: string;
  readonly title: string;
  readonly hint: string;
  readonly run: (editor: Editor) => void;
}

/**
 * The blocks a description can hold.
 *
 * Deliberately short. A `/` menu is a promise that each entry produces
 * something the *stored Markdown* can round-trip, so anything that would only
 * live in the editor's own memory has no place here.
 */
const COMMANDS: readonly SlashCommand[] = [
  {
    key: "H1",
    title: "Título",
    hint: "Encabezado grande",
    run: (e) => e.chain().focus().toggleHeading({ level: 1 }).run(),
  },
  {
    key: "H2",
    title: "Subtítulo",
    hint: "Encabezado mediano",
    run: (e) => e.chain().focus().toggleHeading({ level: 2 }).run(),
  },
  {
    key: "H3",
    title: "Apartado",
    hint: "Encabezado pequeño",
    run: (e) => e.chain().focus().toggleHeading({ level: 3 }).run(),
  },
  {
    key: "•",
    title: "Lista",
    hint: "Viñetas",
    run: (e) => e.chain().focus().toggleBulletList().run(),
  },
  {
    key: "1.",
    title: "Lista numerada",
    hint: "Pasos en orden",
    run: (e) => e.chain().focus().toggleOrderedList().run(),
  },
  {
    key: "❝",
    title: "Cita",
    hint: "Texto citado",
    run: (e) => e.chain().focus().toggleBlockquote().run(),
  },
  {
    key: "<>",
    title: "Código",
    hint: "Bloque monoespaciado",
    run: (e) => e.chain().focus().toggleCodeBlock().run(),
  },
  {
    key: "—",
    title: "Separador",
    hint: "Línea horizontal",
    run: (e) => e.chain().focus().setHorizontalRule().run(),
  },
];

/**
 * Mounts one rich-text field.
 *
 * @param root - The element carrying `data-rich-text`.
 * @returns The teardown; destroys the editor, so a screen left behind by a
 *   navigation does not keep a ProseMirror view and its listeners alive.
 */
export function mountRichText(root: HTMLElement): () => void {
  const surface = root.querySelector<HTMLElement>("[data-rich-surface]");
  const fallback = root.querySelector<HTMLElement>("[data-rich-fallback]");
  const slash = root.querySelector<HTMLElement>("[data-rich-slash]");
  if (surface === null || fallback === null || slash === null) return () => {};

  const scope = root.dataset["scope"] ?? "";
  const code = root.dataset["code"] ?? "";
  const resolved = submitFor(scope);
  if (resolved === null) return () => {};
  // Bound to a non-nullable const: narrowing an outer `let`/union does not
  // survive into the async closure below, and the alternative is the `!` the
  // house rules forbid.
  const submit: RichTextSubmit = resolved;

  // The stored Markdown is read as *text*, never as markup — see the module note.
  const initial = fallback.textContent ?? "";

  let timer = 0;
  let lastSaved = initial;
  let destroyed = false;

  // Late-bound: the menu must exist before the editor so its `handleKeyDown`
  // can be handed to ProseMirror at construction, and it needs the editor back
  // to read the selection. A getter closes that loop without a mutable `any`.
  let editorRef: Editor | null = null;
  const slashMenu = mountSlash(() => editorRef, slash);

  const editor = new Editor({
    element: surface,
    extensions: [
      StarterKit.configure({ heading: { levels: [1, 2, 3] } }),
      // `html: false` is the security boundary; see the module note.
      Markdown.configure({ html: false, transformPastedText: true }),
    ],
    content: initial,
    editorProps: {
      attributes: { "data-placeholder": RICH_TEXT_PROMPT },
      // Before any editor command: this is what lets the menu own `Enter` and
      // the arrows instead of receiving them after the caret already moved.
      handleKeyDown: (_view, event) => slashMenu.handleKeyDown(event),
    },
    onUpdate: () => {
      // The query is read from the document, after it changed — never from the
      // key that changed it, which on `keydown` has not been inserted yet.
      slashMenu.sync();
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        void save();
      }, SAVE_DEBOUNCE_MS);
    },
  });

  async function save(): Promise<void> {
    if (destroyed) return;
    const markdown = markdownOf(editor);
    // `null` means the serializer was not where it was expected. Saving `""`
    // here would quietly empty a description the operator can still see on
    // screen, so the write is skipped instead — the worst case becomes an
    // unsaved edit, never a destroyed one.
    if (markdown === null || markdown === lastSaved) return;

    status(root, "saving");
    const ok = await submit(scope, code, markdown);
    if (destroyed) return;
    if (!ok) {
      status(root, "failed");
      return;
    }
    lastSaved = markdown;
    status(root, "saved");
    const empty = root.querySelector<HTMLElement>("[data-rich-empty]");
    if (empty !== null) empty.hidden = true;
  }

  editorRef = editor;
  editor.on("blur", slashMenu.close);

  // Only now is the editor real: swapping before it mounts would blank the
  // description for the width of one frame.
  fallback.hidden = true;
  surface.hidden = false;
  const empty = root.querySelector<HTMLElement>("[data-rich-empty]");
  if (empty !== null) empty.hidden = initial !== "";

  return () => {
    destroyed = true;
    editorRef = null;
    window.clearTimeout(timer);
    editor.destroy();
  };
}

/** What `tiptap-markdown` parks on the editor's storage. */
interface MarkdownStorage {
  readonly getMarkdown: () => string;
}

function isMarkdownStorage(value: unknown): value is MarkdownStorage {
  return (
    typeof value === "object" &&
    value !== null &&
    "getMarkdown" in value &&
    typeof Reflect.get(value, "getMarkdown") === "function"
  );
}

/**
 * The document as Markdown, or `null` when the serializer is not there.
 *
 * `editor.storage` is an untyped bag that extensions write into, so the shape
 * is checked rather than asserted: the alternative is a cast that would turn a
 * dependency upgrade into a silently empty description instead of a skipped
 * save (see {@link mountRichText}'s `save`).
 */
function markdownOf(editor: Editor): string | null {
  const storage: unknown = Reflect.get(editor.storage, "markdown");
  if (!isMarkdownStorage(storage)) return null;
  const markdown: unknown = storage.getMarkdown();
  return typeof markdown === "string" ? markdown : null;
}

/** Writes the save state, and its tone, into the status line. */
function status(root: HTMLElement, state: keyof typeof RICH_TEXT_STATUS): void {
  setField(root, "rich-status", RICH_TEXT_STATUS[state]);
  const line = root.querySelector<HTMLElement>("[data-field='rich-status']");
  if (line === null) return;
  if (state === "failed") line.dataset["tone"] = "error";
  else delete line.dataset["tone"];
}

/** The `/` menu, as the editor's own key handling sees it. */
interface SlashMenu {
  /** Consumes a key while the menu is open; `false` lets the editor have it. */
  readonly handleKeyDown: (event: KeyboardEvent) => boolean;
  /** Recomputes the query after the document changed. */
  readonly sync: () => void;
  readonly close: () => void;
}

/**
 * The `/` menu: a filtered list anchored at the caret.
 *
 * **Keys are handled through ProseMirror, not through a DOM listener.** A
 * `keydown` listener added to `view.dom` runs *after* the editor's own handler,
 * so by the time it saw `Enter` the paragraph had already been split and the
 * range it then deleted was computed against a selection that no longer
 * existed — which is how choosing a block used to wipe what was written.
 * `editorProps.handleKeyDown` runs before any editor command and returns `true`
 * to consume the event, which is the only way to own `Enter` and the arrows.
 *
 * The panel is plain DOM cloned from a `<template>` rather than
 * `@tiptap/suggestion`'s renderer, for the reason every menu in this app clones:
 * an element built in TypeScript carries no scope id and renders unstyled.
 */
function mountSlash(
  getEditor: () => Editor | null,
  panel: HTMLElement,
): SlashMenu {
  let open = false;
  let index = 0;
  let matches: readonly SlashCommand[] = COMMANDS;

  // Held before the first render, because `replaceChildren` below would other-
  // wise delete the very `<template>` the rows are cloned from — the panel
  // renders once, empties itself, and every later open is a blank box.
  const template = panel.querySelector<HTMLTemplateElement>(
    "[data-template='slash-item']",
  );

  const close = (): void => {
    open = false;
    panel.hidden = true;
  };

  const choose = (command: SlashCommand): void => {
    const editor = getEditor();
    if (editor === null) return;
    // The query is removed and the block applied in one chain, so the two are a
    // single undo step — an operator pressing Ctrl+Z once must not be left with
    // a heading and a stray "/lista" they have to delete by hand.
    close();
    removeSlashQuery(editor);
    command.run(editor);
  };

  const render = (): void => {
    if (template === null) return;
    panel.replaceChildren();
    matches.forEach((command, position) => {
      const node = cloneSlashItem(template);
      if (node === null) return;
      setField(node, "slash-key", command.key);
      setField(node, "slash-title", command.title);
      setField(node, "slash-hint", command.hint);
      node.setAttribute("aria-selected", String(position === index));
      node.addEventListener("mousedown", (event) => {
        // `mousedown`, not `click`: the editor would lose the selection the
        // command is about to act on before a click ever landed.
        event.preventDefault();
        choose(command);
      });
      panel.appendChild(node);
    });
  };

  const place = (): void => {
    const editor = getEditor();
    if (editor === null) return;
    const { from } = editor.state.selection;
    const caret = editor.view.coordsAtPos(from);
    panel.style.left = `${Math.round(caret.left)}px`;
    panel.style.top = `${Math.round(caret.bottom + 6)}px`;
  };

  const sync = (): void => {
    const editor = getEditor();
    if (editor === null) return;
    const query = slashQuery(editor);
    if (query === null) {
      if (open) close();
      return;
    }
    matches = COMMANDS.filter((command) =>
      command.title.toLowerCase().startsWith(query.toLowerCase()),
    );
    if (matches.length === 0) {
      close();
      return;
    }
    index = 0;
    open = true;
    panel.hidden = false;
    render();
    place();
  };

  const handleKeyDown = (event: KeyboardEvent): boolean => {
    if (!open) return false;
    if (event.key === "Escape") {
      close();
      return true;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const step = event.key === "ArrowDown" ? 1 : -1;
      index = (index + step + matches.length) % matches.length;
      render();
      return true;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      const command = matches[index];
      if (command === undefined) return false;
      choose(command);
      return true;
    }
    return false;
  };

  return { handleKeyDown, sync, close };
}

/**
 * The text typed after a `/` that starts a word, or `null` when the caret is
 * not in one.
 *
 * Anchored to a word boundary so a URL's slashes never open the menu.
 */
function slashQuery(editor: Editor): string | null {
  const { from, empty } = editor.state.selection;
  if (!empty) return null;
  const before = editor.state.doc.textBetween(
    Math.max(0, from - 30),
    from,
    "\n",
    "\0",
  );
  const match = /(?:^|\s)\/(\S*)$/.exec(before);
  return match?.[1] ?? null;
}

/** Deletes the `/query` the operator typed, before running the command. */
function removeSlashQuery(editor: Editor): void {
  const query = slashQuery(editor);
  if (query === null) return;
  const { from } = editor.state.selection;
  editor
    .chain()
    .focus()
    .deleteRange({ from: from - query.length - 1, to: from })
    .run();
}

/** One `/` menu row, cloned so it keeps the component's scoped styles. */
function cloneSlashItem(template: HTMLTemplateElement): HTMLElement | null {
  const node = template.content.firstElementChild?.cloneNode(true);
  return node instanceof HTMLElement ? node : null;
}

/**
 * Which endpoint a scope writes to.
 *
 * Resolved here rather than passed in, so the Astro component stays declarative
 * and one mount serves both aggregates. An unknown scope returns `null` and the
 * field simply does not mount, which is louder than silently editing nothing.
 */
function submitFor(scope: string): RichTextSubmit | null {
  if (scope === "task") return saveTaskDescription;
  if (scope === "project") return saveProjectDescription;
  return null;
}

async function saveTaskDescription(
  _scope: string,
  code: string,
  markdown: string,
): Promise<boolean> {
  const { patchTask } = await import("../../lib/api/client");
  const result = await patchTask(code, { description: markdown });
  return result.ok;
}

async function saveProjectDescription(
  _scope: string,
  code: string,
  markdown: string,
): Promise<boolean> {
  const { patchProject } = await import("../../lib/api/client");
  const result = await patchProject(code, { description: markdown });
  return result.ok;
}
