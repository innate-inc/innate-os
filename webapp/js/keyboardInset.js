// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The on-screen keyboard, for a shell that has nothing to scroll.
//
// iOS Safari shrinks the visual viewport for a focused field and scrolls the
// layout viewport to reveal it, which drags a one-viewport-tall shell off the
// top. Ending the shell where the keyboard begins does the job instead.
// Chrome honours the meta viewport's interactive-widget hint and needs none.

// A keyboard covers hundreds of pixels; a browser's own bars far less, and
// shrinking for those would leave dead space under the app.
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
    // The shell now ends above the keyboard, so the scroll is only the jump.
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
