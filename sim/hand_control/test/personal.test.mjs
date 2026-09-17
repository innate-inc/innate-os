import test from "node:test";
import assert from "node:assert/strict";
import {
  thumbBaseFrame,
  rotationVector,
  PersonalMapper,
  PERSONAL_WRIST_MAX,
  gripFor,
  personalGrip,
  floorCorrection,
  floorFeatures,
  personalSample,
  yawFor,
  yawGeometry,
} from "../src/personal.mjs";

function points() {
  const p = Array.from({ length: 21 }, () => ({ x: 0, y: 0, z: 0 }));
  p[0] = { x: 0, y: 0.07, z: 0 };
  p[2] = { x: 0.025, y: 0.035, z: 0 };
  p[5] = { x: 0.012, y: 0.02, z: 0 };
  p[4] = { x: 0.06, y: 0.01, z: 0 };
  p[8] = { x: 0.01, y: -0.03, z: 0 };
  return p;
}
const reference = thumbBaseFrame(points());
const profile = {
  reference,
  rotation: {
    matrix: [
      [-1, 0, 0],
      [0, 0.7, 0],
    ],
    response: [0, 1].map(() => ({ deadzone: 0.012, negative: 1, positive: 1 })),
  },
  grip: { knots: [0.3, 1, 1.5], neutral: 0.55 },
  workspace: { span: [0.045, 0.1, 0.13] },
  position: {
    matrix: [
      [0, 0, 0.2],
      [-0.4, 0, 0],
      [0, -0.3, 0],
    ],
    compensation: Array.from({ length: 3 }, () => [0, 0, 0, 0, 0]),
  },
};
const sample = {
  valid: true,
  orientation: reference,
  aperture: 1,
  camera: [0.5, 0.5, -2],
};
const matmul = (a, b) =>
  Array.from({ length: 9 }, (_, i) =>
    [0, 1, 2].reduce(
      (s, k) => s + a[Math.floor(i / 3) * 3 + k] * b[k * 3 + (i % 3)],
      0,
    ),
  );
const turn = (angle) => [
  Math.cos(angle),
  -Math.sin(angle),
  0,
  Math.sin(angle),
  Math.cos(angle),
  0,
  0,
  0,
  1,
];
const tilt = (angle) => [
  Math.cos(angle),
  0,
  Math.sin(angle),
  0,
  1,
  0,
  -Math.sin(angle),
  0,
  Math.cos(angle),
];

test("yaw stays one-to-one at level and vertical pitch, even with tilt calibration", () => {
  for (const pitch of [0, 0.6, Math.PI / 2, 1.7]) {
    const orientation = matmul(tilt(pitch), reference);
    const geometry = yawGeometry(orientation, reference);
    const p = {
      ...profile,
      yaw: {
        width: 0.3,
        centers: [[...geometry.tilt, 0, 0]],
        coefficients: [0.2],
      },
    };
    const start = { ...sample, orientation };
    for (const angle of [-0.4, -0.15, 0.15, 0.4]) {
      const rotated = {
        ...start,
        orientation: matmul(turn(-angle), orientation),
      };
      assert.ok(
        Math.abs(yawFor(rotated, p) - yawFor(start, p) - angle) < 1e-10,
      );
      assert.ok(
        yawGeometry(rotated.orientation, reference).tilt.every(
          (v, i) => Math.abs(v - geometry.tilt[i]) < 1e-10,
        ),
      );
    }
  }
});

test("personal yaw reaches both sides, returns to neutral, and reanchors without a jump", () => {
  const mapper = new PersonalMapper(profile);
  mapper.anchor(sample, [0.1, 0.2, 0.3], 0.55, [0, 0, 0.12]);
  let now = 0;
  for (const angle of [0.3, -0.3, 0, 1, 0]) {
    let value;
    const s = { ...sample, orientation: matmul(turn(-angle), reference) };
    for (let i = 0; i < 80; i++) value = mapper.map(s, (now += 33));
    const delta = Math.sign(angle) * Math.max(0, Math.abs(angle) - 0.02);
    const expected = Math.max(-0.45, Math.min(0.45, 0.12 + delta));
    assert.ok(Math.abs(value.wrist[2] - expected) < 0.001);
  }
  const rotated = { ...sample, orientation: matmul(turn(-0.3), reference) };
  mapper.anchor(rotated, [0.1, 0.2, 0.3], 0.55, [0.1, 0.7, -0.23]);
  assert.deepEqual(mapper.map(rotated, now + 100).wrist, [0.1, 0.7, -0.23]);
});

