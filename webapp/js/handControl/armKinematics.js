// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Closed-form kinematics for the MARS arm, in the browser.
//
// Camera control needs a Cartesian target turned into joints ~20x a second and
// streamed; the robot's KDL node is a topic round trip that logs every solve,
// and the SDK's move_to is rest-to-rest. But this arm is not a general 6-DOF
// chain: joint1 swivels the whole arm about a fixed shoulder, joints 2-4 are a
// planar 3R linkage in the swiveled plane, and joint5 rolls about the tool
// axis. So position + tool pitch + roll solve exactly, with no iteration and no
// solver to tune -- and the FK below is bit-identical to the URDF chain.
//
// The geometry is pinned rather than fetched: it is the same mars.urdf the IK
// node solves against, and tests/handControl.test.js re-derives every constant
// from that file, so a model change fails there instead of drifting silently.

/** @typedef {[number, number, number, number, number]} ArmJoints j1..j5 (rad) */

// joint2's axis in base_link -- joint1's origin plus joint2's, which lies on
// the swivel axis and so does not move with j1.
export const SHOULDER = [0.086, -0.05285, 0.0845];

// Planar link geometry: each link's length and the angle of its own offset,
// since neither link2->3 nor link3->4 runs straight down its parent's x axis.
const LINK_A = Math.hypot(0.02825, 0.12125);
const ANGLE_A = Math.atan2(0.12125, 0.02825);
const LINK_B = Math.hypot(0.1375, 0.0045);
const ANGLE_B = Math.atan2(0.0045, 0.1375);
// joint4 -> ee_link, all along x: joint5 rolls about it and cannot move it.
const TOOL = 0.019 + 0.091838;

/** URDF joint limits, j1..j6. @type {[number, number][]} */
export const JOINT_LIMITS = [
  [-1.5708, 1.5708],
  [-1.5708, 1.22],
  [-1.5708, 1.7453],
  [-1.9199, 1.7453],
  [-1.5708, 1.5708],
  [0, 0.8727],
];

// Gripper j6: Manipulation.GRIPPER_CLOSED / GRIPPER_OPEN, inside the URDF range.
export const GRIPPER_OPEN = 0.85;

// arm_control.cpp's "intelligent joint limits" as mars_sim_driver ports them:
// swinging joint1 across the robot's front arc forces joint2 to duck under the
// head instead of sweeping through it. The sim's -0.25 floor is the shallower
// of the two, so honoring it keeps a commanded pose achievable on either.
const JOINT2_GUARD_MIN = -0.25;
const JOINT2_GUARD_MARGIN = 0.09;

/** joint2's floor for a given joint1, matching mars_sim_driver.core.joint2_min_target.
 * @param {number} j1 */
export function joint2Floor(j1) {
  const full = JOINT_LIMITS[1][0];
  let t;
  if (j1 < -1.35 || j1 >= 1.25) t = 1;
  else if (j1 < -1.0) t = -(j1 + 1.0) / 0.35;
  else if (j1 < 1.0) t = 0;
  else t = (j1 - 1.0) / 0.25;
  return JOINT2_GUARD_MIN + t * (full - JOINT2_GUARD_MIN) + JOINT2_GUARD_MARGIN;
}

const clamp = (/** @type {number} */ v, /** @type {number} */ lo, /** @type {number} */ hi) =>
  Math.max(lo, Math.min(hi, v));

/**
 * Tool pose from joint angles -- exact, and the inverse of {@link solveArm}.
 * @param {number[]} joints at least j1..j5 (rad)
 * @returns {{ x: number, y: number, z: number, radius: number, roll: number, pitch: number, yaw: number }}
 *   radius is the tool's distance from the swivel axis; pitch is positive tool-down.
 */
export function forwardArm(joints) {
  const [j1, j2, j3, j4, j5] = joints;
  const a = ANGLE_A - j2;
  const b = ANGLE_B - j2 - j3;
  const pitch = j2 + j3 + j4;
  const radius = LINK_A * Math.cos(a) + LINK_B * Math.cos(b) + TOOL * Math.cos(pitch);
  const height = LINK_A * Math.sin(a) + LINK_B * Math.sin(b) - TOOL * Math.sin(pitch);
  return {
    x: SHOULDER[0] + radius * Math.cos(j1),
    y: SHOULDER[1] + radius * Math.sin(j1),
    z: SHOULDER[2] + height,
    radius,
    roll: j5,
    pitch,
    yaw: j1,
  };
}

/** Every joint solution putting the tool at (radius, height) from the shoulder at
 * exactly this pitch -- both elbow branches, joint limits and the joint2 guard applied.
 * @returns {ArmJoints[]}
 */
