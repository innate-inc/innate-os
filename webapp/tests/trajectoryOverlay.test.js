// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Focused geometry regressions for the camera-projected navigation ribbon.

import assert from "node:assert/strict";

const { CAMERA, cameraHeight, robotRelative, projectToImage, ribbon, SIM_CAMERA } = await import(
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
  const ahead = [
    { fwd: 1, right: 0 },
    { fwd: 1.5, right: 0 },
  ];
  const [[level]] = projectToImage(ahead, 0, 640, 480).segments;
  const [[up]] = projectToImage(ahead, 15, 640, 480).segments;
  close(level.x, CAMERA.CX * (640 / CAMERA.CALIB_W), "optical-axis column");
  assert.ok(up.y > level.y, `pitching up should push the ground down (${up.y} vs ${level.y})`);
});

test("a route that leaves and re-enters the frame is split instead of bridged", () => {
  const { segments, startAtRobot } = projectToImage(
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
  assert.equal(segments.length, 2, "two runs, never bridged into one");
  // Each run stops at the frame edge the route actually crossed, and every
  // drawn point stays inside the frame.
  for (const seg of segments) {
    for (const p of seg) {
      assert.ok(p.x >= 0 && p.x <= 640 && p.y >= 0 && p.y <= 480, `outside the frame: ${p.x},${p.y}`);
    }
  }
  close(segments[0][segments[0].length - 1].x, 640, "first run exits at the right edge");
  close(segments[1][0].x, 640, "second run re-enters at the right edge");
  const first = ribbon(segments[0], 640, 480, startAtRobot);
  const second = ribbon(segments[1], 640, 480, false);
  assert.ok(first && second);
  assert.ok(Math.max(...second.map((p) => p.y)) < 480, "re-entering run must not anchor");
});

test("an edge crossing the view is drawn though both its poses are outside", () => {
  // The poses sit off opposite sides at the same depth, so the line between
  // them sweeps straight across the frame.
  const { segments } = projectToImage(
    [
      { fwd: 1, right: -1.5 },
      { fwd: 1, right: 1.5 },
    ],
    0,
    640,
    480,
  );
  assert.equal(segments.length, 1, "the crossing stretch must survive");
  const [run] = segments;
  close(run[0].x, 0, "clipped to the left edge");
  close(run[run.length - 1].x, 640, "clipped to the right edge");
  for (const p of run) close(p.y, run[0].y, "a constant-depth crossing stays on one row");
  assert.ok(ribbon(run, 640, 480, false), "and it yields a drawable ribbon");
});

test("a route whose first point is culled is not anchored into a wedge", () => {
  const { segments, startAtRobot } = projectToImage(
    [
      { fwd: 0.2, right: 5 },
      { fwd: 1, right: -0.2 },
      { fwd: 1.05, right: 0.2 },
    ],
    0,
    640,
    480,
  );
  assert.equal(startAtRobot, false);
  const poly = ribbon(segments[0], 640, 480, startAtRobot);
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
  assert.equal(ahead.startAtRobot, false, "a visible but distant start is not the robot's");
  const distant = ribbon(ahead.segments[0], 640, 480, ahead.startAtRobot);
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
  assert.equal(near.startAtRobot, true);
  const anchored = ribbon(near.segments[0], 640, 480, near.startAtRobot);
  assert.ok(anchored);
  close(Math.max(...anchored.map((p) => p.y)), 480, "a start at the robot reaches the feet");
});

test("the sim camera is an exact pinhole: centred, square-pixel, fixed height", () => {
  const lens = SIM_CAMERA.lens(1600, 900);
  close(lens.cx, 800, "principal point x");
  close(lens.cy, 450, "principal point y");
  close(lens.fx, lens.fy, "square pixels");
  // The half-frame subtends half the render's vertical FOV.
  close((Math.atan(450 / lens.fy) * 360) / Math.PI, 68.5, "vertical FOV");
  // Riding the head pivot, the sim camera does not rise or fall with pitch.
  close(SIM_CAMERA.height(-30), SIM_CAMERA.height(30), "height is pitch-independent");

  // Straight ahead lands on the sim's optical axis; the real lens is off-centre.
  const ahead = [
    { fwd: 1, right: 0 },
    { fwd: 1.5, right: 0 },
  ];
  const [[sim]] = projectToImage(ahead, 0, 1600, 900, SIM_CAMERA).segments;
  close(sim.x, 800, "sim optical-axis column");
  const [[real]] = projectToImage(ahead, 0, 1600, 900).segments;
  assert.ok(Math.abs(real.x - 800) > 1, "the real lens's principal point is off-centre");
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
