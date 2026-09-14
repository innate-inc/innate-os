import test from "node:test";
import assert from "node:assert/strict";
import {
  fingerFrame,
  relativeAngles,
  WristMapper,
  WRIST_LIMITS,
} from "../src/orientation.mjs";

function fingers() {
  const p = Array.from({ length: 21 }, () => ({ x: 0, y: 0, z: 0 }));
  p[0] = { x: 0, y: 0.07, z: 0 };
  p[2] = { x: 0.025, y: 0.035, z: 0 };
  p[5] = { x: 0.012, y: 0.02, z: 0 };
  p[4] = { x: 0.065, y: 0.015, z: 0 };
  p[8] = { x: 0.012, y: -0.035, z: 0 };
  return p;
}
function turn(points, axis, angle) {
  const c = Math.cos(angle),
    s = Math.sin(angle);
  return points.map(({ x, y, z }) =>
    axis === 0
      ? { x, y: c * y - s * z, z: s * y + c * z }
      : axis === 1
        ? { x: c * x + s * z, y, z: -s * x + c * z }
        : { x: c * x - s * y, y: s * x + c * y, z },
  );
}
const nearly = (actual, expected) =>
  actual.forEach((v, i) =>
    assert.ok(Math.abs(v - expected[i]) < 1e-8, `${actual} != ${expected}`),
  );

test("screen twist, tilt and sideways turn become independent roll, pitch and yaw", () => {
  const points = fingers(),
    reference = fingerFrame(points);
  for (const [cameraAxis, wristAxis] of [
    [2, 0],
    [0, 1],
    [1, 2],
  ])
    for (const angle of [-0.3, 0.3]) {
      const rotated = fingerFrame(turn(points, cameraAxis, angle));
      const expected = [0, 0, 0];
      expected[wristAxis] = angle;
      nearly(relativeAngles(rotated, reference), expected);
      const mapper = new WristMapper();
      mapper.anchor(reference);
      let value;
      for (let t = 0; t < 2000; t += 30) value = mapper.update(rotated, t);
      expected[wristAxis] = Math.sign(angle) * (Math.abs(angle) - 0.025);
      value.forEach((v, i) => assert.ok(Math.abs(v - expected[i]) < 1e-6));
    }
});

test("pinching changes aperture without twisting, including a fully closed rotating pinch", () => {
  const points = fingers(),
    reference = fingerFrame(points);
  for (const fraction of [1, 0.8, 0.4, 0.1, 0]) {
    const pinch = structuredClone(points);
    pinch[4] = {
      x: points[8].x + (points[4].x - points[8].x) * fraction,
      y: points[8].y + (points[4].y - points[8].y) * fraction,
      z: 0,
    };
    nearly(relativeAngles(fingerFrame(pinch), reference), [0, 0, 0]);
    nearly(
      relativeAngles(fingerFrame(turn(pinch, 2, 0.4)), reference),
      [0.4, 0, 0],
    );
  }
});

test("thumb/index pointing controls pitch even when the wrist and knuckles stay still", () => {
  const points = fingers();
  points[2] = { x: 0.04, y: 0.02, z: 0 };
  points[5] = { x: 0, y: 0.02, z: 0 };
  points[4] = { x: 0.06, y: -0.04, z: 0 };
  points[8] = { x: 0, y: -0.04, z: 0 };
  const reference = fingerFrame(points);
  for (const angle of [-0.4, -0.2, 0.2, 0.4]) {
    const tilted = structuredClone(points);
    for (const i of [4, 8]) {
      const y = points[i].y - 0.02;
      tilted[i].y = 0.02 + y * Math.cos(angle);
      tilted[i].z = y * Math.sin(angle);
    }
    nearly(relativeAngles(fingerFrame(tilted), reference), [0, angle, 0]);
    // Closing along the jaw axis preserves the same pointing direction.
    tilted[4] = { ...tilted[8] };
    nearly(relativeAngles(fingerFrame(tilted), reference), [0, angle, 0]);
  }
});

test("orientation ignores translation and scale, anchors current wrist, and holds on degenerate geometry", () => {
  const points = fingers(),
    reference = fingerFrame(points);
  nearly(
    relativeAngles(
      fingerFrame(
        points.map((p) => ({
          x: p.x * 3 + 0.4,
          y: p.y * 3 - 0.6,
          z: p.z * 3 + 0.2,
        })),
      ),
      reference,
    ),
    [0, 0, 0],
  );
  const mapper = new WristMapper();
  mapper.anchor(reference, [0.2, 0.1, -0.1]);
  nearly(mapper.update(reference, 0), [0.2, 0.1, -0.1]);
  assert.equal(
    fingerFrame(Array.from({ length: 21 }, () => ({ x: 0, y: 0, z: 0 }))),
    null,
  );
  nearly(mapper.update(null, 300), [0.2, 0.1, -0.1]);
});

test("angle wrapping and limits cannot snap a claw to the opposite side", () => {
  const points = fingers();
  const mapper = new WristMapper();
  mapper.anchor(fingerFrame(turn(points, 2, (179 * Math.PI) / 180)));
  const value = mapper.update(
    fingerFrame(turn(points, 2, (-179 * Math.PI) / 180)),
    100,
  );
  assert.ok(value[0] >= 0 && value[0] < 0.04);
  mapper.anchor(fingerFrame(points));
  for (let t = 0; t < 4000; t += 30) {
    const value = mapper.update(fingerFrame(turn(points, 2, 2)), t);
    value.forEach((v, i) => assert.ok(Math.abs(v) <= WRIST_LIMITS[i]));
  }
});

test("pitch responds immediately when reversing beyond the input limit", () => {
  const points = fingers(),
    mapper = new WristMapper();
  mapper.anchor(fingerFrame(points));
  let value;
  for (let t = 0; t < 2000; t += 30)
    value = mapper.update(fingerFrame(turn(points, 0, 1)), t);
  assert.ok(Math.abs(value[1] - WRIST_LIMITS[1]) < 0.001);
  for (let t = 2010; t < 4000; t += 30)
    value = mapper.update(fingerFrame(turn(points, 0, 0.96)), t);
  assert.ok(Math.abs(value[1] - (WRIST_LIMITS[1] - 0.04)) < 0.001);
});
