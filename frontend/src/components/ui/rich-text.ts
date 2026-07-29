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
import { RICH_TEXT_STATUS } from "./rich-text-copy";
import { setField } from "../projects/dom";

/** How long the editor stays quiet before persisting a burst of typing. */
const SAVE_DEBOUNCE_MS = 900;

/** Performs the write; the caller owns which endpoint that is. */
export type RichTextSubmit = (
  scope: string,
  code: string,
  markdown: string,
) => Promise<boolean>;

/**
 * One entry of the `/` menu.
 *
 * A block is Markdown syntax, not an editor command: the operator is writing
 * the source, so the menu's job is to type the marks they would otherwise have
 * to remember. That also keeps every entry round-trippable by definition —
 * whatever it inserts *is* what gets stored.
 */
interface SlashCommand {
  readonly key: string;
  readonly title: string;
  readonly hint: string;
  /** Written at the start of the line the caret is on. */
  readonly prefix: string;
  /** Written after the caret, for the marks that wrap. */
  readonly suffix?: string;
}

/**
 * The blocks a description can hold.
 *
 * Deliberately short. A `/` menu is a promise that each entry produces
 * something the *stored Markdown* can round-trip, so anything that would only
 * live in the editor's own memory has no place here.
 */
const COMMANDS: readonly SlashCommand[] = [
  { key: "H1", title: "Título", hint: "Encabezado grande", prefix: "# " },
  { key: "H2", title: "Subtítulo", hint: "Encabezado mediano", prefix: "## " },
  { key: "H3", title: "Apartado", hint: "Encabezado pequeño", prefix: "### " },
  { key: "•", title: "Lista", hint: "Viñetas", prefix: "- " },
  { key: "1.", title: "Lista numerada", hint: "Pasos en orden", prefix: "1. " },
  { key: "❝", title: "Cita", hint: "Texto citado", prefix: "> " },
  {
    key: "<>",
    title: "Código",
    hint: "Bloque monoespaciado",
    prefix: "```\n",
    suffix: "\n```",
  },
  { key: "—", title: "Separador", hint: "Línea horizontal", prefix: "---\n" },
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
  const source = root.querySelector<HTMLTextAreaElement>("[data-rich-source]");
  if (surface === null || fallback === null || slash === null) return () => {};
  if (source === null) return () => {};

  const scope = root.dataset["scope"] ?? "";
  const code = root.dataset["code"] ?? "";
  const resolved = submitFor(scope);
  if (resolved === null) return () => {};
  const submit: RichTextSubmit = resolved;
  // Bound to non-nullable consts: the guard above narrows these, but that
  // narrowing does not survive into the closures below, and the alternative is
  // the `!` the house rules forbid.
  const fallbackNode: HTMLElement = fallback;
  const sourceNode: HTMLTextAreaElement = source;
  const surfaceNode: HTMLElement = surface;

  // The stored Markdown is read as *text*, never as markup — see the module note.
  const initial = fallbackNode.textContent ?? "";
  sourceNode.value = initial;

  let timer = 0;
  let lastSaved = initial;
  let destroyed = false;
  /**
   * Whether the operator has typed into this field during this mount.
   *
   * Load-bearing, and the reason is a real loss: the teardown flushes the
   * debounce, and a teardown can run when the textarea is already detached from
   * a swapped-out document — where its `value` reads `""`. Saving that emptied a
   * description nobody had touched. A write now requires evidence that somebody
   * wrote, so clearing a description stays possible (they typed the deletion)
   * while inventing one does not.
   */
  let dirty = false;

  // Read mode only. The editor never becomes editable: it is the renderer, and
  // `html: false` plus ProseMirror's schema is what keeps somebody's text from
  // ever being parsed as markup.
  const editor = new Editor({
    element: surface,
    editable: false,
    extensions: [
      StarterKit.configure({ heading: { levels: [1, 2, 3] } }),
      Markdown.configure({ html: false, transformPastedText: true }),
    ],
    content: initial,
  });

  const slashMenu = mountSlash(sourceNode, slash);

  async function save(): Promise<void> {
    if (destroyed || !dirty) return;
    const markdown = sourceNode.value;
    if (markdown === lastSaved) return;

    status(root, "saving");
    const ok = await submit(scope, code, markdown);
    if (destroyed) return;
    if (!ok) {
      status(root, "failed");
      return;
    }
    lastSaved = markdown;
    // The pre-hydration node is the only record a re-mount reads back; left at
    // the first paint's text, any reload would revert the field to what the
    // server sent when the page was built.
    fallbackNode.textContent = markdown;
    status(root, "saved");
    renderEmpty(root, markdown);
  }

  /**
   * Reading or editing.
   *
   * Read renders the Markdown; edit shows the Markdown itself. Editing the
   * rendering would hide the syntax being written — somebody typing `##` needs
   * to see `##`, not watch it turn into a size — which is the whole reason
   * these are two surfaces over one value rather than one editable rendering.
   */
  function setMode(mode: "read" | "edit"): void {
    root.dataset["mode"] = mode;
    setField(root, "edit-label", mode === "edit" ? "Listo" : "Editar");
    surfaceNode.hidden = mode === "edit";
    sourceNode.hidden = mode === "read";
    if (mode === "edit") {
      sourceNode.focus();
      return;
    }
    slashMenu.close();
    // Re-render from whatever the source now holds, so leaving the editor is
    // also what proves the Markdown was understood.
    editor.commands.setContent(sourceNode.value);
    renderEmpty(root, sourceNode.value);
  }

  sourceNode.addEventListener("input", () => {
    dirty = true;
    slashMenu.sync();
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      void save();
    }, SAVE_DEBOUNCE_MS);
  });

  sourceNode.addEventListener("keydown", (event) => {
    if (slashMenu.handleKeyDown(event)) {
      event.preventDefault();
      return;
    }
    if (event.key !== "Escape") return;
    event.preventDefault();
    window.clearTimeout(timer);
    void save();
    setMode("read");
  });

  sourceNode.addEventListener("blur", () => {
    // Saves, but does not leave: blur fires for reasons that are not "I am
    // finished", and being thrown back to read mode mid-thought is worse than a
    // border left on a moment too long. Leaving is the button, or Escape.
    window.clearTimeout(timer);
    void save();
  });

  const toggle = root.querySelector<HTMLButtonElement>(
    "[data-action='toggle-edit']",
  );
  const onToggle = (): void => {
    if (root.dataset["mode"] === "edit") {
      window.clearTimeout(timer);
      void save();
      setMode("read");
      return;
    }
    setMode("edit");
  };
  if (toggle !== null) {
    toggle.hidden = false;
    toggle.addEventListener("click", onToggle);
  }

  // The prose is its own affordance: clicking the text is how most people will
  // try to correct it, and a field that answered only its button would be a
  // field most people call broken.
  surfaceNode.addEventListener("mousedown", () => {
    if (root.dataset["mode"] !== "edit") setMode("edit");
  });

  fallbackNode.hidden = true;
  setMode("read");

  return () => {
    window.clearTimeout(timer);
    void save();
    destroyed = true;
    editor.destroy();
  };
}

