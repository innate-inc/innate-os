// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Display-only squaring of saved maps. SLAM keeps whatever heading the robot
// started in, so buildings land at an angle; only the drawing turns — the map
// and everything placed in its frame stay exactly as saved.

const OCCUPIED = 65;
const MIN_WALL_CELLS = 50;
const COARSE_STEP = Math.PI / 180;
const FINE_STEP = COARSE_STEP / 10;
// Without a clearly sharper alignment (one cluttered room, a sparse scan) the
// best angle is noise, and the map stays as recorded.
const MIN_GAIN = 1.1;
// Only clearly oblong shapes count as portrait or landscape: a near-square
// map keeps the smaller rotation, and a near-square screen (the teleop
// thumbnail) lays maps out like the landscape Nav page it expands into.
const OBLONG = 1.15;
// The aspect ignores the outermost few wall cells: stray hits through doors
// and windows would otherwise decide it.
const SPREAD_TAIL = 0.005;

/**
 * `angle` is the dominant wall direction in the grid frame (radians CCW, in
 * (-π/4, π/4]). Every wall cell lies within `along` × `across`: ranges in
 * cells, along that direction and across it, from the grid's corner. `aspect`
 * is the walls' along/across ratio.
 * @typedef {{ angle: number, along: [number, number], across: [number, number], aspect: number }} MapOrientation
 */

/** A point in grid cells: x along the columns, y up the rows.
 * @typedef {{ x: number, y: number }} CellPoint */

/**
 * The dominant wall direction of an occupancy grid (row 0 at the bottom, as in
 * nav_msgs/OccupancyGrid) — the angle at which projecting the occupied cells
 * onto both axes piles them into the sharpest peaks — and the walls' extent.
 * @param {ArrayLike<number>} cells @param {number} width @param {number} height
 * @returns {MapOrientation}
 */
export function mapOrientation(cells, width, height) {
  /** @type {number[]} */
  const xs = [];
  /** @type {number[]} */
  const ys = [];
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      if (cells[row * width + col] >= OCCUPIED) {
        xs.push(col);
        ys.push(row);
      }
    }
  }
  if (xs.length < MIN_WALL_CELLS) return { angle: 0, along: [0, width], across: [0, height], aspect: width / Math.max(1, height) };

  const reach = Math.ceil(Math.hypot(width, height)) + 1;
  const bins = new Float64Array(2 * reach + 2);
  /** @param {number} c @param {number} s */
  const peakiness = (c, s) => {
    bins.fill(0);
    for (let i = 0; i < xs.length; i++) {
      const p = xs[i] * c + ys[i] * s + reach;
      const b = Math.floor(p);
      bins[b] += b + 1 - p;
      bins[b + 1] += p - b;
    }
    let sum = 0;
    for (let b = 0; b < bins.length; b++) sum += bins[b] * bins[b];
    return sum;
  };
  /** @param {number} theta */
  const sharpness = (theta) => peakiness(Math.cos(theta), Math.sin(theta)) + peakiness(-Math.sin(theta), Math.cos(theta));

  const unrotated = sharpness(0);
  let best = 0;
  let bestScore = unrotated;
  /** @param {number} theta */
  const consider = (theta) => {
    const score = sharpness(theta);
    if (score > bestScore) {
      best = theta;
      bestScore = score;
    }
  };
  for (let k = -44; k <= 45; k++) consider(k * COARSE_STEP);
  const coarse = best;
  for (let k = -10; k <= 10; k++) consider(coarse + k * FINE_STEP);
  const angle = bestScore >= MIN_GAIN * unrotated ? foldQuarter(best) : 0;

  const c = Math.cos(angle);
  const s = Math.sin(angle);
  const along = measure(xs.map((x, i) => (x + 0.5) * c + (ys[i] + 0.5) * s));
  const across = measure(xs.map((x, i) => (ys[i] + 0.5) * c - (x + 0.5) * s));
  return { angle, along: along.extent, across: across.extent, aspect: along.spread / Math.max(1, across.spread) };
}

/**
 * The canvas rotation (ctx.rotate, clockwise-positive) that draws a grid image
 * with its walls along the screen axes and its long side along the screen's
 * long side — never more than a quarter turn.
 * @param {MapOrientation} orientation @param {number} screenAspect width over height
 */
export function squaringRotation({ angle, aspect }, screenAspect) {
  const portrait = screenAspect * OBLONG < 1;
  const turnQuarter = portrait ? aspect > OBLONG : aspect * OBLONG < 1;
  if (!turnQuarter) return angle;
  return angle > 0 ? angle - Math.PI / 2 : angle + Math.PI / 2;
}

/**
 * The corners of the walls' extent, padded by `pad` cells — a rectangle that
 * reads upright once squared and holds every wall and the floor between them.
 * @param {MapOrientation} orientation @param {number} pad
 * @returns {CellPoint[]}
 */
export function wallOutline({ angle, along, across }, pad) {
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  const [a0, a1] = [along[0] - pad, along[1] + pad];
  const [b0, b1] = [across[0] - pad, across[1] + pad];
  return [
    [a0, b0],
    [a1, b0],
    [a1, b1],
    [a0, b1],
  ].map(([a, b]) => ({ x: a * c - b * s, y: a * s + b * c }));
}

/** @param {number} theta → the same axis pair, in (-π/4, π/4] */
function foldQuarter(theta) {
  const quarter = Math.PI / 2;
  return theta - quarter * Math.round(theta / quarter - 1e-9);
}

/** @param {number[]} values @returns {{ extent: [number, number], spread: number }} */
function measure(values) {
  values.sort((a, b) => a - b);
  const last = values.length - 1;
  const spread = values[Math.round(last * (1 - SPREAD_TAIL))] - values[Math.round(last * SPREAD_TAIL)];
  return { extent: [values[0], values[last]], spread };
}
