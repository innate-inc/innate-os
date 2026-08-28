// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Keyboard drive: WASD/arrows with ramping, so taps nudge and holds glide.
//
// Axes are gated independently, so W+A is (-1, 1) and a turn costs no throttle.
// Normalising onto the unit circle bought nothing — scaling both axes equally leaves
// the turn radius unchanged, it only made the same arc slower.
//
// The ramp is polar so the vector turns rather than having x and y race each other:
// ramping separately dropped y in one tick while x crawled up over 250 ms, which read
// as a stall mid-corner.
//
// Blur and hidden-tab clear held keys — Cmd+Tab eats keyup, so a latched key must not
// survive focus loss.

const TICK_MS = 50;
const RAMP_UP_MS = 350;
const RAMP_DOWN_MS = 150;
const PRECISION_SCALE = 0.75;
const EPSILON = 0.02;
// ms to sweep a half turn; the 45 deg W -> W+A corner lands in ~125 ms.
const ANGLE_RAMP_MS = 500;
const ANGLE_STEP = Math.PI * (TICK_MS / ANGLE_RAMP_MS);
// Past a quarter turn, arcing would sweep through headings nobody asked for — A to D
// would drive full-speed forward on the way past. Bleed to zero and re-emerge instead.
const REORIENT_MAX_ANGLE = Math.PI / 2;

/** A polar sweep between two corners bulges outside the square, so recomposed axes need
 *  clamping before mars_app's curve amplifies y > 1 past max_speed. */
const clampAxis = (v) => Math.max(-1, Math.min(1, v));

/** @type {Map<string, "up" | "down" | "left" | "right">} */
const KEY_DIRS = new Map([
  ["KeyW", "up"],
  ["ArrowUp", "up"],
  ["KeyS", "down"],
  ["ArrowDown", "down"],
  ["KeyA", "left"],
  ["ArrowLeft", "left"],
  ["KeyD", "right"],
  ["ArrowRight", "right"],
]);

function isTypingContext() {
  const el = document.activeElement;
  if (!(el instanceof HTMLElement)) return false;
  if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable) return true;
  // The skills popup owns its keys (arrows cycle enum pills, digits pick rows)
  // — a focused control in it must not also drive the base.
  return el.closest(".skills-pop") !== null;
}

/**
 * @param {import("../driveController.js").DriveController} driveController
 * @returns {{ onChange: (cb: (s: KeyboardDriveState) => void) => () => void, destroy: () => void }}
 */
