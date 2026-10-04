// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// JS port of the expressive core (brain_client/expressive: channels, dsl, plan,
// basis, liveliness) so the studio turns a recipe into motion with no robot.
// Pure: no DOM, no ROS. The Python core is the reference — tests/expression.test.js
// holds this port to its golden fixture (expressive/fixtures/golden.json).

export const FPS = 25;

/** Plan-space channels, CONTRACTS §1 — the order is frozen (the planner is trained on it). */
export const CHANNELS = /** @type {const} */ ([
  "approach",
  "expand",
  "rise",
  "attend",
  "askew",
  "orient",
  "advance",
  "grip",
  "energy",
]);
/** @typedef {typeof CHANNELS[number]} Channel */

/** The 8 channels a clip carries per frame (energy is plan-only). */
export const MOTION_CHANNELS = CHANNELS.slice(0, 8);
export const ENERGY = 8;

/** DSL key for each channel, same order. */
export const DSL_KEYS = ["a", "x", "z", "p", "k", "b", "d", "g", "E"];
export const NEUTRAL = [0, 0, 0, 0, 0, 0, 0, 0.15, 0.5];

/** @type {Record<string, [number, number]>} */
export const LIMITS = {
  a: [-1, 1],
  x: [-1, 1],
  z: [-1, 1],
  p: [-1, 1],
  k: [-1, 1],
  b: [-60, 60],
  d: [-0.25, 0.25],
  g: [0, 1],
  E: [0, 12],
};

/** @typedef {number[]} Row one frame, indexed like CHANNELS */
/** @typedef {{ t: number } & Record<Channel, number>} PlanKey */
/** @typedef {{ duration: number, keys: PlanKey[] }} Plan */

export class RecipeError extends Error {}

const MIN_SEGMENT_S = 0.05;
const MAX_SEGMENT_S = 10;
const MAX_TOTAL_S = 30;
const MIN_OSC_PERIOD_S = 0.3;
const OSC_RAMP_S = 0.3;
const KEY_LIST = DSL_KEYS.join(" ");
/** The prompt names each osc channel by its gesture ("nod (p)"); planners often write the word. @type {Record<string, string>} */
const OSC_ALIASES = { nod: "p", bob: "z", sway: "k", lean: "a", turn: "b", chatter: "g" };
/** Channels a variant's amplitude scale applies to (not grip, not energy). */
const SCALED = new Set([0, 1, 2, 3, 4, 5, 6]);

/** @typedef {{ rand?: () => number, amp?: number, tempo?: number }} ExpandOptions */

/** @param {() => number} rand @param {number} lo @param {number} hi */
const uniform = (rand, lo, hi) => lo + (hi - lo) * rand();

/** Frames covering `seconds`, rounding half up — the core's frame_count. @param {number} seconds */
export const frameCount = (seconds) => Math.floor(seconds * FPS + 0.5);

// ---- Python formatting, so error strings match the core's (they are the planner's repair hints) ----

/** repr() of a str. @param {string} s */
function pyRepr(s) {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  const body = s
    .replace(/\\/g, "\\\\")
    .replace(/\n/g, "\\n")
    .replace(/\r/g, "\\r")
    .replace(/\t/g, "\\t")
    .replaceAll(quote, `\\${quote}`);
  return quote + body + quote;
}

/** format(v, "g"). @param {number} v */
function pyG(v) {
  if (!Number.isFinite(v)) return Number.isNaN(v) ? "nan" : v > 0 ? "inf" : "-inf";
  if (v === 0) return Object.is(v, -0) ? "-0" : "0";
  const [mantissa, exponent] = v.toExponential(5).split("e");
  const exp = Number(exponent);
  if (exp < -4 || exp >= 6) {
    const m = mantissa.includes(".") ? mantissa.replace(/\.?0+$/, "") : mantissa;
    return `${m}e${exp < 0 ? "-" : "+"}${String(Math.abs(exp)).padStart(2, "0")}`;
  }
  const fixed = v.toFixed(5 - exp);
  return fixed.includes(".") ? fixed.replace(/\.?0+$/, "") : fixed;
}

/** format(v, ".1f") — round half to even on exact binary ties, as Python does. @param {number} v */
function pyFixed1(v) {
  const scaled = v * 10;
  const floor = Math.floor(scaled);
  if (scaled - floor === 0.5 && floor % 2 === 0) return (floor / 10).toFixed(1);
  return v.toFixed(1);
}

const PY_FLOAT = /^[+-]?(\d(_?\d)*(\.(\d(_?\d)*)?)?|\.\d(_?\d)*)([eE][+-]?\d(_?\d)*)?$/;

/** @param {string} token @param {string} segment */
function parseNumber(token, segment) {
  const value = PY_FLOAT.test(token) ? Number(token.replaceAll("_", "")) : NaN;
  if (!Number.isFinite(value)) throw new RecipeError(`bad number ${pyRepr(token)} in ${pyRepr(segment)}`);
  return value;
}

