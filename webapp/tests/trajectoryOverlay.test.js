// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for js/teleop/trajectoryOverlay.js — zero dependencies, plain node:
//   node tests/trajectoryOverlay.test.js
// The projection is a port of the controller app's TrajectoryOverlay; these
// pin the geometry that would silently drift on a bad edit: the world->robot
// frame change (yaw + the ROS-left to camera-right flip), behind-camera and
// out-of-frame culling (each cull splits the route into segments), the
// pitch-dependent camera height, and the ribbon polygon's shape.

import assert from "node:assert/strict";

const { CAMERA, cameraHeight, robotRelative, projectToImage, ribbon } = await import(
  "../js/teleop/trajectoryOverlay.js"
);

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** @param {number} a @param {number} b @param {string} what */
function close(a, b, what) {
  assert.ok(Math.abs(a - b) < 1e-9, `${what}: ${a} !~ ${b}`);
}

test("camera height slides with pitch across the mount's compensation range", () => {
  close(cameraHeight(0), CAMERA.HEIGHT_M, "level");
  close(cameraHeight(CAMERA.MIN_PITCH_DEG), CAMERA.HEIGHT_M - CAMERA.PITCH_HEIGHT_COMP_M, "full down");
  close(cameraHeight(CAMERA.MAX_PITCH_DEG), CAMERA.HEIGHT_M + CAMERA.PITCH_HEIGHT_COMP_M, "full up");
  close(cameraHeight(-90), CAMERA.HEIGHT_M - CAMERA.PITCH_HEIGHT_COMP_M, "clamped below");
});

test("robotRelative: identity pose keeps forward, flips ROS-left to camera-right", () => {
  const [ahead, left] = robotRelative(
    [
      { x: 2, y: 0 },
      { x: 0, y: 1 },
    ],
    { x: 0, y: 0, yaw: 0 },
  );
  close(ahead.fwd, 2, "ahead.fwd");
  close(ahead.right, 0, "ahead.right");
  close(left.fwd, 0, "left.fwd");
  close(left.right, -1, "left point is negative right");
});

test("robotRelative: translation and yaw are both removed", () => {
  // Robot at (2, 3) facing +y; a point 1 m dead ahead of it is (2, 4).
  const [p] = robotRelative([{ x: 2, y: 4 }], { x: 2, y: 3, yaw: Math.PI / 2 });
  close(p.fwd, 1, "fwd");
  close(p.right, 0, "right");
});

test("projectToImage: a point dead ahead lands on the optical axis column, below the horizon", () => {
  const [[p]] = projectToImage([{ fwd: 1, right: 0 }], 0, 640, 480);
  const scale = 640 / CAMERA.CALIB_W;
  close(p.x, CAMERA.CX * scale, "u is the principal-point column");
  const fy = CAMERA.FY * scale * CAMERA.FY_SCALE;
  close(p.y, fy * (CAMERA.HEIGHT_M / 1) + CAMERA.CY * scale, "v from height over depth");
  close(p.depth, 1, "depth is the forward distance at zero pitch");
});

test("projectToImage culls behind-camera and out-of-frame points", () => {
  const projected = projectToImage(
    [
      { fwd: -1, right: 0 }, // behind
      { fwd: 0.2, right: 5 }, // far off to the side
      { fwd: 1, right: 0 }, // visible
    ],
    0,
    640,
    480,
  );
  assert.equal(projected.length, 1);
  assert.equal(projected[0].length, 1);
});

test("a route that leaves the frame and re-enters draws two disjoint ribbons, not a bridge", () => {
  const segments = projectToImage(
    [
      { fwd: 1, right: -0.4 },
      { fwd: 1.5, right: -0.4 },
      { fwd: 0.2, right: 5 }, // swings out of frame
      { fwd: 2, right: 0.5 },
      { fwd: 2.5, right: 0.5 },
    ],
    0,
    640,
    480,
  );
  assert.equal(segments.length, 2, "the culled point must split the route");
  const polys = segments.map((seg, i) => ribbon(seg, 480, i === 0));
  assert.ok(polys[0] && polys[1]);
  const maxX0 = Math.max(...polys[0].map((p) => p.x));
  const minX1 = Math.min(...polys[1].map((p) => p.x));
  assert.ok(maxX0 < minX1, `ribbons must not bridge the culled gap (${maxX0} vs ${minX1})`);
  assert.ok(Math.max(...polys[0].map((p) => p.y)) >= 480, "leading ribbon still anchors at the robot's feet");
  assert.ok(
    Math.max(...polys[1].map((p) => p.y)) < 480,
    "re-entering ribbon must not be extended down to the bottom edge",
  );
});

test("projectToImage: pitching up pushes the ground lower in the image", () => {
  const [[level]] = projectToImage([{ fwd: 1, right: 0 }], 0, 640, 480);
  const [[up]] = projectToImage([{ fwd: 1, right: 0 }], 15, 640, 480);
  assert.ok(up.y > level.y, `pitch up should increase v (${up.y} vs ${level.y})`);
});

test("ribbon: straight-ahead path yields a symmetric polygon anchored at the bottom edge", () => {
  const poly = ribbon(
    [
      { x: 320, y: 400, depth: 1 },
      { x: 320, y: 300, depth: 2 },
      { x: 320, y: 250, depth: 3 },
    ],
    480,
  );
  assert.ok(poly);
  // Start vertex pair + one pair per point, left edge then reversed right edge.
  assert.equal(poly.length, 8);
  close(Math.max(...poly.map((p) => p.y)), 480, "extends to the bottom edge");
  // Left edge is poly[0..3], right edge poly[4..7] reversed — mirror pairs.
  for (let i = 0; i < 4; i++) {
    close(poly[i].x - 320, 320 - poly[7 - i].x, "left/right edges mirror around the centreline");
    close(poly[i].y, poly[7 - i].y, "mirror pairs share a row");
  }
  // Widths taper with depth: the base pair sits further from the centreline
  // than the far pair.
  const baseHalf = Math.abs(poly[0].x - 320);
  const farHalf = Math.abs(poly[3].x - 320);
  assert.ok(baseHalf > farHalf, `ribbon should taper (${baseHalf} vs ${farHalf})`);
});

test("ribbon needs at least two projected points", () => {
  assert.equal(ribbon([], 480), null);
  assert.equal(ribbon([{ x: 1, y: 1, depth: 1 }], 480), null);
});

console.log(`\n${passed} tests passed`);