export function createKeyboardDrive(driveController) {
  /** @type {Set<"up" | "down" | "left" | "right">} */
  const held = new Set();
  let shift = false;
  let vx = 0;
  let vy = 0;
  let engaged = false;
  /** @type {number | null} */
  let timer = null;
  /** @type {Set<(s: KeyboardDriveState) => void>} */
  const listeners = new Set();

  function emit() {
    /** @type {KeyboardDriveState} */
    const snapshot = {
      held: {
        up: held.has("up"),
        down: held.has("down"),
        left: held.has("left"),
        right: held.has("right"),
      },
      x: vx,
      y: vy,
      engaged,
    };
    for (const cb of [...listeners]) cb(snapshot);
  }

  function targetVector() {
    const tx = (held.has("right") ? 1 : 0) - (held.has("left") ? 1 : 0);
    const ty = (held.has("up") ? 1 : 0) - (held.has("down") ? 1 : 0);
    const scale = shift ? PRECISION_SCALE : 1;
    return { x: tx * scale, y: ty * scale };
  }

  /**
   * Move the vector's magnitude toward its target by the ramp rate for this tick.
   * @param {number} current
   * @param {number} target
   */
  function approach(current, target) {
    const rampMs = Math.abs(target) > Math.abs(current) ? RAMP_UP_MS : RAMP_DOWN_MS;
    const step = TICK_MS / rampMs;
    const delta = target - current;
    if (Math.abs(delta) <= step) return target;
    return current + Math.sign(delta) * step;
  }

  /**
   * Shortest signed rotation from one heading to another, wrapped to [-pi, pi] so
   * a sweep never takes the long way round.
   * @param {number} from
   * @param {number} to
   */
  function angleDelta(from, to) {
    return Math.atan2(Math.sin(to - from), Math.cos(to - from));
  }

  function tick() {
    const target = targetVector();
    const targetMag = Math.hypot(target.x, target.y);
    const currentMag = Math.hypot(vx, vy);

    // A zero-length vector has no heading: off rest adopt the target's, heading for the
    // origin keep the current one so we decay straight rather than spin.
    const heading = currentMag >= EPSILON ? Math.atan2(vy, vx) : Math.atan2(target.y, target.x);
    const targetHeading = targetMag >= EPSILON ? Math.atan2(target.y, target.x) : heading;
    const delta = angleDelta(heading, targetHeading);

    let nextMag;
    let nextHeading;
    if (Math.abs(delta) > REORIENT_MAX_ANGLE) {
      nextMag = approach(currentMag, 0);
      nextHeading = heading;
    } else {
      nextMag = approach(currentMag, targetMag);
      nextHeading = heading + Math.max(-ANGLE_STEP, Math.min(ANGLE_STEP, delta));
    }

    vx = clampAxis(nextMag * Math.cos(nextHeading));
    vy = clampAxis(nextMag * Math.sin(nextHeading));

    if (held.size === 0 && Math.hypot(vx, vy) < EPSILON) {
      stopLoop();
      return;
    }
    driveController.setInput("keyboard", vx, vy, true);
    emit();
  }

  function startLoop() {
    if (timer !== null) return;
    engaged = true;
    timer = setInterval(tick, TICK_MS);
    tick();
  }

  /** Stop the loop and disengage (DriveController publishes the one zero). */
  function stopLoop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
    vx = 0;
    vy = 0;
    if (engaged) {
      engaged = false;
      driveController.setInput("keyboard", 0, 0, false);
    }
    emit();
  }

  /** Immediate halt: focus loss may have eaten the keyup. */
  function clearHeld() {
    held.clear();
    shift = false;
    stopLoop();
  }

  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
    if (e.key === "Shift") {
      shift = true;
      return;
    }
    const dir = KEY_DIRS.get(e.code);
    if (!dir) return;
    if (isTypingContext()) return;
    if (e.code.startsWith("Arrow")) e.preventDefault();
    if (e.repeat) return;
    shift = e.shiftKey;
    held.add(dir);
    startLoop();
  }

  /** @param {KeyboardEvent} e */
  function onKeyUp(e) {
    // No typing guard here: a key pressed before focusing an input must
    // still release. The ramp-down loop handles the rest.
    if (e.key === "Shift") shift = false;
    const dir = KEY_DIRS.get(e.code);
    if (dir) held.delete(dir);
    emit();
  }

  function onBlurOrHide() {
    clearHeld();
    driveController.haltAll();
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") onBlurOrHide();
  }

  function onFocusIn() {
    // Keys held while the operator clicks into the TTS bar must not keep
    // driving under the typing.
    if (isTypingContext() && held.size > 0) clearHeld();
  }

  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", onBlurOrHide);
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("focusin", onFocusIn);

  return {
    /**
     * @param {(s: KeyboardDriveState) => void} cb
     * @returns {() => void} unsubscribe
     */
    onChange(cb) {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    destroy() {
      clearHeld();
      listeners.clear();
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlurOrHide);
      document.removeEventListener("visibilitychange", onVisibility);
      document.removeEventListener("focusin", onFocusIn);
    },
  };
}

/**
 * WASD hint chips (inverted-T) that light while keys are held and tint
 * amber when keyboard drive is actually moving the robot.
 * @param {HTMLElement} parent
 * @param {{ onChange: (cb: (s: KeyboardDriveState) => void) => () => void }} keyboardDrive
 * @returns {{ destroy: () => void }}
 */
export function createWasdChips(parent, keyboardDrive) {
  const wrap = document.createElement("div");
  wrap.className = "wasd";
  wrap.setAttribute("aria-hidden", "true");

  /** @type {Record<"up" | "left" | "down" | "right", HTMLElement>} */
  const chips = {
    up: chip("W"),
    left: chip("A"),
    down: chip("S"),
    right: chip("D"),
  };
  const top = document.createElement("div");
  top.className = "wasd-row";
  top.append(chips.up);
  const bottom = document.createElement("div");
  bottom.className = "wasd-row";
  bottom.append(chips.left, chips.down, chips.right);
  wrap.append(top, bottom);

  const hint = document.createElement("div");
  hint.className = "microlabel wasd-hint";
  hint.textContent = "shift = slow";
  wrap.append(hint);

  /** @param {string} label */
  function chip(label) {
    const el = document.createElement("span");
    el.className = "wasd-chip";
    el.textContent = label;
    return el;
  }

  const unsub = keyboardDrive.onChange((s) => {
    chips.up.classList.toggle("held", s.held.up);
    chips.down.classList.toggle("held", s.held.down);
    chips.left.classList.toggle("held", s.held.left);
    chips.right.classList.toggle("held", s.held.right);
    wrap.classList.toggle("driving", s.engaged);
  });

  parent.appendChild(wrap);
  return {
    destroy() {
      unsub();
      wrap.remove();
    },
  };
}