/** Shows or hides the named absence, from the value that decides it. */
function renderEmpty(root: HTMLElement, markdown: string): void {
  const empty = root.querySelector<HTMLElement>("[data-rich-empty]");
  if (empty !== null) empty.hidden = markdown.trim() !== "";
}

/** Writes the save state, and its tone, into the status line. */
function status(root: HTMLElement, state: keyof typeof RICH_TEXT_STATUS): void {
  setField(root, "rich-status", RICH_TEXT_STATUS[state]);
  const line = root.querySelector<HTMLElement>("[data-field='rich-status']");
  if (line === null) return;
  if (state === "failed") line.dataset["tone"] = "error";
  else delete line.dataset["tone"];
}

/** The `/` menu, as the source textarea's key handling sees it. */
interface SlashMenu {
  /** Consumes a key while the menu is open; `false` lets the textarea have it. */
  readonly handleKeyDown: (event: KeyboardEvent) => boolean;
  /** Recomputes the query after the text changed. */
  readonly sync: () => void;
  readonly close: () => void;
}

/**
 * The `/` menu over the Markdown source.
 *
 * It types what the operator would otherwise have to remember: choosing "Lista"
 * writes `- ` at the start of the line. Because the menu only ever inserts
 * syntax, every entry round-trips by construction — there is no editor state it
 * could produce that the stored text cannot express.
 *
 * The panel is plain DOM cloned from a `<template>`, for the reason every menu
 * in this app clones: an element built in TypeScript carries no scope id and
 * renders unstyled.
 */
function mountSlash(
  source: HTMLTextAreaElement,
  panel: HTMLElement,
): SlashMenu {
  let open = false;
  let index = 0;
  let matches: readonly SlashCommand[] = COMMANDS;

  // Held before the first render: `replaceChildren` below would otherwise
  // delete the very `<template>` the rows are cloned from, and every later open
  // would be a blank box.
  const template = panel.querySelector<HTMLTemplateElement>(
    "[data-template='slash-item']",
  );

  const close = (): void => {
    open = false;
    panel.hidden = true;
  };

  /** The `/word` immediately before the caret, or `null` when there is none. */
  const query = (): string | null => {
    const before = source.value.slice(0, source.selectionStart);
    const match = /(?:^|\s)\/(\S*)$/.exec(before);
    return match?.[1] ?? null;
  };

  const choose = (command: SlashCommand): void => {
    const typed = query();
    if (typed === null) return;
    const caret = source.selectionStart;
    // The `/query` itself is replaced, so the marks land where the operator was
    // already writing rather than beside the trigger they typed.
    const cut = caret - typed.length - 1;
    const lineStart = source.value.lastIndexOf("\n", cut - 1) + 1;
    const head = source.value.slice(0, lineStart);
    const middle = source.value.slice(lineStart, cut);
    const tail = source.value.slice(caret);
    const suffix = command.suffix ?? "";
    source.value = `${head}${command.prefix}${middle}${tail}${suffix}`;
    const at = head.length + command.prefix.length + middle.length;
    source.setSelectionRange(at, at);
    close();
    source.focus();
    source.dispatchEvent(new Event("input", { bubbles: true }));
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
        // `mousedown`, not `click`: the textarea would lose its caret — the very
        // position the command writes at — before a click ever landed.
        event.preventDefault();
        choose(command);
      });
      panel.appendChild(node);
    });
  };

  const place = (): void => {
    const box = source.getBoundingClientRect();
    panel.style.left = `${Math.round(box.left + 12)}px`;
    panel.style.top = `${Math.round(box.top + 32)}px`;
  };

  const sync = (): void => {
    const typed = query();
    if (typed === null) {
      if (open) close();
      return;
    }
    matches = COMMANDS.filter((command) =>
      command.title.toLowerCase().startsWith(typed.toLowerCase()),
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
      index =
        (index + (event.key === "ArrowDown" ? 1 : -1) + matches.length) %
        matches.length;
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
