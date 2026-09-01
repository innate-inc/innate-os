// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The on-screen keyboard, for a shell that has nothing to scroll.
//
// iOS Safari answers a focused field by shrinking the *visual* viewport and
// scrolling the layout viewport to bring the field into view. Our shell is
// exactly one viewport tall and does not scroll, so that scroll drags the page
// off the top and leaves a band of dead space beside the keyboard -- and where
// it lands varies with how far up the field was. Ending the shell where the
// keyboard begins puts the composer directly above it, and undoing the scroll
// keeps the top of the page where it belongs.
//
// Chrome and Android need none of this: the meta viewport's interactive-widget
// hint has them resize the layout viewport themselves.

// A keyboard covers a few hundred pixels; a browser's own bars take far less,
// and shrinking for those would leave dead space under the app.
const KEYBOARD_MIN_PX = 140;

/** @returns {() => void} teardown, for symmetry — the shell never calls it. */
export function trackKeyboardInset() {
  const viewport = window.visualViewport;
  if (!viewport) return () => {};
  const root = document.documentElement;

  const apply = () => {
    const covered = window.innerHeight - viewport.height;
    const keyboardUp = covered > KEYBOARD_MIN_PX;
    root.classList.toggle("keyboard-up", keyboardUp);
    root.style.setProperty("--keyboard-inset", keyboardUp ? `${Math.round(covered)}px` : "0px");
    // Safari's scroll-into-view is what produces the jump; with the shell now
    // ending above the keyboard, the field is visible without it.
    if (keyboardUp && window.scrollY !== 0) window.scrollTo(0, 0);
  };

  viewport.addEventListener("resize", apply);
  viewport.addEventListener("scroll", apply);
  apply();

  return () => {
    viewport.removeEventListener("resize", apply);
    viewport.removeEventListener("scroll", apply);
  };
}