/** Apply `k=v` tokens onto `target`. @param {string[]} tokens @param {Row} target @param {number} amp @param {string} segment */
function applyTargets(tokens, target, amp, segment) {
  for (const token of tokens) {
    const eq = token.indexOf("=");
    if (eq < 0) throw new RecipeError(`expected key=value, got ${pyRepr(token)} in ${pyRepr(segment)}`);
    const key = token.slice(0, eq);
    const index = DSL_KEYS.indexOf(key);
    if (index < 0) throw new RecipeError(`unknown channel ${pyRepr(key)} (use ${KEY_LIST})`);
    const value = parseNumber(token.slice(eq + 1), segment);
    const [lo, hi] = LIMITS[key];
    if (!(lo <= value && value <= hi)) throw new RecipeError(`${key}=${pyG(value)} outside [${pyG(lo)}, ${pyG(hi)}]`);
    target[index] = SCALED.has(index) ? value * amp : value;
  }
}

/** numpy.linspace(start, stop, n). @param {number} start @param {number} stop @param {number} n */
function linspace(start, stop, n) {
  if (n === 1) return [start];
  const step = (stop - start) / (n - 1);
  return Array.from({ length: n }, (_, i) => (i === n - 1 ? stop : start + i * step));
}

/** @param {Row} a @param {Row} b */
const samePosture = (a, b) => a.slice(0, ENERGY).every((v, j) => v === b[j]);

/** @param {Row} start @param {Row} target @param {number} count @returns {Row[]} */
function cosineEase(start, target, count) {
  return Array.from({ length: count }, (_, i) => {
    const w = 0.5 - 0.5 * Math.cos((Math.PI * (i + 1)) / count);
    return start.map((c, j) => c + w * (target[j] - c));
  });
}

/**
 * @param {string[]} tokens @param {Row} current @param {number} count @param {string} segment
 * @param {ExpandOptions} opts @returns {Row[]}
 */
function osc(tokens, current, count, segment, { rand, amp = 1 }) {
  if (tokens.length < 5) throw new RecipeError(`osc needs: osc D ch amp period, got ${pyRepr(segment)}`);
  const channel = OSC_ALIASES[tokens[2]] ?? tokens[2];
  const index = DSL_KEYS.indexOf(channel);
  if (index < 0 || channel === "E") {
    throw new RecipeError(
      `osc on unknown channel ${pyRepr(channel)} (use ${KEY_LIST.slice(0, -2)} or ${Object.keys(OSC_ALIASES).join(" ")})`,
    );
  }
  let amplitude = parseNumber(tokens[3], segment);
  let period = parseNumber(tokens[4], segment);
  if (period < MIN_OSC_PERIOD_S) {
    throw new RecipeError("osc period below 0.3 s: fast shaking belongs in E (energy), not osc");
  }
  const [lo, hi] = LIMITS[channel];
  if (Math.abs(amplitude) > hi - lo) {
    throw new RecipeError(`osc amplitude ${pyG(amplitude)} too big for ${channel} (range ${pyG(lo)}..${pyG(hi)})`);
  }
  const target = current.slice();
  applyTargets(tokens.slice(5), target, amp, segment);
  if (!samePosture(target, current)) {
    throw new RecipeError(`osc only changes energy (E=v); move with go: ${pyRepr(segment)}`);
  }
  if (SCALED.has(index)) amplitude *= amp;
  if (rand) {
    amplitude *= uniform(rand, 0.8, 1.2);
    period *= uniform(rand, 0.85, 1.15);
  }
  const energy = linspace(current[ENERGY], target[ENERGY], count);
  const last = count / FPS;
  return Array.from({ length: count }, (_, i) => {
    const u = (i + 1) / FPS;
    const envelope = Math.min(1, Math.min(u, last - u) / OSC_RAMP_S);
    const row = current.slice();
    row[ENERGY] = energy[i];
    row[index] += amplitude * Math.sin((2 * Math.PI * u) / period) * envelope;
    return row;
  });
}

/** @param {Row} row */
const clampRow = (row) =>
  row.map((v, j) => {
    const [lo, hi] = LIMITS[DSL_KEYS[j]];
    return Math.min(hi, Math.max(lo, v));
  });

/**
 * Recipe → (T, 9) frames at 25 Hz, first row NEUTRAL, clamped to the channel
 * ranges (the core's dsl.expand). Exact unless `rand` is given: then each
 * segment's timing gets ±15 % jitter and each osc ±20 % amplitude; `amp`
 * scales the posture targets and `tempo` the durations (>1 = slower).
 * @param {string} recipe @param {ExpandOptions} [opts] @returns {Row[]}
 */
