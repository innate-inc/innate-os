// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tick/limit/current math for the leader-arm reachability guard. Pure — no
// serial, no DOM, no state — so the arithmetic that decides where a servo
// stops and how hard it may push can be reasoned about on its own.

const TICKS_PER_REV = 4096;
const TICK_CENTER = 2048;

/** @typedef {{ min: number, max: number }} Band Reachable tick range, inclusive. */

/** @param {number} rad @returns {number} */
export function radToTick(rad) {
  return TICK_CENTER + (rad * TICKS_PER_REV) / (2 * Math.PI);
}

/** @param {number} tick @returns {number} */
export function tickToRad(tick) {
  return ((tick - TICK_CENTER) * 2 * Math.PI) / TICKS_PER_REV;
}

/**
 * A follower joint's [lo, hi] radian limits as a tick band in the frame the
 * leader publishes. A flipped joint's band mirrors to [-hi, -lo], because
 * mars_arm negates the command before it reaches the servo. Rounds inward so a
 * clamped tick always converts back to a radian the follower accepts — rounding
 * outward would hand it a goal one tick past its own limit.
 * @param {number[]} positionLimits
 * @param {boolean} [flipped]
 * @returns {Band | null} null if the parameter isn't a usable pair.
 */
export function limitsToBand(positionLimits, flipped = false) {
  if (positionLimits.length !== 2) return null;
  const [lo, hi] = positionLimits;
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo >= hi) return null;
  const [min, max] = flipped ? [-hi, -lo] : [lo, hi];
  return { min: Math.ceil(radToTick(min)), max: Math.floor(radToTick(max)) };
}

/**
 * Joint 2's floor given where joint 1 is, in radians. Full travel with the arm
 * swung clear behind the ramp, tightened to `restrictedMin` across the front arc
 * where lowering joint 2 folds the arm into the body, and linearly interpolated
 * between. Mirrors arm_control.cpp so the operator feels the same boundary the
 * follower enforces instead of silently diverging from it.
 * @param {number} joint1Rad
 * @param {number} baseMinRad Joint 2's unrestricted floor.
 * @param {{ restrictedMin: number, arcLo: number, arcHi: number, rampLo: number, rampHi: number }} shape
 * @returns {number}
 */
export function joint2FloorRad(joint1Rad, baseMinRad, shape) {
  const { restrictedMin, arcLo, arcHi, rampLo, rampHi } = shape;
  if (restrictedMin <= baseMinRad) return baseMinRad;
  const ramp = (/** @type {number} */ t) => restrictedMin + t * (baseMinRad - restrictedMin);
  if (joint1Rad < rampLo || joint1Rad >= rampHi) return baseMinRad;
  if (joint1Rad < arcLo) return Math.max(baseMinRad, ramp((arcLo - joint1Rad) / (arcLo - rampLo)));
  if (joint1Rad < arcHi) return Math.max(baseMinRad, restrictedMin);
  return Math.max(baseMinRad, ramp((joint1Rad - arcHi) / (rampHi - arcHi)));
}

/** @param {number} tick @param {Band} band @returns {number} */
export function clampTick(tick, band) {
  return Math.min(band.max, Math.max(band.min, tick));
}

/**
 * Whether the joint counts as out of reach, with hysteresis on the way back:
 * re-entry needs `hysteresis` ticks past the boundary. Without it a joint
 * resting exactly on the limit flickers, and each flicker is an EEPROM-free but
 * still pointless torque on/off cycle.
 * @param {number} tick
 * @param {Band} band
 * @param {number} hysteresis
 * @param {boolean} wasOutside
 * @returns {boolean}
 */
export function isOutside(tick, band, hysteresis, wasOutside) {
  if (!wasOutside) return tick < band.min || tick > band.max;
  return tick < band.min + hysteresis || tick > band.max - hysteresis;
}

/**
 * Fraction of the way into the danger zone at each end, 0 well inside and 1 at
 * the boundary — drives the amber warning before the wall is reached.
 * @param {number} tick @param {Band} band @param {number} warnTicks
 * @returns {number}
 */
export function proximity(tick, band, warnTicks) {
  if (warnTicks <= 0) return 0;
  const slack = Math.min(tick - band.min, band.max - tick);
  if (slack >= warnTicks) return 0;
  return Math.min(1, Math.max(0, 1 - slack / warnTicks));
}

/**
 * Per-servo goal current when `activeCount` servos hold at once. The budget is
 * a ceiling on the sum, so it divides rather than applying to each.
 * @param {number} activeCount
 * @param {number} budgetMa
 * @returns {number}
 */
export function allocateCurrent(activeCount, budgetMa) {
  if (activeCount <= 0) return 0;
  return Math.max(0, Math.floor(budgetMa / activeCount));
}

/**
 * Total draw. Present Current is signed by direction, so magnitude is what
 * counts against a power budget.
 * @param {number[]} currents
 * @returns {number}
 */
export function totalCurrent(currents) {
  return currents.reduce((sum, mA) => sum + Math.abs(mA), 0);
}
