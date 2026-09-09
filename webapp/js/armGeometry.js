// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Where the arm is in space, and whether that intersects the robot's own body.
//
// A port of mars_arm's arm_types.hpp, deliberately duplicated rather than asked
// for over rosbridge. The robot's answer arrives a round trip late, and a round
// trip is exactly long enough for a fast move to be over before the operator
// feels anything — which is how the arm still reached the frame when swung
// quickly. Computing it here puts the wall in the operator's hand in the same
// frame the leader is already reading at 60 Hz.
//
// The robot keeps its own copy and remains the authority: this one exists to be
// FELT, not to be trusted. If the two ever disagree the robot still refuses the
// pose. Both are transcribed from mars_sim/urdf/mars.urdf and must change
// together.

import { BODY_MARGIN_M } from "./constants.js";
import { tickToRad } from "./leaderLimits.js";

// Link offsets, the joint origins in mars.urdf.
const L2_X = 0.02825, L2_Z = 0.12125; // joint2 -> joint3
const L3_X = 0.1375, L3_Z = 0.0045; // joint3 -> joint4
const L45_X = 0.110838; // joint4 -> tool
const WRIST_FROM_ELBOW = 0.063; // joint4 -> joint6

// The joint_2 axis in base_link: joint1's origin plus joint2's.
const SHOULDER_X = 0.086, SHOULDER_Y = -0.05285, SHOULDER_Z = 0.0845;

/**
 * Boxes covering the body, in base_link metres. The arm mount is deliberately
 * absent — the arm is bolted to it and would always read as touching.
 * `pad` is the clearance demanded around that box. Per-box because one global
 * figure cannot work: the shoulder sits 51 mm from the chassis and never moves
 * further away, so a global pad near that blocks the arm at rest. The chassis
 * keeps a small pad; the turret and neck above it — what joint_2 folds back
 * into — get much more. Must match self_collision.box_pads in arm_config.yaml.
 * @type {{ minX: number, minY: number, minZ: number, maxX: number, maxY: number, maxZ: number, pad: number }[]}
 */
export const BODY_BOXES = [
  { minX: -0.1526, minY: -0.091, minZ: 0.0, maxX: 0.0352, maxY: 0.091, maxZ: 0.1676, pad: 0.015 }, // chassis
  { minX: -0.229, minY: -0.0829, minZ: 0.0169, maxX: -0.1501, maxY: 0.0829, maxZ: 0.0763, pad: 0.015 }, // rear tray
  { minX: -0.1155, minY: -0.052, minZ: 0.1676, maxX: 0.0101, maxY: 0.052, maxZ: 0.198, pad: 0.055 }, // lidar turret
  { minX: -0.0612, minY: -0.0427, minZ: 0.196, maxX: -0.0102, maxY: 0.0427, maxZ: 0.235, pad: 0.055 }, // neck lower
  { minX: -0.0653, minY: -0.018, minZ: 0.235, maxX: -0.0279, maxY: 0.018, maxZ: 0.2716, pad: 0.055 }, // neck upper
];

/**
 * The arm's joints in the shoulder's plane: `x` along the arm's bearing, `z`
 * vertical. Five points, shoulder first, so the LINKS between them can be
 * tested — the joints alone all sit past 0.2 m and leave the upper arm bare.
 * @param {number} q2 @param {number} q3 @param {number} q4
 * @returns {{ x: number[], z: number[] }}
 */
export function armPlanarPoints(q2, q3, q4) {
  const a23 = q2 + q3;
  const a234 = a23 + q4;
  const c2 = Math.cos(q2), s2 = Math.sin(q2);
  const c23 = Math.cos(a23), s23 = Math.sin(a23);
  const c234 = Math.cos(a234), s234 = Math.sin(a234);
  const x1 = L2_X * c2 + L2_Z * s2;
  const z1 = -L2_X * s2 + L2_Z * c2;
  const x2 = x1 + L3_X * c23 + L3_Z * s23;
  const z2 = z1 - L3_X * s23 + L3_Z * c23;
  return {
    x: [0, x1, x2, x2 + WRIST_FROM_ELBOW * c234, x2 + L45_X * c234],
    z: [0, z1, z2, z2 - WRIST_FROM_ELBOW * s234, z2 - L45_X * s234],
  };
}