test("pinching and translation cannot create yaw; small turns still respond at vertical pitch", () => {
  const p = {
    ...profile,
    yaw: { width: 0.2, centers: [[0, 0, 1, 0, 0]], coefficients: [0.3] },
  };
  assert.equal(
    yawFor({ ...sample, aperture: 0, camera: [10, 20, 30] }, p),
    yawFor(sample, p),
  );
  const mapper = new PersonalMapper(profile);
  const tilted = matmul(tilt(1.55), reference);
  // The profile-reference rotation stays below its 2.7-rad ambiguity guard.
  const before = { ...sample, orientation: matmul(turn(0.02), tilted) };
  mapper.anchor(before, [0, 0, 0], 0.55, [0, 0, -0.2]);
  const after = { ...sample, orientation: matmul(turn(-0.03), tilted) };
  let value;
  for (let i = 0; i < 80; i++) value = mapper.map(after, i * 33);
  assert.ok(Math.abs(value.wrist[2] - -0.17) < 0.001);
});
test("taught wrist frame survives closing and arbitrary movement of unused fingertips", () => {
  const p = points();
  p[4] = { ...p[8] };
  for (const i of [10, 11, 12, 14, 15, 16, 18, 19, 20])
    p[i] = { x: 0.7, y: -0.3, z: 0.4 };
  assert.deepEqual(thumbBaseFrame(p), reference);
  const translated = p.map((v) => ({
    x: v.x * 2 + 0.1,
    y: v.y * 2 - 0.4,
    z: v.z * 2 + 0.7,
  }));
  assert.ok(
    rotationVector(thumbBaseFrame(translated), reference).every(
      (v) => Math.abs(v) < 1e-10,
    ),
  );
});
test("relative reanchoring holds a raised arm and does not impose a ground reference", () => {
  const mapper = new PersonalMapper(profile),
    offset = [0.4, 0.6, -0.2];
  mapper.anchor(sample, offset, 0.55, [0.2, -0.1, 0]);
  const v = mapper.map(sample, 0);
  assert.deepEqual(v.position, offset);
  assert.deepEqual(v.wrist, [0.2, -0.1, 0]);
  const other = { ...sample, camera: [0.2, 0.8, -1] };
  mapper.anchor(other, offset, 0.55, [0.2, -0.1, 0]);
  assert.deepEqual(mapper.map(other, 50).position, offset);
});
test("calibrated grip spans closed, neutral and open while movement stays bounded", () => {
  assert.equal(gripFor(0.3, profile), 0);
  assert.equal(gripFor(1, profile), 0.55);
  assert.equal(gripFor(1.5, profile), 1);
  const mapper = new PersonalMapper(profile);
  mapper.anchor(sample, [0, 0, 0], 0.55);
  let v;
  for (let t = 0; t < 1500; t += 30)
    v = mapper.map({ ...sample, camera: [100, -100, 100], aperture: 0 }, t);
  assert.ok(v.position.every((n) => Math.abs(n) <= 1));
  assert.ok(v.grip < 0.001);
  assert.equal(mapper.map({ ...sample, valid: false }, 1600), null);
});
test("unobservable frames and half-turns hold rather than flip the wrist", () => {
  assert.equal(thumbBaseFrame(null), null);
  assert.equal(
    thumbBaseFrame(Array.from({ length: 21 }, () => ({ x: 0, y: 0, z: 0 }))),
    null,
  );
  const half = reference.map((v, i) => (Math.floor(i / 3) === 0 ? v : -v));
  assert.equal(rotationVector(half, reference), null);
});

test("a saturated personal pitch returns to the same neutral", () => {
  const mapper = new PersonalMapper(profile);
  mapper.anchor(sample, [0, 0, 0], 0.55);
  const c = Math.cos(2.6),
    s = Math.sin(2.6),
    r = [c, 0, s, 0, 1, 0, -s, 0, c];
  const rotated = Array.from({ length: 9 }, (_, i) =>
    [0, 1, 2].reduce(
      (sum, k) =>
        sum + r[Math.floor(i / 3) * 3 + k] * reference[k * 3 + (i % 3)],
      0,
    ),
  );
  let value;
  for (let t = 0; t < 1500; t += 30)
    value = mapper.map({ ...sample, orientation: rotated }, t);
  assert.ok(Math.abs(value.wrist[1] - PERSONAL_WRIST_MAX[1]) < 0.001);
  assert.equal(PERSONAL_WRIST_MAX[1], Math.PI / 2);
  for (let t = 1500; t < 3000; t += 30) value = mapper.map(sample, t);
  assert.ok(Math.abs(value.wrist[1]) < 0.001);
});

test("floor corrections contain no absolute hand position or size", () => {
  const makeSample = (scale, shift) =>
    personalSample({
      landmarks: [
        points().map((p) => ({
          x: 0.4 + p.x * scale + shift,
          y: 0.4 + p.y * scale + shift,
          z: p.z * scale,
        })),
      ],
      worldLandmarks: [
        points().map((p) => ({
          x: p.x * scale + shift,
          y: p.y * scale + shift,
          z: p.z * scale + shift,
        })),
      ],
    });
  const s = makeSample(2, 0),
    moved = makeSample(3, 0.1);
  assert.ok(s.valid && moved.valid);
  const a = floorFeatures(s, profile),
    b = floorFeatures(moved, profile);
  assert.ok(a.every((v, i) => Math.abs(v - b[i]) < 1e-10));
  assert.ok(s.yawShape.every((v, i) => Math.abs(v - moved.yawShape[i]) < 1e-10));
  const p = {
    ...profile,
    floor: {
      scales: [1, 1, 1, 1, 1, 0.3, 0.3, 0.2],
      width: 0.65,
      centers: [floorFeatures(s, profile)],
      coefficients: [[0.1, 0.7, 0.01, -0.02, 0.03]],
    },
  };
  const first = floorCorrection(s, p),
    second = floorCorrection(moved, p);
  assert.ok(first.every((v, i) => Math.abs(v - second[i]) < 1e-10));
  assert.ok(
    floorCorrection({ ...s, imageAxes: [10, 10, 10, 10] }, p).every(
      (v) => Math.abs(v) < 1e-10,
    ),
  );
});

test("a visibly closed downward pinch stays closed despite a false depth gap", () => {
  const p = {
    ...profile,
    floor: {
      grip: {
        pitch: [0, Math.PI / 4, 1.4],
        closed: [0.2, 0.38, 0.38],
        neutral: [1.46, 2.07, 2.13],
        open: [2.32, 2.42, 2.5],
      },
    },
  };
  const closed = { ...sample, aperture: 0.9, screenAperture: 0.32 };
  assert.equal(personalGrip(closed, p, [0, 1.4]), 0);
  assert.equal(
    personalGrip({ ...closed, screenAperture: 2.6 }, p, [0, 1.4]),
    1,
  );
  assert.equal(personalGrip(closed, p, [0, 0]), gripFor(closed.aperture, p));
});
