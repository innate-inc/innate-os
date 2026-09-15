// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The camera-control maths, headlessly — zero dependencies, plain node:
//   node tests/handControl.test.js
//
// armKinematics.js pins the MARS chain as constants instead of parsing the URDF
// at runtime, so the first test here rebuilds the whole chain straight out of
// mars.urdf and checks the module against it: a model change fails here rather
// than quietly aiming the claw a centimetre off on every robot.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { forwardArm, solveArm, joint2Floor, JOINT_LIMITS, SHOULDER } from "../js/handControl/armKinematics.js";
import { relativeAngles, WristMapper, WRIST_LIMITS } from "../js/handControl/orientation.js";
import { measureHand, HandMapper } from "../js/handControl/handSample.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const URDF = resolve(ROOT, "../ros2_ws/src/mars_bot/mars_description/urdf/mars.urdf");
const BOUNDS = { radius: [0.14, 0.32] };

// ---- the URDF is the source of truth -------------------------------------

/** Every revolute/fixed joint in mars.urdf as { origin, axis, limit }. */
function urdfJoints() {
  const xml = readFileSync(URDF, "utf8");
  const joints = {};
  for (const block of xml.split("<joint ").slice(1)) {
    const name = block.match(/name="([^"]+)"/)?.[1];
    const xyz = block.match(/<origin[^>]*xyz="([^"]+)"/)?.[1] ?? "0 0 0";
    const rpy = block.match(/<origin[^>]*rpy="([^"]+)"/)?.[1] ?? "0 0 0";
    const axis = block.match(/<axis[^>]*xyz="([^"]+)"/)?.[1] ?? null;
    const limit = block.match(/<limit[^>]*lower="([^"]+)"[^>]*upper="([^"]+)"/);
    joints[name] = {
      xyz: xyz.trim().split(/\s+/).map(Number),
      rpy: rpy.trim().split(/\s+/).map(Number),
      axis: axis ? axis.trim().split(/\s+/).map(Number) : null,
      limit: limit ? [Number(limit[1]), Number(limit[2])] : null,
    };
  }
  return joints;
}

const mul = (a, b) => a.map((row, i) => b[0].map((_, j) => row.reduce((s, v, k) => s + v * b[k][j], 0)));
const apply = (m, v) => m.map((row) => row.reduce((s, x, i) => s + x * v[i], 0));
const add = (a, b) => a.map((v, i) => v + b[i]);
const rot = (axis, a) => {
  const c = Math.cos(a);
  const s = Math.sin(a);
  if (axis[2]) return [[c, -s, 0], [s, c, 0], [0, 0, 1]];
  if (axis[1]) return [[c, 0, s], [0, 1, 0], [-s, 0, c]];
  return [[1, 0, 0], [0, c, -s], [0, s, c]];
};

/** Tool pose walked down the URDF chain itself, independent of armKinematics. */
function urdfForward(joints, q) {
  let p = [0, 0, 0];
  let r = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (const [i, name] of ["joint1", "joint2", "joint3", "joint4", "joint5"].entries()) {
    const j = joints[name];
    assert.deepEqual(j.rpy, [0, 0, 0], `${name} carries a fixed rotation this FK ignores`);
    p = add(p, apply(r, j.xyz));
    r = mul(r, rot(j.axis, q[i]));
  }
  p = add(p, apply(r, joints.ee_joint.xyz));
  return {
    p,
    roll: Math.atan2(r[2][1], r[2][2]),
    pitch: Math.asin(-r[2][0]),
    yaw: Math.atan2(r[1][0], r[0][0]),
  };
}

{
  const joints = urdfJoints();
  const shoulder = add(joints.joint1.xyz, joints.joint2.xyz);
  assert.ok(
    SHOULDER.every((v, i) => Math.abs(v - shoulder[i]) < 1e-9),
    `SHOULDER ${SHOULDER} drifted from mars.urdf ${shoulder}`,
  );
  for (const [i, name] of ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"].entries()) {
    assert.deepEqual(
      JOINT_LIMITS[i].map((v) => Math.round(v * 1e4) / 1e4),
      joints[name].limit.map((v) => Math.round(v * 1e4) / 1e4),
      `${name} limits drifted from mars.urdf`,
    );
  }
  for (const q of [
    [0, 0, 0, 0, 0],
    [0.3, -0.15, 0.6, 0.2, 0.5],
    [-0.8, 0.5, -0.3, 1.0, -1.1],
    [1.2, 0.2, 0.9, -0.7, 0.3],
    [-1.4, 0.9, 1.4, -1.2, 1.5],
  ]) {
    const truth = urdfForward(joints, q);
    const pose = forwardArm(q);
    assert.ok(Math.hypot(pose.x - truth.p[0], pose.y - truth.p[1], pose.z - truth.p[2]) < 1e-9, `position at ${q}`);
    assert.ok(Math.abs(pose.roll - truth.roll) < 1e-9, `roll at ${q}`);
    assert.ok(Math.abs(pose.pitch - truth.pitch) < 1e-9, `pitch at ${q}`);
    assert.ok(Math.abs(pose.yaw - truth.yaw) < 1e-9, `yaw at ${q}`);
  }
}

