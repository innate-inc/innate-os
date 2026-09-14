import test from "node:test";
import assert from "node:assert/strict";
import {
  Calibration,
  GroundCalibration,
  visibleHeightRange,
  HandMapper,
  measureHand,
  continuousHand,
} from "../src/control.mjs";

const sample = (extra = {}) => ({
  valid: true,
  x: 0.5,
  y: 0.5,
  scale: 0.13,
  side: "Right",
  grip: 1,
  gripValid: true,
  ...extra,
});
function hand() {
  const points = Array.from({ length: 21 }, () => ({ x: 0.5, y: 0.5, z: 0 }));
  points[0] = { x: 0.5, y: 0.75, z: 0 };
  points[4] = { x: 0.8, y: 0.5, z: 0 };
  for (const [finger, x] of [
    [5, 0.62],
    [9, 0.54],
    [13, 0.46],
    [17, 0.38],
  ])
    for (let k = 0; k < 4; k++)
      points[finger + k] = { x, y: 0.55 - k * 0.105, z: 0 };
  return { landmarks: [points], handedness: [[{ categoryName: "Right" }]] };
}

test("3D mapping anchors without jumps; each hand axis moves only its corresponding robot axis", () => {
  for (const [input, axis, sign] of [
    [{ x: 0.3 }, 0, -1],
    [{ x: 0.7 }, 0, 1],
    [{ y: 0.3 }, 1, 1],
    [{ y: 0.7 }, 1, -1],
    [{ scale: 0.2 }, 2, 1],
    [{ scale: 0.09 }, 2, -1],
  ]) {
    const mapper = new HandMapper();
    mapper.anchor(sample(), [0, 0, 0]);
    assert.deepEqual(mapper.map(sample(), 0).position, [0, 0, 0]);
    let value;
    for (let t = 30; t < 1000; t += 30)
      value = mapper.map(sample(input), t).position;
    assert.ok(value[axis] * sign > 0.5);
    value.forEach((v, i) => {
      if (i !== axis) assert.equal(v, 0);
    });
  }
});

test("dead zones, workspace bounds and harmless handedness flips", () => {
  const mapper = new HandMapper();
  mapper.anchor(sample(), [0.2, -0.1, 0.3]);
  assert.deepEqual(
    mapper.map(sample({ x: 0.501, scale: 0.1301, side: "Left" }), 0).position,
    [0.2, -0.1, 0.3],
  );
  assert.equal(mapper.map(sample({ valid: false }), 100), null);
  for (let t = 0; t < 2000; t += 30)
    assert.ok(
      mapper
        .map(sample({ x: 10, y: -10, scale: 10 }), t)
        .position.every((v) => v <= 1 && v >= -1),
    );
  assert.equal(continuousHand(sample(), sample({ side: "Left" })), true);
  assert.equal(continuousHand(sample(), sample({ x: 0.9 })), false);
});

test("only thumb/index separation controls the gripper, independently of the arm", () => {
  const opened = hand();
  const together = structuredClone(opened);
  together.landmarks[0][4] = { ...together.landmarks[0][8], x: 0.64 };
  const closed = measureHand(together);
  assert.equal(closed.valid, true);
  assert.equal(closed.grip, 0);
  assert.equal(closed.scale, measureHand(opened).scale);
  assert.equal(measureHand(opened).grip, 1);
  const halfway = structuredClone(opened);
  halfway.landmarks[0][4] = { ...halfway.landmarks[0][8], x: 0.72 };
  const intermediate = measureHand(halfway).grip;
  assert.ok(intermediate > 0.1 && intermediate < 0.9);

  for (const pose of [opened, halfway, together]) {
    const ignoredFingers = structuredClone(pose);
    // An index/middle pinch and curled or clipped remaining fingers must not
    // change either the grip value or whether the grip can be updated.
    ignoredFingers.landmarks[0][12] = { ...ignoredFingers.landmarks[0][8] };
    ignoredFingers.landmarks[0][16].y = 0.7;
    ignoredFingers.landmarks[0][20].x = -0.1;
    assert.equal(measureHand(ignoredFingers).grip, measureHand(pose).grip);
    assert.equal(measureHand(ignoredFingers).gripValid, true);
    const scaled = structuredClone(pose);
    for (const p of scaled.landmarks[0]) {
      p.x = 0.5 + (p.x - 0.5) * 0.8;
      p.y = 0.5 + (p.y - 0.5) * 0.8;
      p.z *= 0.8;
    }
    assert.ok(
      Math.abs(measureHand(scaled).grip - measureHand(pose).grip) < 1e-9,
    );
  }
  const mapper = new HandMapper();
  mapper.anchor(measureHand(opened), [0, 0, 0]);
  let value;
  for (let t = 0; t < 1000; t += 30) value = mapper.map(closed, t);
  assert.ok(value.grip < 0.01);
  assert.deepEqual(value.position, [0, 0, 0]);
  for (let t = 1000; t < 2000; t += 30)
    value = mapper.map(measureHand(opened), t);
  assert.ok(value.grip > 0.99);
});

