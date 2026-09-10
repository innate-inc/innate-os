// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Point at a part of the interface the way a game does: the element pops, a halo breathes
// around it, rings ping outward, a sheen sweeps across, and a badge drops in saying what to
// do there. One mechanism for every "look here" the story makes, so a camera tile and an
// offer draw the eye the same way. The element must be positioned (the frame hangs inside it).

/**
 * @param {HTMLElement} el
 * @param {string} [label] the badge; none for the halo alone
 * @returns {() => void} removes the cue
 */
export function cue(el, label) {
  el.classList.add("ui-cue");
  const frame = document.createElement("span");
  frame.className = "ui-cue-frame";
  frame.setAttribute("aria-hidden", "true");
  frame.innerHTML =
    '<i class="ui-cue-glow"></i><i class="ui-cue-ring"></i><i class="ui-cue-ring"></i><i class="ui-cue-sheen"><i></i></i>';
  el.append(frame);
  /** @type {HTMLElement | null} */ let badge = null;
  if (label) {
    badge = document.createElement("span");
    badge.className = "ui-cue-badge";
    badge.textContent = label;
    el.append(badge);
  }
  return () => {
    el.classList.remove("ui-cue");
    frame.remove();
    badge?.remove();
  };
}

const CLOSE_IN_MS = 1200;

/** A square that closes in on `el` once, like a target being acquired. Fixed over the page
 * rather than inside the element, so a parent that clips (a camera tile) cannot hide it. */
export function closeIn(el) {
  const rect = el.getBoundingClientRect();
  const square = document.createElement("span");
  square.className = "ui-cue-square";
  square.style.cssText =
    `left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;` +
    `border-radius:${getComputedStyle(el).borderRadius}`;
  document.body.append(square);
  setTimeout(() => square.remove(), CLOSE_IN_MS); // no animationend under reduced motion
}