// ---- the solver is the exact inverse where the arm can hold the pose ------

{
  let checked = 0;
  for (const yaw of [-0.9, -0.3, 0, 0.4, 1.0]) {
    for (const radius of [0.18, 0.24, 0.29]) {
      for (const z of [0.02, 0.1, 0.18, 0.24]) {
        for (const pitch of [-0.4, 0, 0.5, 1.2]) {
          const target = [SHOULDER[0] + radius * Math.cos(yaw), SHOULDER[1] + radius * Math.sin(yaw), z];
          const solved = solveArm(target, pitch, 0.3, [0, 0.4, -0.3, 0.5, 0], BOUNDS);
          if (solved.limited) continue;
          const back = forwardArm(solved.joints);
          assert.ok(Math.hypot(back.x - target[0], back.y - target[1], back.z - target[2]) < 1e-9, "solved position");
          assert.ok(Math.abs(back.pitch - pitch) < 1e-9, "solved pitch");
          assert.ok(Math.abs(back.roll - 0.3) < 1e-9, "solved roll");
          checked++;
        }
      }
    }
  }
  assert.ok(checked > 60, `only ${checked} reachable poses in the sweep — the workspace collapsed`);
}

// Yaw is a base swivel, and it goes both ways by the same amount.
{
  const seed = [0, 0.4, -0.3, 0.5, 0];
  for (const yaw of [-0.8, -0.4, 0.4, 0.8]) {
    const target = [SHOULDER[0] + 0.25 * Math.cos(yaw), SHOULDER[1] + 0.25 * Math.sin(yaw), 0.12];
    const solved = solveArm(target, 0.2, 0, seed, BOUNDS);
    assert.ok(Math.abs(solved.joints[0] - yaw) < 1e-9, `j1 must follow yaw ${yaw}`);
    assert.ok(!solved.limited, `yaw ${yaw} must be reachable`);
  }
}

// A steep downward tilt the arm cannot hold at that exact reach still comes out
// tilted: the solver slides radially rather than levelling the claw off.
{
  const seed = [0, 0.4, -0.3, 0.5, 0];
  const target = [SHOULDER[0] + 0.31, SHOULDER[1], 0.0];
  const solved = solveArm(target, 1.3, 0, seed, BOUNDS);
  const back = forwardArm(solved.joints);
  assert.ok(Math.abs(back.pitch - 1.3) < 1e-6, `tilt lost: pitch ${back.pitch}`);
  assert.ok(Math.abs(back.radius - 0.31) <= 0.06 + 1e-9, "radial slide stayed within its 60 mm budget");
}

// Nothing the solver returns may ask for a joint the arm does not have.
{
  const seed = [0, 0.4, -0.3, 0.5, 0];
  for (const z of [-0.2, 0, 0.14, 0.45]) {
    for (const radius of [0.05, 0.2, 0.5]) {
      for (const pitch of [-1.5, 0, 1.5]) {
        const solved = solveArm([SHOULDER[0] + radius, SHOULDER[1], z], pitch, 1.4, seed, BOUNDS);
        solved.joints.forEach((v, i) => {
          assert.ok(Number.isFinite(v), `joint ${i} not finite`);
          assert.ok(v >= JOINT_LIMITS[i][0] - 1e-9 && v <= JOINT_LIMITS[i][1] + 1e-9, `joint ${i} out of range: ${v}`);
        });
        assert.ok(solved.joints[1] >= joint2Floor(solved.joints[0]) - 1e-9, "joint2 broke the front-arc guard");
      }
    }
  }
}

// The guard itself, against mars_sim_driver's own ramp (sim/sandbox/test_driver_core.py).
{
  const margin = 0.09;
  const full = JOINT_LIMITS[1][0];
  const guard = -0.25;
  assert.ok(Math.abs(joint2Floor(-1.4) - (full + margin)) < 1e-9, "outside the arc: full range");
  assert.ok(Math.abs(joint2Floor(0) - (guard + margin)) < 1e-9, "front arc: duck");
  assert.ok(Math.abs(joint2Floor(1.125) - (guard + 0.5 * (full - guard) + margin)) < 1e-9, "mid-ramp");
  assert.ok(Math.abs(joint2Floor(1.25) - (full + margin)) < 1e-9, "past the arc: full range");
}

// ---- hand -> claw ---------------------------------------------------------

const IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1];

{
  assert.ok(relativeAngles(IDENTITY, IDENTITY).every((v) => Math.abs(v) < 1e-12), "no turn, no angles");
}