test("turning a palm does not clutch; clipped fingertips hold grip while retaining palm control", () => {
  const result = hand();
  for (const p of result.landmarks[0]) {
    const x = p.x - 0.5;
    p.x = 0.5 + x * Math.cos(1.2);
    p.z = x * Math.sin(1.2);
  }
  const turned = measureHand(result);
  assert.equal(turned.valid, true);
  assert.ok(Math.abs(turned.scale - measureHand(hand()).scale) < 1e-9);
  result.landmarks[0][8].y = -0.1;
  const clipped = measureHand(result);
  assert.equal(clipped.valid, true);
  assert.equal(clipped.gripValid, false);
  const mapper = new HandMapper();
  mapper.anchor(turned, [0, 0, 0], 0.4);
  assert.equal(mapper.map(clipped, 0).grip, 0.4);
  result.landmarks[0][0].x = -0.1;
  assert.equal(measureHand(result).valid, false);
  assert.equal(measureHand({ landmarks: [] }).valid, false);
});

test("calibration accepts classification flicker but requires a stable palm", () => {
  const calibration = new Calibration();
  for (let t = 0; t < 800; t += 40)
    assert.equal(
      calibration.update(sample({ side: t % 80 ? "Left" : "Right" }), t).ready,
      false,
    );
  assert.equal(calibration.update(sample(), 800).ready, true);
  assert.equal(calibration.update(sample({ valid: false }), 840).ready, false);
  assert.equal(calibration.update(sample(), 880).progress, 0);
  assert.equal(calibration.update(sample({ x: 0.8 }), 920).progress, 0);
});

test("ground calibration uses the lowest visible palm position, with room for tracking", () => {
  const calibrator = new GroundCalibration();
  const center = measureHand(hand());
  for (let t = 0; t < 1000; t += 40)
    assert.equal(calibrator.update(center, t).ready, false);
  const lowered = hand();
  for (const point of lowered.landmarks[0]) point.y += 0.19;
  const low = measureHand(lowered);
  assert.equal(low.valid, true);
  assert.ok(
    Math.abs(
      visibleHeightRange(low).bottom - visibleHeightRange(center).bottom,
    ) < 1e-9,
  );
  let calibration;
  for (let t = 1000; t <= 1840; t += 40)
    calibration = calibrator.update(low, t);
  assert.equal(calibration.ready, true);
  assert.ok(calibration.bounds.bottom > low.y);
  assert.ok(calibration.bounds.bottom - low.y < 0.04);
  const mapper = new HandMapper();
  mapper.setGround(calibration.bounds);
  mapper.anchor(low, [0, -1, 0]);
  assert.equal(mapper.map(low, 0).position[1], -1);
  const lifted = { ...low, y: low.y - 0.2 };
  let value;
  for (let t = 40; t < 1000; t += 40) value = mapper.map(lifted, t);
  assert.ok(value.position[1] > -0.2);
  assert.equal(value.position[0], 0);
  assert.equal(value.position[2], 0);
  const fixedHeight = mapper.vertical(lifted);
  mapper.anchor(lifted, [0.2, 0.8, -0.2]);
  assert.equal(mapper.vertical(lifted), fixedHeight);
  for (const sensitivity of [0.6, 1, 1.5]) {
    mapper.sensitivity = sensitivity;
    assert.equal(mapper.vertical(low), -1);
    assert.equal(mapper.vertical({ ...low, y: 1 }), -1);
    assert.ok(mapper.vertical({ ...low, y: -1 }) <= 1);
  }
  for (let t = 1000; t < 2200; t += 40) value = mapper.map(low, t);
  assert.ok(Math.abs(value.position[1] + 1) < 1e-6);
});
