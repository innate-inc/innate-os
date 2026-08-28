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
  const [[level]] = projectToImage([{ fwd: 1, right: 0 }], 0, 640, 480);
  const [[up]] = projectToImage([{ fwd: 1, right: 0 }], 15, 640, 480);
  close(level.x, CAMERA.CX * (640 / CAMERA.CALIB_W), "optical-axis column");
  assert.ok(up.y > level.y, `pitching up should push the ground down (${up.y} vs ${level.y})`);
});

test("a route that leaves and re-enters the frame is split instead of bridged", () => {
  const segments = projectToImage(
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
  const first = ribbon(segments[0]);
  const second = ribbon(segments[1]);
  assert.ok(first && second);
  assert.ok(Math.max(...first.map((p) => p.x)) < Math.min(...second.map((p) => p.x)));
  assert.ok(Math.max(...second.map((p) => p.y)) < 480, "re-entering run must not anchor");
});

test("a route whose first point is culled is not anchored into a wedge", () => {
  const segments = projectToImage(
    [
      { fwd: 0.2, right: 5 },
      { fwd: 1, right: -0.2 },
      { fwd: 1.05, right: 0.2 },
    ],
    0,
    640,
    480,
  );
  assert.equal(segments[0].length, 2, "a culled start gains no connector");
  const poly = ribbon(segments[0]);
  assert.ok(poly);
  assert.ok(Math.max(...poly.map((p) => p.y)) < 480, "culled start must not touch the bottom");
});

test("only a route that truly starts at the robot is anchored to its feet", () => {
  const ahead = projectToImage(
    [
      { fwd: 1, right: 0 },
      { fwd: 1.5, right: 0 },
    ],
    0,
    640,
    480,
  );
  assert.equal(ahead[0].length, 2, "a visible but distant start gains no connector");
  const distant = ribbon(ahead[0]);
  assert.ok(distant);
  assert.ok(Math.max(...distant.map((p) => p.y)) < 480, "distant start must not touch the bottom");

  const near = projectToImage(
    [
      { fwd: 0.2, right: 0 },
      { fwd: 0.6, right: 0 },
    ],
    -20,
    640,
    480,
  );
  assert.equal(near[0].length, 3, "a start at the robot gains the feet connector");
  const anchored = ribbon(near[0]);
  assert.ok(anchored);
  assert.ok(
    Math.max(...anchored.map((p) => p.y)) >= 480,
    "the connector runs to the feet, past the frame's bottom edge",
  );
});

test("the feet connector follows the robot-to-start line, not the route's direction", () => {
  const [seg] = projectToImage(
    [
      { fwd: 0.3, right: 0.15 },
      { fwd: 0.7, right: -0.1 },
    ],
    -20,
    640,
    480,
  );
  assert.equal(seg.length, 3);
  const [connector, start, next] = seg;
  assert.ok(connector.y > 480, "the connector is projected geometry below the frame");
  assert.ok(next.x < start.x, "the route itself heads left");
  assert.ok(
    connector.x < start.x,
    "the connector heads back toward the robot, not along the extrapolated route",
  );
});

test("the ribbon is symmetric, tapers with distance, and needs two points", () => {
  assert.equal(ribbon([{ x: 1, y: 1, depth: 1 }]), null);
  const poly = ribbon([
    { x: 320, y: 400, depth: 1 },
    { x: 320, y: 300, depth: 2 },
    { x: 320, y: 250, depth: 3 },
  ]);
  assert.ok(poly);
  assert.equal(poly.length, 6);
  for (let i = 0; i < 3; i++) {
    close(poly[i].x - 320, 320 - poly[5 - i].x, "symmetric edge pair");
  }
  assert.ok(Math.abs(poly[0].x - 320) > Math.abs(poly[2].x - 320), "ribbon tapers");
});

console.log(`\n${passed} tests passed`);
