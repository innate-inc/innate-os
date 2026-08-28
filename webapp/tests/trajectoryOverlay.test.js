// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Focused geometry regressions for the camera-projected navigation ribbon.

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

test("world points are transformed into the robot camera frame", () => {
  const [ahead, left] = robotRelative(
    [
      { x: 2, y: 4 },
      { x: 1, y: 3 },
    ],
    { x: 2, y: 3, yaw: Math.PI / 2 },
  );
  close(ahead.fwd, 1, "ahead.fwd");
  close(ahead.right, 0, "ahead.right");
  close(left.fwd, 0, "left.fwd");
  close(left.right, -1, "robot-left becomes negative camera-right");
});

test("projection responds to head pitch and its compensated camera height", () => {
  close(cameraHeight(0), CAMERA.HEIGHT_M, "level height");
  close(
    cameraHeight(CAMERA.MAX_PITCH_DEG),
    CAMERA.HEIGHT_M + CAMERA.PITCH_HEIGHT_COMP_M,
    "raised height",
  );
  const [[level]] = projectToImage([{ fwd: 1, right: 0 }], 0, 640, 480).segments;
  const [[up]] = projectToImage([{ fwd: 1, right: 0 }], 15, 640, 480).segments;
  close(level.x, CAMERA.CX * (640 / CAMERA.CALIB_W), "optical-axis column");
  assert.ok(up.y > level.y, `pitching up should push the ground down (${up.y} vs ${level.y})`);
});

test("a route that leaves and re-enters the frame is split instead of bridged", () => {
  const { segments, startVisible } = projectToImage(
    [
      { fwd: 1, right: -0.4 },
      { fwd: 1.5, right: -0.4 },
      { fwd: 0.2, right: 5 },
      { fwd: 2, right: 0.5 },
      { fwd: 2.5, right: 0.5 },
    ],
    0,
    640,
    480,
  );
  assert.equal(segments.length, 2);
  const first = ribbon(segments[0], 640, 480, startVisible);
  const second = ribbon(segments[1], 640, 480, false);
  assert.ok(first && second);
  assert.ok(Math.max(...first.map((p) => p.x)) < Math.min(...second.map((p) => p.x)));
  assert.ok(Math.max(...second.map((p) => p.y)) < 480, "re-entering run must not anchor");
});

test("a route whose first point is culled is not anchored into a wedge", () => {
  const { segments, startVisible } = projectToImage(
    [
      { fwd: 0.2, right: 5 },
      { fwd: 1, right: -0.2 },
      { fwd: 1.05, right: 0.2 },
    ],
    0,
    640,
    480,
  );
  assert.equal(startVisible, false);
  const poly = ribbon(segments[0], 640, 480, startVisible);
  assert.ok(poly);
  assert.ok(Math.max(...poly.map((p) => p.y)) < 480, "culled start must not touch the bottom");
});

test("the ribbon is symmetric, tapers with distance, and needs two points", () => {
  assert.equal(ribbon([{ x: 1, y: 1, depth: 1 }], 640, 480), null);
  const poly = ribbon(
    [
      { x: 320, y: 400, depth: 1 },
      { x: 320, y: 300, depth: 2 },
      { x: 320, y: 250, depth: 3 },
    ],
    640,
    480,
  );
  assert.ok(poly);
  assert.equal(poly.length, 8);
  for (let i = 0; i < 4; i++) {
    close(poly[i].x - 320, 320 - poly[7 - i].x, "symmetric edge pair");
  }
  assert.ok(Math.abs(poly[0].x - 320) > Math.abs(poly[3].x - 320), "ribbon tapers");
});

console.log(`\n${passed} tests passed`);