export function expandRecipe(recipe, opts = {}) {
  const { rand, amp = 1, tempo = 1 } = opts;
  const segments = recipe
    .split("|")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!segments.length) throw new RecipeError("empty recipe");
  let current = NEUTRAL.slice();
  /** @type {Row[]} */
  const rows = [current];
  let written = 0;
  for (const segment of segments) {
    const tokens = segment.split(/\s+/);
    const command = tokens[0];
    if (!["go", "hold", "osc"].includes(command) || tokens.length < 2) {
      throw new RecipeError(`bad segment ${pyRepr(segment)} (use go, hold, osc)`);
    }
    const seconds = parseNumber(tokens[1], segment);
    if (!(MIN_SEGMENT_S <= seconds && seconds <= MAX_SEGMENT_S)) {
      throw new RecipeError(`duration ${pyG(seconds)} outside [${MIN_SEGMENT_S}, ${MAX_SEGMENT_S}] s`);
    }
    written += seconds;
    if (written > MAX_TOTAL_S + 1e-9) {
      throw new RecipeError(`recipe lasts ${pyFixed1(written)} s; keep it under ${MAX_TOTAL_S} s`);
    }
    const count = Math.max(1, frameCount(seconds * (tempo * (rand ? uniform(rand, 0.85, 1.15) : 1))));
    /** @type {Row[]} */
    let block;
    if (command === "osc") {
      block = osc(tokens, current, count, segment, opts);
    } else {
      const target = current.slice();
      applyTargets(tokens.slice(2), target, amp, segment);
      if (command === "hold" && !samePosture(target, current)) {
        throw new RecipeError(`hold only changes energy (E=v); move with go: ${pyRepr(segment)}`);
      }
      block = cosineEase(current, target, count);
    }
    rows.push(...block);
    current = block[block.length - 1].slice();
  }
  return rows.map(clampRow);
}

/** null when the recipe is valid, else the error message (the planner's repair hint). @param {string} recipe */
export function checkRecipe(recipe) {
  try {
    expandRecipe(recipe);
    return null;
  } catch (err) {
    if (err instanceof RecipeError) return err.message;
    throw err;
  }
}

// ---- low-pass: scipy filtfilt(*butter(2, fc / (fps / 2)), padtype="odd"), as the core's plan.lowpass ----

/** @param {number} fc @param {number} [fps] */
export function butter2(fc, fps = FPS) {
  const k = Math.tan((Math.PI * fc) / fps);
  const norm = 1 / (1 + Math.SQRT2 * k + k * k);
  const b0 = k * k * norm;
  return { b: [b0, 2 * b0, b0], a: [1, 2 * (k * k - 1) * norm, (1 - Math.SQRT2 * k + k * k) * norm] };
}

/** Transposed direct form II. @param {number[]} b @param {number[]} a @param {number[]} x @param {number} z0 @param {number} z1 */
function lfilter(b, a, x, z0, z1) {
  return x.map((xi) => {
    const yi = b[0] * xi + z0;
    z0 = b[1] * xi - a[1] * yi + z1;
    z1 = b[2] * xi - a[2] * yi;
    return yi;
  });
}

/** @param {{b: number[], a: number[]}} ba @param {number[]} x @param {number} pad */
function filtfilt({ b, a }, x, pad) {
  const r0 = b[1] - a[1] * b[0];
  const r1 = b[2] - a[2] * b[0];
  const zi0 = (r0 + r1) / (1 + a[1] + a[2]);
  const zi1 = r1 - a[2] * zi0;
  const n = x.length;
  const ext = [];
  for (let i = pad; i >= 1; i--) ext.push(2 * x[0] - x[i]);
  ext.push(...x);
  for (let i = n - 2; i >= n - 1 - pad; i--) ext.push(2 * x[n - 1] - x[i]);
  const forward = lfilter(b, a, ext, zi0 * ext[0], zi1 * ext[0]);
  const y0 = forward[forward.length - 1];
  const backward = lfilter(b, a, forward.reverse(), zi0 * y0, zi1 * y0).reverse();
  return backward.slice(pad, pad + n);
}

/**
 * Zero-phase low-pass of each column of `rows`; under 16 frames it is the identity.
 * @param {number[][]} rows @param {number} fc @param {number} [fps] @returns {number[][]}
 */
export function lowpass(rows, fc, fps = FPS) {
  if (rows.length < 16) return rows.map((r) => r.slice());
  const ba = butter2(fc, fps);
  const pad = Math.min(rows.length - 1, 9);
  const cols = rows[0].map((_, j) =>
    filtfilt(
      ba,
      rows.map((r) => r[j]),
      pad,
    ),
  );
  return rows.map((_, i) => cols.map((col) => col[i]));
}

// ---- plan ----

export const SERVE_KDT = 0.25;
export const SERVE_FC = 2.0;

/**
 * (T, 9) expanded frames → plan: posture low-passed at `fc` Hz (0 = raw), a
 * key every `kdt` s, energy read raw. The core's plan.to_plan with serving defaults.
 * @param {Row[]} frames @param {{ kdt?: number, fc?: number }} [opts] @returns {Plan}
 */