function branchesAt(/** @type {number} */ radius, /** @type {number} */ height,
  /** @type {number} */ pitch, /** @type {number} */ roll, /** @type {number} */ yaw) {
  // Walk back down the tool to the wrist, which the 2R linkage must reach.
  const x = radius - TOOL * Math.cos(pitch);
  const z = height + TOOL * Math.sin(pitch);
  const cosine = (x * x + z * z - LINK_A * LINK_A - LINK_B * LINK_B) / (2 * LINK_A * LINK_B);
  if (Math.abs(cosine) > 1) return [];
  const floor = joint2Floor(yaw);
  /** @type {ArmJoints[]} */
  const found = [];
  for (const sign of [-1, 1]) {
    const elbow = sign * Math.acos(clamp(cosine, -1, 1));
    const shoulder = Math.atan2(z, x) - Math.atan2(LINK_B * Math.sin(elbow), LINK_A + LINK_B * Math.cos(elbow));
    const j2 = ANGLE_A - shoulder;
    const j3 = ANGLE_B - ANGLE_A - elbow;
    /** @type {ArmJoints} */
    const q = [yaw, j2, j3, pitch - j2 - j3, roll];
    if (q.every((v, i) => v >= JOINT_LIMITS[i][0] && v <= JOINT_LIMITS[i][1]) && j2 >= floor) found.push(q);
  }
  return found;
}

const nearest = (/** @type {ArmJoints[]} */ options, /** @type {number[]} */ seed) =>
  options.reduce((best, q) =>
    q.reduce((s, v, i) => s + (v - seed[i]) ** 2, 0) < best.reduce((s, v, i) => s + (v - seed[i]) ** 2, 0) ? q : best,
  );

const PITCH_SCAN = 361; // 1 degree over the full turn
const RADIAL_SEARCH_M = 0.06;
const RADIAL_STEP_M = 0.0025;

/**
 * Joints putting the tool at `target` with this pitch and roll, or the closest
 * posture the arm can hold.
 *
 * Order matters and is the point of this function: the requested tilt is worth
 * more than the requested reach. A hand tilted down at the floor asks for a
 * pitch that is only reachable a few centimetres nearer or further out, so the
 * search slides radially first and only then gives up tilt -- the other way
 * round the claw levels off just as it arrives at the object.
 *
 * @param {[number, number, number]} target tool position in base_link (m)
 * @param {number} pitch requested tool pitch (rad, positive = down)
 * @param {number} roll requested tool roll (rad)
 * @param {number[]} seed previous solution; picks between elbow branches
 * @param {{ radius: [number, number] }} bounds radial reach the search may use
 * @returns {{ joints: ArmJoints, pitch: number, limited: boolean }}
 *   limited is true whenever the arm could not take the pose as asked.
 */
export function solveArm(target, pitch, roll, seed, bounds) {
  const dx = target[0] - SHOULDER[0];
  const dy = target[1] - SHOULDER[1];
  const rawYaw = Math.atan2(dy, dx);
  const yaw = clamp(rawYaw, JOINT_LIMITS[0][0], JOINT_LIMITS[0][1]);
  const height = target[2] - SHOULDER[2];
  const radius = clamp(Math.hypot(dx, dy), bounds.radius[0], bounds.radius[1]);
  let limited = yaw !== rawYaw || radius !== Math.hypot(dx, dy);

  const exact = branchesAt(radius, height, pitch, roll, yaw);
  if (exact.length) return { joints: nearest(exact, seed), pitch, limited };

  for (let shift = RADIAL_STEP_M; shift <= RADIAL_SEARCH_M + 1e-9; shift += RADIAL_STEP_M) {
    for (const sign of [1, -1]) {
      const moved = radius + shift * sign;
      if (moved < bounds.radius[0] || moved > bounds.radius[1]) continue;
      const options = branchesAt(moved, height, pitch, roll, yaw);
      if (options.length) return { joints: nearest(options, seed), pitch, limited: true };
    }
  }

  // No reach holds this tilt. Fall back to the reachable pitch nearest the
  // request, staying in the posture group the arm is already in: joint limits
  // can split the reachable pitches into disconnected intervals, and crossing
  // one flips the elbow branch mid-motion.
  /** @type {{ q: ArmJoints, pitch: number }[]} */
  const scan = [];
  for (let i = 0; i < PITCH_SCAN; i++) {
    const candidate = -Math.PI + (2 * Math.PI * i) / (PITCH_SCAN - 1);
    for (const q of branchesAt(radius, height, candidate, roll, yaw)) scan.push({ q, pitch: candidate });
  }
  if (!scan.length) {
    /** @type {ArmJoints} */
    const held = [seed[0], seed[1], seed[2], seed[3], roll];
    return { joints: held, pitch: seed[1] + seed[2] + seed[3], limited: true };
  }
  const best = scan.reduce((a, b) => (Math.abs(b.pitch - pitch) < Math.abs(a.pitch - pitch) ? b : a));
  return { joints: best.q, pitch: best.pitch, limited: true };
}
