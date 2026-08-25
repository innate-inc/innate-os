// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Expression model — the studio's representation of a pose, in two layers.
//
// Actuator layer: everything MARS can hold still — the 6 arm joints, the head
// pitch servo, and where the base sits relative to its neutral spot (the robot
// can drive anywhere, so base offset is a pose variable like any joint).
//
// Expression layer: a small basis of semantic axes. Each axis is a pair of
// actuator-space deltas (the −1 and +1 extremes) chosen from what the axis
// physically *means* — approach is literally distance to the person, expand is
// literally silhouette area — not from how an emotion "should look". Emotions
// are then just weight vectors over the basis (PRESETS), which is what makes
// the vocabulary searchable by sliders, optimizers, and VLM judges alike.
//
// synthesize() is the whole runtime: neutral + Σ weight·delta + manual
// offsets, clamped to actuator limits. Pure module — no DOM, no ROS.

/**
 * @typedef {"j1"|"j2"|"j3"|"j4"|"j5"|"j6"|"head"|"baseX"|"baseY"|"baseYaw"} ActuatorKey
 * @typedef {Record<ActuatorKey, number>} Pose
 * @typedef {Partial<Record<ActuatorKey, number>>} PoseDelta
 * @typedef {{ key: string, label: string, negLabel: string, posLabel: string,
 *             group: "shape" | "stance", doc: string, neg: PoseDelta, pos: PoseDelta }} Axis
 * @typedef {Record<string, number>} Weights axis key -> weight in [-1, 1]
 * @typedef {{ key: string, label: string, doc: string, weights: Weights }} Preset
 */

// Arm ranges are the URDF joint limits (mars.urdf — what the arm can actually
// hold); head is the servo's real range in degrees (HEAD_MIN/MAX_DEG, wider
// than the URDF's stale ±20°); base offsets are studio bounds for "slightly"
// — the runtime controller will own real driving.
/** @type {{ key: ActuatorKey, label: string, min: number, max: number, unit: string, step: number }[]} */
export const ACTUATORS = [
  { key: "j1", label: "j1 · base yaw", min: -1.5708, max: 1.5708, unit: "rad", step: 0.01 },
  { key: "j2", label: "j2 · shoulder", min: -1.5708, max: 1.22, unit: "rad", step: 0.01 },
  { key: "j3", label: "j3 · elbow", min: -1.5708, max: 1.7453, unit: "rad", step: 0.01 },
  { key: "j4", label: "j4 · wrist pitch", min: -1.9199, max: 1.7453, unit: "rad", step: 0.01 },
  { key: "j5", label: "j5 · wrist roll", min: -1.5708, max: 1.5708, unit: "rad", step: 0.01 },
  { key: "j6", label: "j6 · gripper", min: 0, max: 0.8727, unit: "rad", step: 0.01 },
  { key: "head", label: "head pitch", min: -40, max: 70, unit: "°", step: 1 },
  { key: "baseX", label: "base advance", min: -0.3, max: 0.3, unit: "m", step: 0.005 },
  { key: "baseY", label: "base sidestep", min: -0.3, max: 0.3, unit: "m", step: 0.005 },
  { key: "baseYaw", label: "base turn", min: -1.6, max: 1.6, unit: "rad", step: 0.01 },
];

// At-ease: arm loosely folded near the SDK rest pose but off the joint limits
// (so every axis has room in both directions), head level on the person, and
// the base standing slightly OBLIQUE — a relaxed body doesn't square up, so
// squaring on becomes an engagement signal the orient axis can spend.
/** @type {Pose} */
export const NEUTRAL = {
  j1: 0.9,
  j2: -0.85,
  j3: 1.35,
  j4: -0.4,
  j5: 0,
  j6: 0.12,
  head: 8,
  baseX: 0,
  baseY: 0,
  baseYaw: 0.26,
};

// The person the robot is expressing *to* stands here (base frame, meters) —
// shared by the renderer's observer figure and the judge's viewpoints, so
// approach/turn/attend always read against the same target.
export const PERSON = { x: 1.0, y: 0, height: 1.65 };