export function toPlan(frames, { kdt = SERVE_KDT, fc = SERVE_FC } = {}) {
  const count = frames.length;
  const raw = frames.map((r) => r.slice(0, ENERGY));
  const posture = fc ? lowpass(raw, fc) : raw;
  /** @type {PlanKey[]} */
  const keys = [];
  const duration = (count - 1) / FPS;
  const times = Array.from({ length: Math.floor(duration / kdt + 1e-9) + 1 }, (_, k) => k * kdt);
  // the recipe's final partial interval (often its release) gets a key too, as in the core
  if (duration - times[times.length - 1] > 1e-9) times.push(duration);
  for (const t of times) {
    const i = Math.min(count - 1, frameCount(t));
    const key = /** @type {PlanKey} */ ({ t });
    MOTION_CHANNELS.forEach((c, j) => {
      key[c] = posture[i][j];
    });
    key.energy = Math.max(0, frames[i][ENERGY]);
    keys.push(key);
  }
  return { duration: (count - 1) / FPS, keys };
}

/** numpy.interp for ascending `xs`. @param {number} x @param {number[]} xs @param {number[]} ys */
function interp(x, xs, ys) {
  const last = xs.length - 1;
  if (x <= xs[0]) return ys[0];
  if (x >= xs[last]) return ys[last];
  let hi = 1;
  while (xs[hi] <= x) hi++;
  const lo = hi - 1;
  if (xs[lo] === x) return ys[lo];
  const slope = (ys[hi] - ys[lo]) / (xs[hi] - xs[lo]);
  return slope * (x - xs[lo]) + ys[lo];
}

/**
 * Plan → (T, 9) per-frame conditioning, linear between keys; a channel missing
 * from a key holds the previous key's value (NEUTRAL before the first).
 * @param {Plan} plan @param {number} [count] @returns {Row[]}
 */
export function planFrames(plan, count) {
  const n = count || Math.max(2, frameCount(plan.duration) + 1);
  const keys = [...plan.keys].sort((p, q) => p.t - q.t);
  const lastSeen = NEUTRAL.slice();
  const ts = keys.map((k) => k.t);
  const cols = CHANNELS.map(() => /** @type {number[]} */ ([]));
  for (const key of keys) {
    CHANNELS.forEach((c, j) => {
      if (typeof key[c] === "number") lastSeen[j] = key[c];
      cols[j].push(lastSeen[j]);
    });
  }
  return Array.from({ length: n }, (_, i) => cols.map((col) => interp(i / FPS, ts, col)));
}

/**
 * `n` randomised plans of one recipe (the core's dsl.variants): amplitude
 * ×0.75-1.25, tempo ×0.8-1.25, per-segment jitter, all from one stream.
 * @param {string} recipe @param {number} n @param {number} [seed] @returns {Plan[]}
 */
export function variants(recipe, n, seed = 0) {
  const rand = mulberry32(seed);
  return Array.from({ length: n }, () => {
    const amp = uniform(rand, 0.75, 1.25);
    const tempo = uniform(rand, 0.8, 1.25);
    return toPlan(expandRecipe(recipe, { rand, amp, tempo }));
  });
}

// ---- liveliness: the procedural generator, the core's liveliness.animate ----

/** mulberry32, bit-identical to the core's prng.Mulberry32. @param {number} seed */
export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const SUBSTEPS = 8;
const NOISE_TERMS = 5;
const WARP_S = 0.05;
const WARP_BAND_HZ = /** @type {const} */ ([0.15, 0.4]);
// approach expand rise attend askew orient advance grip
const NOISE_SCALE = [0.008, 0.008, 0.008, 0.025, 0.015, 0.0, 0.0, 0.02];
const OMEGA = [14, 18, 14, 22, 16, 9, 7, 30];
const NOISE_BAND_HZ = [
  [0.5, 2.0],
  [0.5, 2.0],
  [0.4, 2.0],
  [0.7, 3.0],
  [0.5, 2.0],
  [0.2, 0.8],
  [0.2, 0.8],
  [1.0, 4.0],
];

/** @param {() => number} rand @param {readonly number[]} band */
const drawFrequency = (rand, [lo, hi]) => lo * (hi / lo) ** rand();

/** Frame positions to read the plan at: a smooth wobble, pinned at both ends. @param {number} count @param {() => number} rand */
function warp(count, rand) {
  const duration = Math.max((count - 1) / FPS, 1 / FPS);
  const wobble = new Array(count).fill(0);
  for (let term = 0; term < 2; term++) {
    const frequency = drawFrequency(rand, WARP_BAND_HZ);
    const phase = 2 * Math.PI * rand();
    for (let i = 0; i < count; i++) wobble[i] += Math.sin(2 * Math.PI * frequency * (i / FPS) + phase) / 2;
  }
  return wobble.map((w, i) => {
    const t = i / FPS;
    const warped = t + WARP_S * Math.sin((Math.PI * t) / duration) * w;
    return Math.min(count - 1, Math.max(0, warped * FPS));
  });
}

