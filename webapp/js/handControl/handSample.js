// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Turning one MediaPipe hand result into a control sample, and that sample into
// a claw position. Ported from sim/hand_control's studio (src/control.mjs),
// minus its ground-line calibration: the webapp anchors relatively, at whatever
// pose the arm is already holding, so there is nothing to calibrate before you
// start and Recenter works anywhere.

import { fingerFrame } from "./orientation.js";
import { clamp, median } from "./math.js";

export { clamp } from "./math.js";

/** @typedef {{ x: number, y: number, z: number }} Point */
/** @typedef {import("./orientation.js").Frame} Frame */
/**
 * @typedef {{ valid: boolean, reason: string, x?: number, y?: number, scale?: number,
 *   raw?: Point[], grip?: number, gripValid?: boolean, orientation?: Frame | null }} HandSample
 */

const distance = (/** @type {Point} */ a, /** @type {Point} */ b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
const PALM = [0, 5, 9, 13, 17];

// Normalized thumb/index gap: below this the jaws are shut, this much above it
// they are fully open. Palm-relative, so leaning toward the camera is not a grab.
const GRIP_CLOSED = 0.18;
const GRIP_SPAN = 0.95;

/**
 * @param {{ landmarks: Point[][], worldLandmarks?: Point[][], handedness?: { categoryName: string }[][] }} result
 * @param {number} width @param {number} height
 * @returns {HandSample}
 */
export function measureHand(result, width = 640, height = 480) {
  if (result.landmarks.length !== 1)
    return { valid: false, reason: result.landmarks.length ? "Use one hand" : "Bring your hand back into view" };
  const raw = result.landmarks[0];
  if (raw.length !== 21 || raw.some((p) => ![p.x, p.y, p.z].every(Number.isFinite)))
    return { valid: false, reason: "Finding your hand" };
  const p = raw.map((q) => ({ x: q.x * width, y: q.y * height, z: q.z * width }));
  const center = PALM.reduce((a, i) => ({ x: a.x + raw[i].x / 5, y: a.y + raw[i].y / 5 }), { x: 0, y: 0 });
  // Palm bones keep their length when fingers close. Including relative landmark
  // depth reduces the apparent reach change when the palm turns sideways.
  const palmSize = Math.sqrt((distance(p[0], p[9]) ** 2 + distance(p[5], p[17]) ** 2) / 2);
  const scale = palmSize / width;
  const grip = clamp((distance(p[4], p[8]) / palmSize - GRIP_CLOSED) / GRIP_SPAN, 0, 1);
  const inside = (/** @type {Point} */ q) => q.x > 0.01 && q.x < 0.99 && q.y > 0.01 && q.y < 0.99;
  const gripValid = [4, 8].every((i) => inside(raw[i]));
  const world = result.worldLandmarks?.[0];
  const metric = world?.length === 21 && world.every((q) => [q.x, q.y, q.z].every(Number.isFinite)) ? world : p;
  const sample = {
    x: 1 - center.x, // mirrored: the preview is a mirror, so is the control
    y: center.y,
    scale,
    raw,
    grip,
    gripValid,
    orientation: gripValid ? fingerFrame(metric) : null,
  };
  if (scale < 0.035) return { ...sample, valid: false, reason: "Bring your hand a little closer" };
  if (!PALM.every((i) => inside(raw[i]))) return { ...sample, valid: false, reason: "Keep your palm in the picture" };
  return { ...sample, valid: true, reason: "Hand tracked" };
}

/** A hold: the hand must sit still for `duration` before it counts as a reference. */
export class Calibration {
  /** @type {(HandSample & { now: number })[]} */ samples = [];
  /** @param {number} duration ms @param {number} minFrames */
  constructor(duration = 800, minFrames = 8) {
    this.duration = duration;
    this.minFrames = minFrames;
  }
  reset() {
    this.samples = [];
  }
  /** @param {HandSample} sample @param {number} now */
  update(sample, now) {
    if (!sample.valid) {
      this.reset();
      return { progress: 0, ready: false };
    }
    this.samples.push({ .../** @type {any} */ (sample), now });
    this.samples = this.samples.filter((s) => now - s.now <= this.duration + 400);
    const center = {
      x: median(this.samples.map((s) => /** @type {number} */ (s.x))),
      y: median(this.samples.map((s) => /** @type {number} */ (s.y))),
      scale: median(this.samples.map((s) => /** @type {number} */ (s.scale))),
    };
    const stable = this.samples.every(
      (s) =>
        Math.abs(/** @type {number} */ (s.x) - center.x) < 0.035 &&
        Math.abs(/** @type {number} */ (s.y) - center.y) < 0.045 &&
        Math.abs(Math.log(/** @type {number} */ (s.scale) / center.scale)) < 0.12,
    );
    if (!stable) {
      this.samples = [{ .../** @type {any} */ (sample), now }];
      return { progress: 0, ready: false };
    }
    const held = now - this.samples[0].now;
    return { progress: clamp(held / this.duration, 0, 1), ready: held >= this.duration && this.samples.length >= this.minFrames };
  }
}

/** Whether `sample` is plausibly the same hand as `previous`.
 * @param {HandSample | null} previous @param {HandSample} sample */
export function continuousHand(previous, sample) {
  // Handedness classification flips as a hand turns; position and scale
  // continuity is the better guard against jumping to a newly detected hand.
  return (
    !previous ||
    (Math.hypot(
      /** @type {number} */ (sample.x) - /** @type {number} */ (previous.x),
      /** @type {number} */ (sample.y) - /** @type {number} */ (previous.y),
    ) < 0.22 &&
      Math.abs(Math.log(/** @type {number} */ (sample.scale) / /** @type {number} */ (previous.scale))) < 0.55)
  );
}

const POSITION_SMOOTHING_S = 0.07;
const GRIP_SMOOTHING_S = 0.055;

/** Hand movement -> a normalized claw position in [-1, 1] per axis
 * (sideways, height, reach), relative to wherever following was anchored. */
export class HandMapper {
  /** @type {HandSample | null} */ neutral = null;
  /** @type {[number, number, number]} */ value = [0, 0, 0];
  /** @type {[number, number, number]} */ base = [0, 0, 0];
  grip = 1;
  /** @type {number | null} */ last = null;
  sensitivity = 1;

  /** @param {HandSample} sample @param {[number, number, number]} offset @param {number} [grip] */
  anchor(sample, offset, grip = this.grip) {
    this.neutral = { ...sample };
    this.base = [...offset];
    this.value = [...offset];
    this.grip = grip;
    this.last = null;
  }

  /** Where the hand asks the claw to be, per axis, before smoothing.
   * @param {HandSample} sample @returns {[number, number, number]} */
  wanted(sample) {
    const n = /** @type {HandSample} */ (this.neutral);
    const deadzone = (/** @type {number} */ value, /** @type {number} */ dead) =>
      Math.sign(value) * Math.max(0, Math.abs(value) - dead);
    // Sideways (mirrored), image height, then approach/retract: moving toward
    // the webcam reaches the claw forward.
    const movement = [
      deadzone((/** @type {number} */ (sample.x) - /** @type {number} */ (n.x)) * 3.8, 0.025),
      deadzone((/** @type {number} */ (n.y) - /** @type {number} */ (sample.y)) * 4, 0.025),
      deadzone(Math.log(/** @type {number} */ (sample.scale) / /** @type {number} */ (n.scale)) / 0.5, 0.05),
    ];
    return /** @type {[number, number, number]} */ (movement.map((v, i) => clamp(this.base[i] + v * this.sensitivity)));
  }

  /** @param {HandSample} sample @param {number} now
   * @returns {{ position: [number, number, number], grip: number } | null} */
  map(sample, now) {
    if (!this.neutral || !sample.valid) return null;
    const wanted = this.wanted(sample);
    const dt = this.last === null ? 1 / 30 : clamp((now - this.last) / 1000, 0, 0.1);
    this.last = now;
    const alpha = 1 - Math.exp(-dt / POSITION_SMOOTHING_S);
    this.value = /** @type {[number, number, number]} */ (this.value.map((v, i) => v + (wanted[i] - v) * alpha));
    if (sample.gripValid)
      this.grip += (/** @type {number} */ (sample.grip) - this.grip) * (1 - Math.exp(-dt / GRIP_SMOOTHING_S));
    return { position: [...this.value], grip: this.grip };
  }
}