// The built-in basis — a starting point, not a commitment. The studio edits a
// live copy (defaultBasis/sanitizeBasis below): endpoints are re-captured from
// sculpted poses, axes added or dropped, and the result persists and exports
// with the pose library. Deltas are what the +1 / −1 extreme adds to NEUTRAL;
// anything an axis doesn't mention stays untouched, so axes compose additively.
// Shape axes describe the BODY's configuration (arm + head — Laban's Shape);
// stance axes describe where the base stands relative to the person, which
// the runtime driving layer will own at execution time. This is the
// conversation's core vocabulary — orient, approach, expand, rise, tilt,
// asymmetry — translated to MARS's morphology: "lean" is the arm+head mass
// shifting (the chassis can't tilt), and "head tilt / asymmetry" becomes the
// askew axis (the head can only pitch, so the wrist roll + arm cant carry
// the cocked-head quality).
/** @type {Axis[]} */
export const AXES = [
  {
    key: "approach",
    label: "approach",
    negLabel: "retract",
    posLabel: "lean in",
    group: "shape",
    doc: "Body lean without moving the feet: the arm-and-head mass reaches toward the person (effector presented, gaze committed) or pulls back against the chassis, protecting the protruding parts. Distance itself is the advance axis.",
    neg: { j2: -0.4, j3: 0.35, j4: -0.9, j6: -0.12, head: -4 },
    pos: { j1: -0.9, j2: 1.0, j3: -1.0, j4: -0.3, j6: 0.3, head: 4 },
  },
  {
    key: "expand",
    label: "expand",
    negLabel: "contract",
    posLabel: "expand",
    group: "shape",
    doc: "Silhouette width. Expanding swings the arm out of the body outline and opens the gripper; contracting folds everything inside the chassis footprint and shuts the claw.",
    neg: { j1: 0.5, j3: 0.3, j4: -0.5, j6: -0.12 },
    pos: { j1: -1.5, j3: -0.85, j4: 0.4, j6: 0.6 },
  },
  {
    key: "rise",
    label: "rise",
    negLabel: "low",
    posLabel: "tall",
    group: "shape",
    doc: "Silhouette height. The arm is the only mass MARS can raise: mast-high maximizes apparent size, folded low minimizes it. Gaze height is attend's job, so the two can disagree (tall but downcast, small but watching).",
    neg: { j2: -0.5, j3: 0.35, j4: -0.8 },
    pos: { j1: -0.7, j2: 0.75, j3: -2.75, j4: 0.3 },
  },
  {
    key: "attend",
    label: "attend",
    negLabel: "downcast",
    posLabel: "raised",
    group: "shape",
    doc: "Gaze height. The person's face is ~1.5 m above the robot's, so sensors-on-target means head pitched well up; head at the floor abandons the target entirely.",
    neg: { head: -45 },
    pos: { head: 55 },
  },
  {
    key: "askew",
    label: "askew",
    negLabel: "canted in",
    posLabel: "canted out",
    group: "shape",
    doc: "Asymmetry — the cocked-head quality. MARS's head can't roll, so the wrist roll and the arm canting across (in) or wide of (out) the body carry it; 0 is the composed, regular posture.",
    neg: { j1: 0.55, j4: -0.4, j5: -1.3 },
    pos: { j1: -0.5, j3: -0.3, j5: 1.3 },
  },
  {
    key: "advance",
    label: "advance",
    negLabel: "withdraw",
    posLabel: "advance",
    group: "stance",
    doc: "Distance to the person — the most primitive engagement signal. Advancing commits the whole body toward the stimulus; withdrawing moves it away.",
    neg: { baseX: -0.24 },
    pos: { baseX: 0.17 },
  },
  {
    key: "orient",
    label: "orient",
    negLabel: "turned away",
    posLabel: "squared on",
    group: "stance",
    doc: "Facing, away ↔ toward. Neutral stands casually oblique; +1 squares the body onto the person (full engagement), −1 turns it ~85° away. Turned away with the head still on target (attend up) reads guarded, not disengaged.",
    neg: { baseYaw: 1.2 },
    pos: { baseYaw: -0.26 },
  },
  {
    key: "sidestep",
    label: "sidestep",
    negLabel: "right",
    posLabel: "left",
    group: "stance",
    doc: "Lateral offset from the person's axis — circling, peeking, giving way.",
    neg: { baseY: -0.2 },
    pos: { baseY: 0.2 },
  },
];