/** Critically damped tracking with target-velocity feed-forward, per channel. @param {Row[]} target */
function track(target) {
  const h = 1 / FPS / SUBSTEPS;
  const x = target[0].slice();
  const v = x.map(() => 0);
  const out = [x.slice()];
  for (let i = 1; i < target.length; i++) {
    for (let c = 0; c < x.length; c++) {
      const start = target[i - 1][c];
      const step = target[i][c] - start;
      const targetVelocity = step * FPS;
      const w = OMEGA[c];
      for (let s = 1; s <= SUBSTEPS; s++) {
        const r = start + step * (s / SUBSTEPS);
        const acceleration = w * w * (r - x[c]) + 2 * w * (targetVelocity - v[c]);
        v[c] = v[c] + acceleration * h;
        x[c] = x[c] + v[c] * h;
      }
    }
    out.push(x.slice());
  }
  return out;
}

/** @param {number[]} energy @param {() => number} rand @returns {Row[]} */
function noise(energy, rand) {
  const tempoPhase = [0];
  let acc = 0;
  for (let i = 0; i < energy.length - 1; i++) {
    acc += 1 + energy[i] / 6;
    tempoPhase.push(acc);
  }
  const phases = tempoPhase.map((p) => p / FPS);
  const out = energy.map(() => new Array(ENERGY).fill(0));
  for (let c = 0; c < ENERGY; c++) {
    for (let term = 0; term < NOISE_TERMS; term++) {
      const frequency = drawFrequency(rand, NOISE_BAND_HZ[c]);
      const phase = 2 * Math.PI * rand();
      phases.forEach((p, i) => {
        out[i][c] += Math.sin(2 * Math.PI * frequency * p + phase);
      });
    }
    const gain = Math.sqrt(2 / NOISE_TERMS) * NOISE_SCALE[c];
    out.forEach((row, i) => {
      row[c] *= gain * energy[i];
    });
  }
  return out;
}

/**
 * (T, 9) interpolated plan → (T, 8) lively motion, deterministic for `seed`:
 * a time warp, a second-order tracker that overshoots fast moves, and
 * energy-scaled band-limited detail.
 * @param {Row[]} cond @param {number} [seed] @returns {Row[]}
 */
export function liveliness(cond, seed = 0) {
  const rand = mulberry32(seed);
  const positions = warp(cond.length, rand);
  const target = positions.map((p) => {
    const i = Math.min(cond.length - 1, Math.floor(p));
    const j = Math.min(cond.length - 1, i + 1);
    const f = p - i;
    return MOTION_CHANNELS.map((_, c) => (j === i ? cond[i][c] : (cond[j][c] - cond[i][c]) * f + cond[i][c]));
  });
  const energy = cond.map((r) => Math.max(r[ENERGY], 0));
  const detail = noise(energy, rand);
  return track(target).map((row, i) => clampRow(row.map((v, c) => v + detail[i][c])).slice(0, ENERGY));
}

// ---- idle: the animator's breathing between clips (the core's Breathing.sample) ----

/**
 * The plan row MARS idles in at time `t`: a slow rise/approach bob, a gaze
 * wandering around a slight lift, a tiny grip.
 * @param {number} t
 */
export function breathing(t) {
  const tau = 2 * Math.PI;
  const row = NEUTRAL.slice();
  row[2] += 0.02 * Math.sin(tau * 0.1 * t);
  row[0] += 0.02 * Math.sin(tau * 0.1 * t - Math.PI / 2);
  row[3] += 0.05 + (0.03 * Math.sin(tau * 0.07 * t) + 0.02 * Math.sin(tau * (Math.sqrt(3) / 10) * t));
  row[7] += 0.02 * Math.sin(tau * 0.1 * t);
  return row;
}

// ---- basis: plan row → actuator pose (the core's basis.Basis) ----

export const ACTUATOR_KEYS = /** @type {const} */ ([
  "j1",
  "j2",
  "j3",
  "j4",
  "j5",
  "j6",
  "head_deg",
  "base_yaw",
  "base_x",
]);
/** @typedef {typeof ACTUATOR_KEYS[number]} ActuatorKey */
/** @typedef {Record<ActuatorKey, number> & { grip: number }} ActuatorPose */
/** @typedef {Partial<Record<ActuatorKey, number>>} ActuatorDelta */
/**
 * basis.json, shared with the core (a copy lives beside this file; the test
 * fails when the two drift).
 * @typedef {{
 *   version: number,
 *   neutral: ActuatorDelta,
 *   ready?: ActuatorDelta,
 *   unfold?: string[],
 *   couple?: { from: string, start: number, end: number, add: Record<string, number> }[],
 *   limits: Record<ActuatorKey, [number, number]>,
 *   grip_rad: number,
 *   clearance: { j2_min: number },
 *   channels: Record<string, { neg?: ActuatorDelta, pos?: ActuatorDelta }>,
 *   safe?: { grid: number[], scale: number[] },
 *   max_speed?: ActuatorDelta,
 * }} Basis
 */

