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
// A wall cell sets the walls' extent only with this many wall cells (itself
// included) in the 3 × 3 buckets around it: a real wall, however far, arrives
// as a run of hits, while a lone stray through a door or window must not
// stretch the outline.
const SUPPORT_BUCKET = 20; // cells: 1 m on MARS's 5 cm maps
const MIN_SUPPORT = 5;

/**
 * `angle` is the dominant wall direction in the grid frame (radians CCW, in
 * (-π/4, π/4]). The walls lie within `along` × `across`: ranges in cells,
 * along that direction and across it, from the grid's corner.
 * @typedef {{ angle: number, along: [number, number], across: [number, number] }} MapOrientation
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
  if (xs.length < MIN_WALL_CELLS) return { angle: 0, along: [0, width], across: [0, height] };

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
  /** @type {[number, number]} */
  const along = [Infinity, -Infinity];
  /** @type {[number, number]} */
  const across = [Infinity, -Infinity];
  for (const i of supportedWalls(xs, ys, width, height)) {
    const a = (xs[i] + 0.5) * c + (ys[i] + 0.5) * s;
    const b = (ys[i] + 0.5) * c - (xs[i] + 0.5) * s;
    along[0] = Math.min(along[0], a);
    along[1] = Math.max(along[1], a);
    across[0] = Math.min(across[0], b);
    across[1] = Math.max(across[1], b);
  }
  return { angle, along, across };
}

/**
 * The canvas rotation (ctx.rotate, clockwise-positive) that draws a grid image
 * with its walls along the screen axes and its long side along the screen's
 * long side — never more than a quarter turn.
 * @param {MapOrientation} orientation @param {number} screenAspect width over height
 */
export function squaringRotation({ angle, along, across }, screenAspect) {
  const aspect = (along[1] - along[0]) / Math.max(1, across[1] - across[0]);
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

/**
 * Indices of the wall cells with company enough to set the extent (see
 * MIN_SUPPORT); all of them when none has, so the extent is never empty.
 * @param {number[]} xs @param {number[]} ys @param {number} width @param {number} height
 */
function supportedWalls(xs, ys, width, height) {
  const cols = Math.ceil(width / SUPPORT_BUCKET);
  const rows = Math.ceil(height / SUPPORT_BUCKET);
  const counts = new Int32Array(cols * rows);
  const bx = xs.map((x) => Math.floor(x / SUPPORT_BUCKET));
  const by = ys.map((y) => Math.floor(y / SUPPORT_BUCKET));
  bx.forEach((col, i) => counts[by[i] * cols + col]++);
  const all = xs.map((_, i) => i);
  const supported = all.filter((i) => {
    let n = 0;
    for (let row = Math.max(0, by[i] - 1); row <= Math.min(rows - 1, by[i] + 1); row++) {
      for (let col = Math.max(0, bx[i] - 1); col <= Math.min(cols - 1, bx[i] + 1); col++) n += counts[row * cols + col];
    }
    return n >= MIN_SUPPORT;
  });
  return supported.length ? supported : all;
}