// Emotions as weight vectors — derived from what the state *does* to a body,
// not from acting them out. The interesting ones are combinations single axes
// can't say: fear retreats and shrinks but keeps the sensors on the threat.
/** @type {Preset[]} */
export const PRESETS = [
  {
    key: "curious",
    label: "curious",
    doc: "Get the sensors closer without committing the body: a small advance, a lean in, the cocked-head cant, gaze up on the person.",
    weights: { approach: 0.5, askew: 0.4, advance: 0.4, orient: 0.3, rise: 0.1, attend: 0.7 },
  },
  {
    key: "excited",
    label: "excited",
    doc: "Maximum energy: squared on, big, tall, open, pressing toward the person.",
    weights: { advance: 0.5, orient: 1, rise: 0.7, expand: 0.7, attend: 0.8 },
  },
  {
    key: "confident",
    label: "confident",
    doc: "Maximize apparent size, square on, hold the ground: tall, open, composed (no cant), no retreat.",
    weights: { advance: 0.2, orient: 1, rise: 0.9, expand: 0.45, attend: 0.55 },
  },
  {
    key: "affectionate",
    label: "affectionate",
    doc: "Close the distance and lean in, effector offered — contact-seeking, softly raised gaze.",
    weights: { advance: 0.65, orient: 0.6, approach: 0.85, expand: 0.15, attend: 0.5 },
  },
  {
    key: "fearful",
    label: "fearful",
    doc: "Retreat, shrink, pull the body in — but the head stays on the threat. Monitoring while withdrawing is what separates fear from mere disengagement.",
    weights: { advance: -0.9, orient: -0.25, approach: -0.7, rise: -0.7, expand: -0.6, attend: 0.35 },
  },
  {
    key: "sad",
    label: "sad",
    doc: "Low energy, no target focus: slumped small, gaze at the floor, slight drift away and off-axis.",
    weights: { advance: -0.3, orient: -0.2, approach: -0.3, rise: -0.6, expand: -0.3, attend: -0.9 },
  },
  {
    key: "suspicious",
    label: "suspicious",
    doc: "Body turned off-axis and edged away while the head keeps watching, a wary cant — guarded observation.",
    weights: { advance: -0.25, orient: -0.55, sidestep: -0.3, expand: -0.2, askew: 0.3, attend: 0.4 },
  },
  {
    key: "relaxed",
    label: "relaxed",
    doc: "At ease: the casually oblique neutral stance, soft gaze on the person, nothing braced.",
    weights: { attend: 0.2 },
  },
];

/** Zero weight for every axis. @param {Axis[]} [axes] @returns {Weights} */
export function zeroWeights(axes = AXES) {
  return Object.fromEntries(axes.map((a) => [a.key, 0]));
}

/** Deep copy of the built-in basis — the starting point for editing. @returns {Axis[]} */
export function defaultBasis() {
  return AXES.map((a) => ({ ...a, neg: { ...a.neg }, pos: { ...a.pos } }));
}

/**
 * Validate a stored/imported basis back into Axis[]. Keys must be unique and
 * non-empty; missing labels/docs fall back to the built-in axis of the same
 * key (older exports carried only key+deltas). Throws on anything else.
 * @param {any} raw @returns {Axis[]}
 */
export function sanitizeBasis(raw) {
  if (!Array.isArray(raw) || raw.length === 0) throw new Error("not an axis basis");
  const seen = new Set();
  return raw.map((a) => {
    const key = String(a?.key ?? "").trim();
    if (!key || seen.has(key)) throw new Error("axis keys must be unique and non-empty");
    seen.add(key);
    const builtin = AXES.find((d) => d.key === key);
    return {
      key,
      label: String(a.label || builtin?.label || key),
      negLabel: String(a.negLabel || builtin?.negLabel || "−"),
      posLabel: String(a.posLabel || builtin?.posLabel || "+"),
      group: a.group === "stance" || a.group === "shape" ? a.group : (builtin?.group ?? "shape"),
      doc: String(a.doc || builtin?.doc || "custom axis"),
      neg: sanitizeOffsets(a.neg),
      pos: sanitizeOffsets(a.pos),
    };
  });
}

/** @param {number} v @param {number} lo @param {number} hi */
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Clamp every actuator to its physical range. @param {Pose} pose @returns {Pose} */
export function clampPose(pose) {
  const out = /** @type {Pose} */ ({ ...pose });
  for (const a of ACTUATORS) out[a.key] = clamp(out[a.key], a.min, a.max);
  return out;
}

/**
 * Fold the expression layers into one actuator pose:
 * NEUTRAL + Σ axis contribution (weight toward the pos/neg extreme) + manual
 * per-actuator offsets, clamped to limits.
 * @param {Weights} weights @param {PoseDelta} [offsets] @param {Axis[]} [axes] @returns {Pose}
 */
