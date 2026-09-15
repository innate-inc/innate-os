// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Hand orientation -> claw orientation. Ported from sim/hand_control's studio
// (src/orientation.mjs), where the mapping was tuned against a measured MuJoCo
// MARS; the only change here is that the limits are asymmetric, so the claw can
// tilt far enough down to take something off the floor while keeping the
// studio's modest upward range.
//
// All angles are radians. The camera-axis permutation maps screen twist to
// wrist roll, hand tilt to pitch, and turning sideways to the base swivel.

/** @typedef {number[]} Frame row-major 3x3 rotation */
/** @typedef {{ x: number, y: number, z: number }} Point */

const sub = (/** @type {number[]} */ a, /** @type {number[]} */ b) => a.map((v, i) => v - b[i]);
const dot = (/** @type {number[]} */ a, /** @type {number[]} */ b) => a.reduce((sum, v, i) => sum + v * b[i], 0);
const scale = (/** @type {number[]} */ a, /** @type {number} */ s) => a.map((v) => v * s);
const length = (/** @type {number[]} */ a) => Math.hypot(...a);
const unit = (/** @type {number[]} */ a) => scale(a, 1 / length(a));
const cross = (/** @type {number[]} */ a, /** @type {number[]} */ b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const project = (/** @type {number[]} */ a, /** @type {number[]} */ axis) => sub(a, scale(axis, dot(a, axis)));
const clamp = (/** @type {number} */ v, /** @type {number} */ lo, /** @type {number} */ hi) =>
  Math.max(lo, Math.min(hi, v));
const wrap = (/** @type {number} */ angle) => Math.atan2(Math.sin(angle), Math.cos(angle));

/**
 * A 3D frame for the pinch, from the wrist and the thumb/index chain.
 * @param {Point[]} points 21 landmarks, world (metric) or image
 * @returns {Frame | null} null when the landmarks are degenerate
 */
export function fingerFrame(points) {
  // Three non-collinear landmarks are necessary for a 3D frame. The wrist
  // and thumb/index bases stabilize it when the fingertips meet in a pinch.
  const p = points.map((q) => [q.z, q.x, q.y]);
  const palmForward = sub(p[5], p[0]);
  const palm = length(palmForward);
  if (palm < 1e-6) return null;
  const palmAxis = unit(palmForward);
  const base = project(sub(p[5], p[2]), palmAxis);
  if (length(base) < palm * 0.08) return null;
  let gap = project(sub(p[8], p[4]), palmAxis);
  const gapLength = length(gap);
  const baseAxis = unit(base);
  let y = baseAxis;
  if (gapLength > palm * 0.05) {
    gap = unit(gap);
    if (dot(gap, baseAxis) < 0) gap = scale(gap, -1);
    const weight = clamp((gapLength / palm - 0.05) / 0.35, 0, 1);
    y = unit(baseAxis.map((v, i) => v * (1 - weight) + gap[i] * weight));
  }
  // The claw points from the finger bases toward the midpoint of the two tips.
  // Removing the jaw-axis component makes opening the pinch lateral to that
  // axis leave the pointing direction unchanged -- otherwise a plain grab
  // rolls the claw.
  const fingerForward = p[8].map((v, i) => (v + p[4][i] - p[5][i] - p[2][i]) / 2);
  const pointing = project(fingerForward, y);
  if (length(pointing) < palm * 0.12) return null;
  const x = unit(pointing);
  const z = unit(cross(x, y));
  y = cross(z, x);
  return [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
}

/** (roll, pitch, yaw) of `current` relative to `reference`.
 * @param {Frame} current @param {Frame} reference @returns {[number, number, number]} */
export function relativeAngles(current, reference) {
  const r = Array.from({ length: 9 }, (_, i) => {
    const row = Math.floor(i / 3);
    const column = i % 3;
    return [0, 1, 2].reduce((s, k) => s + current[row * 3 + k] * reference[column * 3 + k], 0);
  });
  return [Math.atan2(r[7], r[8]), Math.asin(clamp(-r[6], -1, 1)), Math.atan2(r[3], r[0])];
}

// Roll and yaw stay symmetric; pitch reaches much further down than up, which
// is what lets the claw come off level and face the floor for a low grasp.
/** @type {[number, number][]} */
export const WRIST_LIMITS = [
  [-1.2, 1.2],
  [-0.7, 1.45],
  [-0.9, 0.9],
];

const DEAD_ZONE = 0.025;
const SMOOTHING_S = 0.1;

/** Claw roll/pitch/yaw tracking the hand, anchored where following started. */
export class WristMapper {
  /** @type {Frame | null} */ reference = null;
  /** @type {[number, number, number]} */ base = [0, 0, 0];
  /** @type {[number, number, number]} */ value = [0, 0, 0];
  /** @type {[number, number, number]} */ unwrapped = [0, 0, 0];
  /** @type {number | null} */ last = null;
  /** @type {[number, number][]} */ limits = WRIST_LIMITS;

  /** @param {Frame | null} frame @param {[number, number, number]} [wrist] the claw's current angles */
  anchor(frame, wrist = [0, 0, 0]) {
    this.reference = frame;
    this.base = [...wrist];
    this.value = [...wrist];
    this.unwrapped = [0, 0, 0];
    this.last = null;
  }

  /** @param {Frame | null} frame @param {number} now ms
   * @returns {[number, number, number]} roll, pitch, yaw -- held when the frame is missing */
  update(frame, now) {
    if (!frame) return [...this.value];
    if (!this.reference) this.reference = frame;
    const relative = relativeAngles(frame, this.reference);
    const dt = this.last === null ? 1 / 30 : clamp((now - this.last) / 1000, 0, 0.1);
    this.last = now;
    const alpha = 1 - Math.exp(-dt / SMOOTHING_S);
    this.value = /** @type {[number, number, number]} */ (
      relative.map((angle, i) => {
        this.unwrapped[i] += wrap(angle - this.unwrapped[i]);
        const delta = Math.sign(this.unwrapped[i]) * Math.max(0, Math.abs(this.unwrapped[i]) - DEAD_ZONE);
        const [lo, hi] = this.limits[i];
        const wanted = clamp(this.base[i] + delta, lo, hi);
        // Consume the blocked part of the turn at a limit, so reversing the
        // hand moves the claw straight away instead of unwinding slack first.
        if (this.base[i] + delta > hi || this.base[i] + delta < lo) this.base[i] = wanted - delta;
        return this.value[i] + (wanted - this.value[i]) * alpha;
      })
    );
    return [...this.value];
  }
}
