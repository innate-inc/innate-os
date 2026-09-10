// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The boot splash lives in index.html so it paints before this module's graph loads.
// It comes down when the first page has mounted -- and, for a page that will hide part
// of the interface, not until that page knows whether it will: the alternative is
// painting the whole app and taking half of it away a beat later.

// A splash outliving its reason is worse than the flash it prevents. Long enough for a
// cold sim to build its world under software GL, not long enough to look hung.
const SETTLE_MAX_MS = 12_000;

/** @type {(() => Promise<unknown>) | null} */
let settling = null;

/** @param {() => Promise<unknown>} until */
export function holdBootSplash(until) {
  const splash = document.getElementById("boot-splash");
  if (!splash) return;
  settling = until;
  // The splash leaves the rail column uncovered, which is right for an ordinary load and
  // wrong here: a page that may hide the rail would show the app through that strip.
  splash.classList.add("is-full");
}

/** Fade then remove; a fallback timer covers a missed transitionend (a pre-paint start). */
export async function dismissBootSplash() {
  const until = settling;
  settling = null;
  if (until) {
    await Promise.race([until().catch(() => {}), new Promise((done) => setTimeout(done, SETTLE_MAX_MS))]);
  }
  const splash = document.getElementById("boot-splash");
  if (!splash) return;
  // Reduced motion turns the fade into `transition: none` (app.css), so there is no
  // transitionend to wait for and the fallback timer would hold an opaque cover over a
  // page that is already up. Drop it now instead.
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    splash.remove();
    return;
  }
  splash.classList.add("is-leaving");
  splash.addEventListener("transitionend", () => splash.remove(), { once: true });
  setTimeout(() => splash.remove(), 400); // fallback >= the 0.2s fade in app.css
}
