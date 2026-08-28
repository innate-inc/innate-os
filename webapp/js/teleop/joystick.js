// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// DriveController publishes /joystick and owns its heartbeat; this module reports normalized input.

const PAD_RADIUS = 92; // At full input, the knob center reaches the rim; CSS controls rendered size.
const PAD_SIZE = PAD_RADIUS * 2;
const TOGGLE_KEY = "KeyJ";

// Helps small movements read clearly via pad glow, direction markers, and knob border/size.
/** @param {number} value */
function strengthenVisualFeedback(value) {
  return Math.sign(value) * Math.abs(value) ** 0.85;
}

/**
 * @param {HTMLElement} parent
 * @param {import("../driveController.js").DriveController} driveController
 * @returns {{ destroy: () => void }}
 */
export function createJoystick(parent, driveController) {
  const pad = document.createElement("div");
  pad.className = "joystick-pad";
  for (const direction of ["forward", "backward", "right", "left"]) {
    const marker = document.createElement("span");
    marker.className = `joystick-direction joystick-direction-${direction}`;
    pad.appendChild(marker);
  }

  const knob = document.createElement("div");
  knob.className = "joystick-knob";

  const hitTarget = document.createElement("div");
  hitTarget.className = "joystick-hit-target";
  hitTarget.setAttribute("role", "application");
  hitTarget.setAttribute("aria-label", "Drive joystick");
  hitTarget.title = "drag to drive — /joystick · press j to hide";
  // Siblings so each backdrop-filter samples the camera, not the other glass.
  hitTarget.append(pad, knob);

  let pointerEngaged = false;

  /** @param {number} dx @param {number} dy */
  function clampToPad(dx, dy) {
    const dist = Math.hypot(dx, dy);
    const t = Math.min(1, dist / PAD_RADIUS);
    if (dist > PAD_RADIUS) {
      const s = PAD_RADIUS / dist;
      dx *= s;
      dy *= s;
    }
    return { dx, dy, t };
  }

  /** @param {number} dx @param {number} dy screen-frame pad units */
  function setKnob(dx, dy) {
    const p = clampToPad(dx, dy);
    const throwAmt = strengthenVisualFeedback(p.t);
    parent.style.setProperty("--joystick-throw", String(throwAmt));
    parent.style.setProperty("--joystick-strafe", String(strengthenVisualFeedback(p.dx / PAD_RADIUS)));
    parent.style.setProperty("--joystick-forward", String(strengthenVisualFeedback(-p.dy / PAD_RADIUS)));
    // Scale from the visible pad so responsive CSS sizes preserve the same input range.
    const toCss = (pad.clientWidth || PAD_SIZE) / PAD_SIZE;
    knob.style.transform =
      `translate(${p.dx * toCss}px, ${p.dy * toCss}px) scale(${1 + throwAmt * 0.05})`;
    return p;
  }

  /** @param {PointerEvent} e */
  function pointerOffset(e) {
    const rect = pad.getBoundingClientRect();
    const toPad = PAD_SIZE / rect.width;
    return {
      dx: (e.clientX - rect.left - rect.width / 2) * toPad,
      dy: (e.clientY - rect.top - rect.height / 2) * toPad,
    };
  }

  /** @param {PointerEvent} e */
  function update(e) {
    const { dx, dy } = pointerOffset(e);
    const p = setKnob(dx, dy);
    // Screen Y grows downward; negate it so dragging up maps to forward motion.
    driveController.setInput("joystick", p.dx / PAD_RADIUS, -p.dy / PAD_RADIUS, true);
  }

  /** @param {PointerEvent} e */
  function onPointerDown(e) {
    if (pointerEngaged) return;
    const { dx, dy } = pointerOffset(e);
    if (Math.hypot(dx, dy) > PAD_RADIUS) return;
    pointerEngaged = true;
    try {
      hitTarget.setPointerCapture(e.pointerId);
    } catch {
      // Synthetic/stale pointers may not be capturable.
    }
    hitTarget.classList.add("engaged");
    update(e);
  }

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (pointerEngaged) update(e);
  }

  function release() {
    if (!pointerEngaged) return;
    pointerEngaged = false;
    hitTarget.classList.remove("engaged");
    setKnob(0, 0);
    driveController.setInput("joystick", 0, 0, false);
  }

  hitTarget.addEventListener("pointerdown", onPointerDown);
  hitTarget.addEventListener("pointermove", onPointerMove);
  hitTarget.addEventListener("pointerup", release);
  hitTarget.addEventListener("pointercancel", release);
  hitTarget.addEventListener("lostpointercapture", release);

  // Clear latched drive on hide so a hidden joystick can't keep driving.
  let hidden = false;
  function toggleHidden() {
    hidden = !hidden;
    if (hidden) release();
    hitTarget.hidden = hidden;
  }

  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
    if (e.code !== TOGGLE_KEY || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    const el = document.activeElement;
    if (el instanceof HTMLElement && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) {
      return;
    }
    toggleHidden();
  }
  window.addEventListener("keydown", onKeyDown);

  parent.appendChild(hitTarget);

  // Show keyboard commands on the joystick knob.
  const unsubActive = driveController.onActiveChange((state) => {
    if (pointerEngaged) return;
    const keyboardActive = state.source === "keyboard";
    hitTarget.classList.toggle("mirroring", keyboardActive);
    if (keyboardActive) setKnob(state.x * PAD_RADIUS, -state.y * PAD_RADIUS);
    else if (state.source === null) setKnob(0, 0);
  });

  return {
    destroy() {
      release();
      window.removeEventListener("keydown", onKeyDown);
      unsubActive();
      hitTarget.remove();
      parent.style.removeProperty("--joystick-throw");
      parent.style.removeProperty("--joystick-strafe");
      parent.style.removeProperty("--joystick-forward");
    },
  };
}