// Turning the hand commands yaw, and the two directions are mirror images —
// the regression behind "yaw is disabled" in the studio's personal mapper.
{
  const about = (angle) => [Math.cos(angle), -Math.sin(angle), 0, Math.sin(angle), Math.cos(angle), 0, 0, 0, 1];
  const left = new WristMapper();
  const right = new WristMapper();
  left.anchor(IDENTITY, [0, 0, 0]);
  right.anchor(IDENTITY, [0, 0, 0]);
  let a = [0, 0, 0];
  let b = [0, 0, 0];
  for (let t = 0; t < 40; t++) {
    a = left.update(about(0.5), 1000 + t * 33);
    b = right.update(about(-0.5), 1000 + t * 33);
  }
  assert.ok(a[2] > 0.4, `left turn should yaw positive, got ${a[2]}`);
  assert.ok(b[2] < -0.4, `right turn should yaw negative, got ${b[2]}`);
  assert.ok(Math.abs(a[2] + b[2]) < 1e-6, "the two directions must be symmetric");
}

// A steep downward tilt reaches the claw's full downward range, and reversing
// out of the limit responds on the first frame (no wind-up to unwind).
{
  const pitchDown = (angle) => [Math.cos(angle), 0, Math.sin(angle), 0, 1, 0, -Math.sin(angle), 0, Math.cos(angle)];
  const mapper = new WristMapper();
  mapper.anchor(IDENTITY, [0, 0, 0]);
  let value = [0, 0, 0];
  for (let t = 0; t < 120; t++) value = mapper.update(pitchDown(1.5), 1000 + t * 33);
  assert.ok(value[1] > WRIST_LIMITS[1][1] - 0.02, `downward pitch must reach its limit, got ${value[1]}`);
  const reversed = mapper.update(pitchDown(1.4), 1000 + 121 * 33);
  assert.ok(reversed[1] < value[1] - 1e-6, "reversing at the pitch limit must move back immediately");
}

/** 21 landmarks: a flat open hand whose only free parameter is the thumb/index
 * gap, expressed in palm widths so it survives translation and scaling. */
function hand({ gap = 0.6, dx = 0, dy = 0, scale = 1 } = {}) {
  const at = (x, y) => ({ x: 0.5 + dx + x * scale, y: 0.5 + dy + y * scale, z: 0 });
  const points = Array.from({ length: 21 }, () => at(0, 0));
  points[0] = at(0, 0.25);
  points[1] = at(0.05, 0.18);
  points[2] = at(0.09, 0.1);
  points[3] = at(0.11, 0.02);
  for (const [base, x] of [[5, 0.06], [9, 0], [13, -0.06], [17, -0.12]])
    for (let k = 0; k < 4; k++) points[base + k] = at(x, 0.08 - k * 0.07);
  points[8] = at(0.06, -0.13);
  points[4] = at(0.06 + gap * 0.16, -0.13);
  return { landmarks: [points], worldLandmarks: [points], handedness: [[{ categoryName: "Right" }]] };
}

// Only the thumb/index gap moves the jaws; the other fingers are not a grip.
{
  const open = measureHand(hand({ gap: 1.4 }), 640, 480);
  const shut = measureHand(hand({ gap: 0 }), 640, 480);
  assert.ok(open.valid && shut.valid, "both hands should track");
  assert.ok(open.grip > 0.6, `a wide thumb/index gap must open the jaws, got ${open.grip}`);
  assert.equal(shut.grip, 0, "a closed pinch must shut the jaws");
  // Moving the whole hand must not touch the gripper.
  const moved = measureHand(hand({ gap: 1.4, dx: 0.15, dy: -0.1 }), 640, 480);
  assert.ok(Math.abs(moved.grip - open.grip) < 1e-9, "translation must not change the grip");
}

// Position is relative to wherever the hand was anchored, and mirrored.
{
  const mapper = new HandMapper();
  const start = measureHand(hand(), 640, 480);
  mapper.anchor(start, [0, 0, 0]);
  let value = { position: [0, 0, 0], grip: 1 };
  for (let t = 0; t < 60; t++) value = mapper.map(measureHand(hand({ dx: 0.1, dy: -0.1 }), 640, 480), 1000 + t * 33);
  assert.ok(value.position[0] < -0.2, "hand right (mirrored) drives the claw right");
  assert.ok(value.position[1] > 0.2, "hand up drives the claw up");
  let closer = { position: [0, 0, 0], grip: 1 };
  mapper.anchor(start, [0, 0, 0]);
  for (let t = 0; t < 60; t++) closer = mapper.map(measureHand(hand({ scale: 1.6 }), 640, 480), 1000 + t * 33);
  assert.ok(closer.position[2] > 0.2, "leaning toward the camera reaches the claw forward");
}

console.log("handControl: ok");