/**
 * The arm's joints in base_link. joint_1 rotates the sagittal plane about the
 * shoulder.
 * @param {number[]} rads Six joint angles in the command frame.
 * @returns {number[][]} One [x, y, z] per point.
 */
export function armPoints(rads) {
  const p = armPlanarPoints(rads[1], rads[2], rads[3]);
  const c = Math.cos(rads[0]), s = Math.sin(rads[0]);
  return p.x.map((px, i) => [SHOULDER_X + px * c, SHOULDER_Y + px * s, SHOULDER_Z + p.z[i]]);
}

/** @param {number[]} p @param {typeof BODY_BOXES[0]} b @returns {number} Distance, 0 inside. */
export function pointBoxDistance(p, b) {
  const dx = Math.max(b.minX - p[0], 0, p[0] - b.maxX);
  const dy = Math.max(b.minY - p[1], 0, p[1] - b.maxY);
  const dz = Math.max(b.minZ - p[2], 0, p[2] - b.maxZ);
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

/**
 * Segment against a box by the slab method — exact for a zero-thickness
 * segment; the margin stands in for link radius.
 * @param {number[]} a @param {number[]} b @param {typeof BODY_BOXES[0]} box @param {number} m
 * @returns {boolean}
 */
export function segmentHitsBox(a, b, box, m) {
  const lo = [box.minX - m, box.minY - m, box.minZ - m];
  const hi = [box.maxX + m, box.maxY + m, box.maxZ + m];
  let t0 = 0, t1 = 1;
  for (let i = 0; i < 3; i++) {
    const d = b[i] - a[i];
    if (Math.abs(d) < 1e-12) {
      if (a[i] < lo[i] || a[i] > hi[i]) return false;
      continue;
    }
    let tn = (lo[i] - a[i]) / d;
    let tf = (hi[i] - a[i]) / d;
    if (tn > tf) [tn, tf] = [tf, tn];
    t0 = Math.max(t0, tn);
    t1 = Math.min(t1, tf);
    if (t0 > t1) return false;
  }
  return true;
}

/**
 * Whether the pose puts any part of any link inside the body.
 * @param {number[]} rads @param {number} margin
 * @returns {boolean}
 */
export function poseHitsBody(rads, margin) {
  const pts = armPoints(rads);
  for (let i = 0; i + 1 < pts.length; i++) {
    for (const box of BODY_BOXES) {
      if (segmentHitsBox(pts[i], pts[i + 1], box, Math.max(margin, box.pad))) return true;
    }
  }
  return false;
}

/**
 * Metres of room left before the arm touches the body. Sampled along each link,
 * then reduced by half the sample spacing so the estimate is conservative by
 * construction — a raw sampled minimum over-reads by up to that much, which on
 * these link lengths is worth as much as the whole margin.
 * @param {number[]} rads
 * @returns {number}
 */
export function bodyClearance(rads) {
  const pts = armPoints(rads);
  const samples = 8;
  let best = Infinity;
  for (let i = 0; i + 1 < pts.length; i++) {
    const a = pts[i], b = pts[i + 1];
    const d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    let near = Infinity;
    for (let k = 0; k <= samples; k++) {
      const t = k / samples;
      const q = [a[0] + t * d[0], a[1] + t * d[1], a[2] + t * d[2]];
      // Distance less that box's extra pad, so a box demanding more room reads
      // as closer and one clearance number still drives the taper.
      for (const box of BODY_BOXES) {
        near = Math.min(near, pointBoxDistance(q, box) - Math.max(0, box.pad - BODY_MARGIN_M));
      }
    }
    const halfSpacing = (0.5 * Math.hypot(d[0], d[1], d[2])) / samples;
    best = Math.min(best, Math.max(0, near - halfSpacing));
  }
  return best;
}

/** @param {number[]} ticks @returns {number[]} The same pose in command radians. */
export function ticksToRads(ticks) {
  return ticks.map((t) => tickToRad(t));
}
