/**
 * The `disconnected` arm, shared by every region of the projects surfaces.
 *
 * When the stream drops, the data on screen stays — blanking a board an
 * operator is reading is the one failure mode an operations tool must not have
 * — and a banner says how old it is and offers the reconnection. The timestamp
 * is the `occurred_at` of the last envelope this tab actually received, not the
 * moment the connection died: "we are showing you 09:41" is answerable, "the
 * socket closed" is not.
 *
 * Islands report every envelope they handle through {@link markEvent}; the
 * banner reads the latest one when it appears.
 */

import { onStatus, retry } from "../../lib/stream/store";
import { relativeTime } from "./format";

/** `occurred_at` of the newest envelope any island on this page has seen. */
let lastEventAt: string | null = null;

/**
 * Records that an envelope arrived. Older envelopes are ignored, so an
 * at-least-once redelivery cannot make the board look staler than it is.
 */
export function markEvent(occurredAt: string): void {
  if (lastEventAt === null || occurredAt > lastEventAt) {
    lastEventAt = occurredAt;
  }
}

/**
 * Wires one banner: shown only while the stream is down, with the age of the
 * data and a "Reconectar" bound to the store's `retry()`.
 *
 * @returns The teardown; call it on `astro:before-swap` or the status handler
 *   keeps a detached banner alive across navigations.
 */
export function watchStale(banner: HTMLElement): () => void {
  const stamp = banner.querySelector("[data-field='last-event']");
  const button = banner.querySelector("[data-action='reconnect']");

  const off = onStatus((status) => {
    banner.hidden = status !== "disconnected";
    if (status !== "disconnected" || stamp === null) return;
    const seen = relativeTime(lastEventAt);
    stamp.textContent =
      seen === null ? "sin eventos en esta sesión" : `último cambio ${seen}`;
  });

  const onRetry = (): void => {
    retry();
  };
  button?.addEventListener("click", onRetry);

  return () => {
    off();
    button?.removeEventListener("click", onRetry);
  };
}