/** The body channels the basis maps through endpoint deltas; grip/orient/advance map directly. */
const BODY_CHANNELS = MOTION_CHANNELS.slice(0, 5);
/** The body channels the arm carries (attend is the head); the safety table scales only these. */
const ARM_CHANNELS = new Set([0, 1, 2, 4]);
export const GRIP_OPEN_RAD = 0.8727;
const J1 = 0;
const J2 = 1;
const J6 = 5;
const BASE_YAW = 7;
const BASE_X = 8;

/**
 * j2's floor for a given j1: across the front arc the arm must duck under the
 * head (the same ramp as arm_control.cpp and the sim).
 * @param {number} j1 @param {number} fullMin @param {number} guardMin
 */
export function clearanceFloor(j1, fullMin, guardMin) {
  if (j1 < -1.35 || j1 >= 1.25) return fullMin;
  const t = j1 < -1.0 ? -(j1 + 1.0) / 0.35 : j1 < 1.0 ? 0 : (j1 - 1.0) / 0.25;
  return guardMin + t * (fullMin - guardMin);
}

/**
 * How far toward `row`'s arm channels the body can go before folding into
 * itself (0..1): multilinear interpolation of the basis's precomputed safety
 * table over the five body channels (built on the host against the MuJoCo
 * robot; the robot itself has no collision model). The core's Basis.safe_factor.
 * @param {Basis} basis @param {Row} row
 */
export function safeFactor(basis, row) {
  if (!basis.safe) return 1;
  const { grid, scale } = basis.safe;
  const n = grid.length;
  const dims = BODY_CHANNELS.length;
  const cells = [];
  const fractions = [];
  for (let d = 0; d < dims; d++) {
    const w = Math.min(grid[n - 1], Math.max(grid[0], row[d]));
    let above = 0;
    while (above < n && grid[above] <= w) above++;
    const cell = Math.min(above - 1, n - 2);
    cells.push(cell);
    fractions.push((w - grid[cell]) / (grid[cell + 1] - grid[cell]));
  }
  let factor = 0;
  for (let corner = 0; corner < 1 << dims; corner++) {
    let weight = 1;
    let index = 0;
    for (let d = 0; d < dims; d++) {
      const bit = (corner >> d) & 1;
      weight *= bit ? fractions[d] : 1 - fractions[d];
      index = index * n + cells[d] + bit;
    }
    if (weight > 0) factor += weight * scale[index];
  }
  return factor;
}

/** `row` with its arm channels (approach, expand, rise, askew) scaled by safeFactor. @param {Basis} basis @param {Row} row */
export function safeRow(basis, row) {
  const factor = safeFactor(basis, row);
  if (!(factor < 1)) return row;
  return row.map((v, j) => (ARM_CHANNELS.has(j) ? v * factor : v));
}

/** @param {Record<ActuatorKey, number>} q @returns {ActuatorPose} */
const withGrip = (q) => ({ ...q, grip: q.j6 / GRIP_OPEN_RAD });

/**
 * `row` with the basis's couple block applied (the core's Basis.couple): each
 * coupling adds depth · add, depth ramping 0 → 1 as its source channel goes from
 * start to end (read from the row as given), then every channel is clipped to range.
 * @param {Basis} basis @param {Row} row @returns {Row}
 */
export function couple(basis, row) {
  const out = row.slice();
  for (const c of basis.couple ?? []) {
    const source = /** @type {number} */ (row[CHANNELS.indexOf(/** @type {any} */ (c.from))]);
    const depth = Math.min(1, Math.max(0, (source - c.start) / (c.end - c.start)));
    if (!(depth > 0)) continue;
    for (const [channel, gain] of Object.entries(c.add)) {
      const j = CHANNELS.indexOf(/** @type {any} */ (channel));
      if (j < out.length) out[j] += depth * gain;
    }
  }
  return out.map((v, j) => {
    const [lo, hi] = LIMITS[DSL_KEYS[j]];
    return Math.min(hi, Math.max(lo, v));
  });
}

/**
 * One plan/motion row → actuator pose: coupled (a lowered gaze slumps the arm a
 * little), the arm channels pulled back to the collision-safe region, then the
 * basis offsets from NEUTRAL, the gripper is grip + the endpoints' j6,
 * orient/advance are base offsets from the anchor; then the joint limits and the
 * shoulder-clearance rule.
 * @param {Basis} basis @param {Row} input @returns {ActuatorPose}
 */
