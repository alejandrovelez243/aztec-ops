/**
 * The pre-paint guards, as source for the shell and the login page to inline.
 *
 * They are strings rather than an imported module because both run *before*
 * the page paints, ahead of any bundle: a module import would land after the
 * first frame and the redirect would arrive as a visible flash.
 *
 * Both read the **cookie**, never `localStorage`, and that is the whole point.
 * The middleware decides from the cookie, so a client guard that consulted a
 * different source could disagree with it — and two guards that disagree do
 * not fail, they ping-pong: the client sends a visitor to `/`, the server sends
 * them back to `/login`, forever, which looks exactly like a page that never
 * finishes loading. A session left in `localStorage` without its cookie is
 * therefore treated as expired and cleared, which is also what makes an old
 * session from before this mechanism heal itself instead of trapping the tab.
 */

/** Reads a cookie by name from `document.cookie`, or `null`. Inlined as text. */
const READ_COOKIE = `function readCookie(name) {
  var parts = document.cookie ? document.cookie.split("; ") : [];
  for (var i = 0; i < parts.length; i += 1) {
    var pair = parts[i].split("=");
    if (pair[0] === name) return decodeURIComponent(pair.slice(1).join("="));
  }
  return null;
}`;

/**
 * Guard for app pages: no cookie means no session the server would honour, so
 * drop any stale local session and leave for the login screen.
 *
 * @param cookieName - The mirror cookie's name.
 * @param storageKey - The session key to clear when the cookie is gone.
 * @param loginPath - Where a signed-out visitor belongs.
 */
export function appGuardScript(
  cookieName: string,
  storageKey: string,
  loginPath: string,
): string {
  return `${READ_COOKIE}
try {
  if (readCookie(${JSON.stringify(cookieName)}) === null) {
    window.localStorage.removeItem(${JSON.stringify(storageKey)});
    window.location.replace(${JSON.stringify(loginPath)} + "?next=" + encodeURIComponent(window.location.pathname + window.location.search));
  }
} catch (error) {
  /* storage or cookies disabled: the middleware and the API's 401s still answer */
}`;
}

/**
 * Guard for the login page: a live cookie means the door is already open, so
 * skip it. A local session without the cookie is stale and is cleared here,
 * which is the self-healing half of the pair.
 *
 * `next` is honoured only when it is a same-site absolute path, so a crafted
 * `?next=//evil.example` cannot turn the login screen into an open redirect.
 */
export function loginGuardScript(
  cookieName: string,
  storageKey: string,
): string {
  return `${READ_COOKIE}
try {
  if (readCookie(${JSON.stringify(cookieName)}) !== null) {
    var next = new URLSearchParams(window.location.search).get("next");
    var safe = next !== null && next.charAt(0) === "/" && next.charAt(1) !== "/" ? next : "/";
    window.location.replace(safe);
  } else if (window.localStorage.getItem(${JSON.stringify(storageKey)}) !== null) {
    // A stored session with no cookie means the access token expired, not that
    // the operator signed out: the refresh token may still be good. The form is
    // hidden while the module script tries to renew, so an expiry reads as a
    // blink instead of a logout. It clears the session itself if the renewal
    // fails, which is what keeps the two guards from disagreeing forever.
    document.documentElement.dataset.restoring = "";
  }
} catch (error) {
  /* storage or cookies disabled: the form below still works */
}`;
}