export function synthesize(weights, offsets = {}, axes = AXES) {
  const q = /** @type {Pose} */ ({ ...NEUTRAL });
  for (const axis of axes) {
    const w = clamp(weights[axis.key] ?? 0, -1, 1);
    if (w === 0) continue;
    const delta = w > 0 ? axis.pos : axis.neg;
    for (const [key, value] of Object.entries(delta)) {
      q[/** @type {ActuatorKey} */ (key)] += Math.abs(w) * /** @type {number} */ (value);
    }
  }
  for (const [key, value] of Object.entries(offsets)) {
    q[/** @type {ActuatorKey} */ (key)] += /** @type {number} */ (value) || 0;
  }
  return clampPose(q);
}

/** @typedef {{ id: string, name: string, weights: Weights, offsets: PoseDelta,
 *              notes: string, thumb: string | null, actuators?: PoseDelta | null,
 *              judge: { target: string, p: number, stamp: number } | null }} SavedPose */

/**
 * The library's export payload — the live basis, plus weights AND the exact
 * actuator vector per pose, so downstream steps (PCA over collected poses,
 * the runtime controller) can consume either layer without importing this
 * module — and so a pose survives later edits to the basis it was authored on.
 * @param {SavedPose[]} poses @param {Axis[]} [axes]
 */
export function exportPayload(poses, axes = AXES) {
  return {
    format: "innate-expression-poses",
    version: 1,
    robot: "mars",
    neutral: NEUTRAL,
    axes,
    poses: poses.map((p) => ({
      name: p.name,
      notes: p.notes,
      weights: p.weights,
      offsets: p.offsets,
      actuators: actuatorNumbers(p.actuators) ?? synthesize(sanitizeWeights(p.weights, axes), p.offsets, axes),
      judge: p.judge,
    })),
  };
}

/** Finite actuator values from a raw object (zeros kept — 0 is a real value
 * here, unlike in a delta), or null when nothing usable. @param {any} raw @returns {PoseDelta | null} */
export function actuatorNumbers(raw) {
  if (!raw || typeof raw !== "object") return null;
  /** @type {PoseDelta} */
  const out = {};
  for (const a of ACTUATORS) {
    const v = Number(raw[a.key]);
    if (Number.isFinite(v)) out[a.key] = v;
  }
  return Object.keys(out).length ? out : null;
}

/**
 * Parse an exported payload back into library entries plus the basis the file
 * was authored on (null when absent/invalid — older exports carried only
 * key+deltas, which sanitizeBasis relabels from the built-ins). Thumbnails
 * are not exported; they re-render on load. Throws on anything not ours.
 * @param {string} text
 * @returns {{ basis: Axis[] | null,
 *             poses: { name: string, weights: Weights, offsets: PoseDelta, notes: string, actuators: PoseDelta | null }[] }}
 */
export function parseImport(text) {
  const data = JSON.parse(text);
  if (data?.format !== "innate-expression-poses" || !Array.isArray(data.poses)) {
    throw new Error("not an expression-poses export");
  }
  /** @type {Axis[] | null} */
  let basis = null;
  try {
    basis = sanitizeBasis(data.axes);
  } catch {
    basis = null;
  }
  return {
    basis,
    poses: data.poses.map((/** @type {any} */ p) => ({
      name: String(p.name || "pose"),
      weights: sanitizeWeights(p.weights, basis ?? AXES),
      offsets: sanitizeOffsets(p.offsets),
      notes: String(p.notes || ""),
      actuators: actuatorNumbers(p.actuators),
    })),
  };
}

/** Keep only known axis keys, as finite numbers in [-1, 1]. @param {any} raw @param {Axis[]} [axes] @returns {Weights} */
export function sanitizeWeights(raw, axes = AXES) {
  const w = zeroWeights(axes);
  if (raw && typeof raw === "object") {
    for (const axis of axes) {
      const v = Number(raw[axis.key]);
      if (Number.isFinite(v)) w[axis.key] = clamp(v, -1, 1);
    }
  }
  return w;
}

/** Keep only known actuator keys with finite values. @param {any} raw @returns {PoseDelta} */
export function sanitizeOffsets(raw) {
  /** @type {PoseDelta} */
  const out = {};
  if (raw && typeof raw === "object") {
    for (const a of ACTUATORS) {
      const v = Number(raw[a.key]);
      if (Number.isFinite(v) && v !== 0) out[a.key] = v;
    }
  }
  return out;
}
