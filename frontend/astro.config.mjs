// @ts-check
import node from "@astrojs/node";
import { defineConfig } from "astro/config";

/**
 * Every page renders on demand, and that is a requirement rather than a preference.
 *
 * This app is behind a login: what a page shows depends on who is asking, and the
 * answer is a per-request fact. Astro's default `static` output prerenders pages,
 * which strips `Astro.request.headers` and the request's cookies — so the session
 * middleware could never see the token, every guard decided "signed out", and the
 * browser, which did hold the session, bounced straight back into it. A prerendered
 * authenticated page is not a slow page, it is a wrong one.
 *
 * The Node adapter in standalone mode is what makes that configuration buildable as
 * well as runnable: without it `output: "server"` fails at build time asking for an
 * adapter, and that failure would only surface the first time somebody ran a build.
 */
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
});
