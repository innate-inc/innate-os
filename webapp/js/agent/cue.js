// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Point at a part of the interface the way a game does: the element pops, a halo breathes
// around it, corner brackets lock on, rings ping outward, a sheen sweeps across, and a badge
// drops in saying what to do there. One mechanism for every "look here" the story makes, so a camera tile and an
// offer draw the eye the same way. The element must be positioned (the frame hangs inside it).

/**
 * @param {HTMLElement} el
 * @param {string} [label] the badge; none for the lock-on alone
 * @returns {() => void} removes the cue
 */
export function cue(el, label) {
  el.classList.add("ui-cue");
  const frame = document.createElement("span");
  frame.className = "ui-cue-frame";
  frame.setAttribute("aria-hidden", "true");
  frame.innerHTML =
    '<i class="ui-cue-glow"></i><i class="ui-cue-ring"></i><i class="ui-cue-ring"></i><i class="ui-cue-corners"></i><i class="ui-cue-sheen"><i></i></i>';
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