export function synthesize(basis, input) {
  const row = safeRow(basis, couple(basis, input));
  const delta = offset(basis, row);
  const q = ACTUATOR_KEYS.map((key, j) => (basis.neutral[key] ?? 0) + delta[j]);
  q[J6] = basis.grip_rad * row[7] + delta[J6];
  q[BASE_YAW] = row[5] * (Math.PI / 180);
  q[BASE_X] = row[6];
  return toPose(clampVector(basis, q));
}

/**
 * Actuator delta of the body channels from NEUTRAL (the core's Basis.offset):
 * the channels that unfold the folded arm share one unfolding toward READY,
 * u = 1 − Π(1 − w⁺), so tall + reaching is one unfolded mast leaning in rather
 * than two unfoldings summed into a knot; then Σ |w| · endpoint(sign w).
 * @param {Basis} basis @param {Row} row
 */
function offset(basis, row) {
  const ready = basis.ready ?? basis.neutral;
  const unfoldChannels = new Set(basis.unfold ?? []);
  let folded = 1;
  const delta = ACTUATOR_KEYS.map(() => 0);
  BODY_CHANNELS.forEach((channel, i) => {
    const w = Math.min(1, Math.max(-1, row[i]));
    if (unfoldChannels.has(channel)) folded *= 1 - Math.max(w, 0);
    const end = basis.channels[channel]?.[w > 0 ? "pos" : "neg"] ?? {};
    ACTUATOR_KEYS.forEach((key, j) => {
      delta[j] += Math.abs(w) * (end[key] ?? 0);
    });
  });
  const unfold = 1 - folded;
  return ACTUATOR_KEYS.map((key, j) => {
    const unfolding = key in ready ? (ready[key] ?? 0) - (basis.neutral[key] ?? 0) : 0;
    return unfold * unfolding + delta[j];
  });
}

/** Joint limits, then the shoulder-clearance rule (the core's Basis.clamp). @param {Basis} basis @param {number[]} q */
function clampVector(basis, q) {
  const out = ACTUATOR_KEYS.map((key, j) => Math.min(basis.limits[key][1], Math.max(basis.limits[key][0], q[j])));
  out[J2] = Math.max(out[J2], clearanceFloor(out[J1], basis.limits.j2[0], basis.clearance.j2_min));
  return out;
}

/** @param {number[]} q @returns {ActuatorPose} */
const toPose = (q) =>
  withGrip(/** @type {Record<ActuatorKey, number>} */ (Object.fromEntries(ACTUATOR_KEYS.map((k, j) => [k, q[j]]))));

/**
 * Step from `previous` toward `target` within the basis's max_speed over `dt`
 * s, keeping the shoulder clearance — what the robot's animator does to every
 * output pose (the core's Basis.limit): j1 may only sweep into the front arc as
 * fast as j2 can rise over the clearance floor, and the rate limit is applied
 * again after the clamp, so no step exceeds max_speed · dt.
 * @param {Basis} basis @param {ActuatorPose} previous @param {ActuatorPose} target @param {number} dt
 * @returns {ActuatorPose}
 */
export function limitSpeed(basis, previous, target, dt) {
  const step = ACTUATOR_KEYS.map((k) => (basis.max_speed?.[k] ?? Infinity) * dt);
  const prev = ACTUATOR_KEYS.map((k) => previous[k]);
  const q = ACTUATOR_KEYS.map((k, j) => {
    const moved = prev[j] + Math.min(step[j], Math.max(-step[j], target[k] - prev[j]));
    return Math.min(basis.limits[k][1], Math.max(basis.limits[k][0], moved));
  });
  /** @param {number} j1 */
  const floor = (j1) => clearanceFloor(j1, basis.limits.j2[0], basis.clearance.j2_min);
  const bound = Math.max(prev[J2] + step[J2], floor(prev[J1]));
  if (floor(q[J1]) > bound) {
    // Within one step j1 cannot cross the front arc's plateau, so the floor is monotonic here.
    let lo = prev[J1];
    let hi = q[J1];
    for (let i = 0; i < 20; i++) {
      const mid = 0.5 * (lo + hi);
      if (floor(mid) <= bound) lo = mid;
      else hi = mid;
    }
    q[J1] = lo;
  }
  return toPose(clampVector(basis, q).map((v, j) => Math.min(prev[j] + step[j], Math.max(prev[j] - step[j], v))));
}

/** Low-pass cut-off (Hz) the retimer judges speed on, so only real moves stretch time (the core's RETIME_FC). */
const RETIME_FC = 4.0;

/**
 * Poses with every move too fast for max_speed slowed down until it fits, so a
 * snap keeps its full excursion instead of being cut short (the core's Basis.retime).
 * @param {Basis} basis @param {ActuatorPose[]} poses @param {number} dt @returns {ActuatorPose[]}
 */
