/**
 * The access token of the request currently being server-rendered.
 *
 * **Server-only.** It imports `node:async_hooks`, so it must never reach the
 * browser bundle: every caller loads it through a dynamic `import()` inside an
 * `import.meta.env.SSR` branch, which Vite removes entirely from the client
 * build.
 *
 * Why it exists: page frontmatter runs on the server, but the session lives in
 * the browser — `localStorage` is not readable from Node, and the API's
 * HttpOnly cookie is scoped to the API's own origin and port, so it is never
 * sent to the Astro server either. Without this, every first paint of an
 * authenticated page renders an error and the operator watches the screen
 * repair itself. The middleware reads the mirror cookie once per request and
 * runs the render inside {@link withServerToken}; `api/client.ts` then picks
 * the token up with no change at any call site.
 *
 * Async-local rather than a module-level variable, because a module variable
 * is shared by every request the server handles concurrently: two operators
 * rendering at the same time would swap tokens. The store is per-async-context,
 * so a token cannot leak across requests.
 */
import { AsyncLocalStorage } from "node:async_hooks";

const storage = new AsyncLocalStorage<string>();

/**
 * Runs `render` with `token` as the ambient credential of this request.
 *
 * @param token - The access token from the mirror cookie, or `null` when the
 *   visitor is signed out; `null` runs the render with no ambient token rather
 *   than inheriting an outer one.
 * @param render - The work to run inside the context.
 */
export function withServerToken<T>(token: string | null, render: () => T): T {
  if (token === null) return storage.exit(render);
  return storage.run(token, render);
}

/** The current request's access token, or `null` outside a rendered request. */
export function getServerToken(): string | null {
  return storage.getStore() ?? null;
}
