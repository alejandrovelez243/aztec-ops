/**
 * Request middleware: the server-side half of the session.
 *
 * Two jobs, both of which have to happen before a page's frontmatter runs.
 *
 * 1. **Carry the credential into the render.** Page frontmatter fetches the
 *    API on the server, where neither `localStorage` nor the API's own
 *    origin-scoped cookie is reachable. The browser mirrors its access token
 *    into a cookie on this origin; this reads it once and runs the whole
 *    render inside that context, so `api/client.ts` finds it without a single
 *    call site passing a token around.
 *
 * 2. **Guard the app.** A visitor with no token is redirected to `/login`
 *    carrying where they were headed. This is the authoritative guard — the
 *    inline script in the shell is only a pre-paint courtesy that avoids a
 *    flash, and a guard that lives solely in client JavaScript is a suggestion.
 *
 * An expired token is deliberately *not* refreshed here: the refresh token is
 * the long-lived credential and stays out of the server's reach. The API
 * answers 401, the page renders its own recovery state, and the browser — which
 * does hold the refresh token — renews and refetches.
 */
import { defineMiddleware } from "astro:middleware";

import { SESSION_COOKIE_NAME } from "./lib/auth/tokens";

/** Paths that must render for a signed-out visitor. */
const PUBLIC_PATHS: ReadonlySet<string> = new Set(["/login"]);

/** Prefixes Astro owns; guarding them breaks HMR and asset serving. */
const INTERNAL_PREFIXES: readonly string[] = ["/_", "/@", "/node_modules"];

export const onRequest = defineMiddleware(async (context, next) => {
  const { pathname } = context.url;
  const token = context.cookies.get(SESSION_COOKIE_NAME)?.value ?? null;

  const isInternal = INTERNAL_PREFIXES.some((prefix) =>
    pathname.startsWith(prefix),
  );
  if (isInternal) return next();

  if (token === null && !PUBLIC_PATHS.has(pathname)) {
    const next_ = encodeURIComponent(pathname + context.url.search);
    return context.redirect(`/login?next=${next_}`, 302);
  }

  const { withServerToken } = await import("./lib/auth/server-token");
  return withServerToken(token, next);
});