export function retime(basis, poses, dt) {
  if (poses.length < 2) return poses.map((p) => ({ ...p }));
  const capped = ACTUATOR_KEYS.filter((k) => Number.isFinite(basis.max_speed?.[k] ?? Infinity));
  const smooth = lowpass(
    poses.map((p) => capped.map((k) => p[k])),
    RETIME_FC,
    1 / dt,
  );
  const times = [0];
  for (let i = 1; i < poses.length; i++) {
    let stretch = 1;
    capped.forEach((k, j) => {
      const cap = /** @type {number} */ (basis.max_speed?.[k]);
      stretch = Math.max(stretch, Math.abs(smooth[i][j] - smooth[i - 1][j]) / (cap * dt));
    });
    times.push(times[i - 1] + stretch);
  }
  const end = times[times.length - 1] * dt;
  const seconds = times.map((t) => t * dt);
  const count = Math.floor(end / dt + 0.5) + 1;
  const columns = ACTUATOR_KEYS.map((k) => poses.map((p) => p[k]));
  return Array.from({ length: count }, (_, i) => toPose(columns.map((col) => interp(i * dt, seconds, col))));
}

/**
 * Actuator frames as the robot plays them: retime, then limitSpeed frame to
 * frame (the core's Basis.limit_frames).
 * @param {Basis} basis @param {ActuatorPose[]} poses @param {number} dt @returns {ActuatorPose[]}
 */
export function limitFrames(basis, poses, dt) {
  const timed = retime(basis, poses, dt);
  const out = timed.slice(0, 1);
  for (let i = 1; i < timed.length; i++) out.push(limitSpeed(basis, out[i - 1], timed[i], dt));
  return out;
}

// ---- clips (the core's motion.Clip as JSON) ----

/**
 * @typedef {{ name: string, prompt: string, idea: string, recipe: string, fps: number,
 *             space: "plan" | "actuator", channels: string[], frames: number[][] }} Clip
 */

/**
 * The whole offline path: recipe → frames → lively motion clip (the core's
 * Clip.from_recipe: procedural liveliness runs on the expanded recipe itself, so
 * fast osc survives), plus the serving plan for the plots.
 * @param {string} recipe
 * @param {{ name?: string, prompt?: string, idea?: string, seed?: number }} [meta]
 */
export function recipeToClip(recipe, { name = "clip", prompt = "", idea = "", seed = 0 } = {}) {
  const frames = expandRecipe(recipe);
  const plan = toPlan(frames);
  /** @type {Clip} */
  const clip = {
    name,
    prompt,
    idea,
    recipe,
    fps: FPS,
    space: "plan",
    channels: [...MOTION_CHANNELS],
    frames: liveliness(frames, seed),
  };
  return { clip, frames, plan };
}

/**
 * A clip's frames as actuator poses: plan rows through the basis, actuator
 * rows (recorded data) verbatim.
 * @param {Basis} basis @param {Clip} clip @returns {ActuatorPose[]}
 */
export function clipActuators(basis, clip) {
  if (clip.space === "actuator") return clip.frames.map(toPose);
  return clip.frames.map((f) => synthesize(basis, [...f, 0]));
}

/** A clip's frame rate: absent means FPS; anything but a sane positive rate is malformed. @param {unknown} raw */
function clipFps(raw) {
  if (raw === undefined) return FPS;
  const fps = Number(raw);
  if (!(fps >= 1 && fps <= 1000)) throw new Error(`clip fps must be 1..1000, got ${String(raw)}`);
  return fps;
}

/**
 * Validate a Clip from JSON (robot, server, file) the way the core's
 * Clip.from_dict does. Throws with the reason.
 * @param {any} raw @returns {Clip}
 */
export function parseClip(raw) {
  const data = typeof raw === "string" ? JSON.parse(raw) : raw;
  const space = data?.space === "actuator" ? "actuator" : "plan";
  /** @type {readonly string[]} */
  const expected = space === "plan" ? MOTION_CHANNELS : ACTUATOR_KEYS;
  const channels = Array.isArray(data?.channels) ? data.channels.map(String) : [...expected];
  if (channels.join() !== expected.join()) {
    throw new Error(`${space} clip channels must be ${expected.join(",")}, got ${channels.join(",")}`);
  }
  const frames = Array.isArray(data?.frames) ? data.frames : [];
  const finite = frames.every(
    (/** @type {any} */ f) => Array.isArray(f) && f.length === expected.length && f.every(Number.isFinite),
  );
  if (!frames.length || !finite) throw new Error("clip frames must be a non-empty grid of finite numbers");
  return {
    name: String(data.name ?? "clip"),
    prompt: String(data.prompt ?? ""),
    idea: String(data.idea ?? ""),
    recipe: String(data.recipe ?? ""),
    fps: clipFps(data.fps),
    space,
    channels,
    frames: frames.map((/** @type {number[]} */ f) => f.map(Number)),
  };
}

/** Clip JSON for the wire, frames rounded like the core's Clip.to_dict. @param {Clip} clip */
export function clipJson(clip) {
  return JSON.stringify({ ...clip, frames: clip.frames.map((f) => f.map((v) => Math.round(v * 1e4) / 1e4)) });
}
