import test from "node:test";
import assert from "node:assert/strict";
import { pinchSample, PinchMapper } from "../src/pinch.mjs";

import { hand } from "./hand-fixture.mjs";

const source = hand(),
  start = pinchSample(source);
const profile = {
  workspace: { span: [0.045, 0.1, 0.13] },
  pinch: {
    positionGain: [0.35, 0.8, 0.5],
    grip: { closed: 0.18, open: 1, visibleClosed: 0.32 },
    orientation: {
      centers: [start.features, start.features.map((v) => v * 0.8)],
      coefficients: [
        [0.4, 0.7, -0.3],
        [-0.1, 0.2, 0.1],
      ],
      width: 1,
    },
    stable: {
      centers: [start.stableFeatures],
      coefficients: [[0.2, 0.4, -0.1]],
      width: 1,
    },
  },
};
const close = (a, b, tolerance = 1e-9) =>
  a.forEach((v, i) =>
    assert.ok(Math.abs(v - b[i]) < tolerance, `${v} != ${b[i]}`),
  );

test("fingertip line defines the jaw axis; midpoint defines the crosshair", () => {
  assert.ok(start.valid);
  const p = source.worldLandmarks[0],
    delta = [p[8].z - p[4].z, p[8].x - p[4].x, p[8].y - p[4].y];
  close(
    [start.features[1], start.features[4], start.features[7]],
    delta.map((v) => v / Math.hypot(...delta)),
  );
  assert.equal(
    start.x,
    1 - (source.landmarks[0][4].x + source.landmarks[0][8].x) / 2,
  );
  assert.equal(
    start.y,
    (source.landmarks[0][4].y + source.landmarks[0][8].y) / 2,
  );
});

test("orientation and aperture are independent of screen position, distance, and world origin", () => {
  const moved = structuredClone(source);
  moved.landmarks[0] = moved.landmarks[0].map((p) => ({
    x: 0.5 + (p.x - 0.5) * 1.2 + 0.05,
    y: 0.45 + (p.y - 0.45) * 1.2 - 0.04,
    z: p.z * 1.2,
  }));
  moved.worldLandmarks[0] = moved.worldLandmarks[0].map((p) => ({
    x: p.x + 0.2,
    y: p.y - 0.3,
    z: p.z + 0.1,
  }));
  const s = pinchSample(moved);
  assert.ok(s.valid);
  close(start.features, s.features);
  close(start.stableFeatures, s.stableFeatures);
  close([start.visible], [s.visible]);
});

test("twenty slow close/open cycles preserve grasp point and orientation without accumulating drift", () => {
  const m = new PinchMapper(profile),
    offset = [0.1, -0.2, 0.3],
    wrist = [0.1, 0.2, -0.1];
  m.anchor(start, offset, 1, wrist);
  let time = 0;
  for (let cycle = 0; cycle < 20; cycle++) {
    let previous = 1;
    for (let i = 0; i <= 90; i++) {
      const s = pinchSample(hand(0.09 * (1 - i / 90))),
        v = m.map(s, (time += 33));
      assert.ok(s.valid);
      close(v.position, offset, 1e-8);
      close(v.wrist, wrist, 1e-8);
      assert.ok(v.grip <= previous + 1e-8);
      previous = v.grip;
    }
    assert.ok(m.grip < 0.001);
    for (let i = 0; i <= 90; i++) {
      const v = m.map(pinchSample(hand((0.09 * i) / 90)), (time += 33));
      close(v.position, offset, 1e-8);
      close(v.wrist, wrist, 1e-8);
    }
    assert.ok(m.grip > 0.9);
  }
});

test("a pinched hand can still translate; reanchoring does not move or rotate the arm", () => {
  const m = new PinchMapper(profile);
  m.anchor(start, [0, 0, 0], 1, [0, 0.8, 0]);
  for (let i = 0; i < 60; i++) m.map(pinchSample(hand(0.001)), i * 33);
  const closed = hand(0.001);
  closed.landmarks[0] = closed.landmarks[0].map((p) => ({
    ...p,
    x: p.x + 0.06,
    y: p.y - 0.08,
  }));
  let v;
  for (let i = 60; i < 150; i++) v = m.map(pinchSample(closed), i * 33);
  assert.ok(v.position[0] > 0.15);
  assert.ok(v.position[1] > 0.08);
  assert.ok(v.grip < 0.001);
  close(v.wrist, [0, 0.8, 0], 1e-8);
  const before = v;
  m.anchor(pinchSample(closed), before.position, before.grip, before.wrist);
  v = m.map(pinchSample(closed), 5000);
  close(v.position, before.position);
  close(v.wrist, before.wrist);
});

test("pitch responds to the index bending at its knuckle even with a stationary wrist and palm", () => {
  const bent = structuredClone(source);
  bent.worldLandmarks[0][6].y -= 0.03;
  const after = pinchSample(bent);
  assert.ok(after.valid);
  assert.ok(
    Math.hypot(...after.features.map((v, i) => v - start.features[i])) > 0.2,
  );
});

test("missing, nonfinite, and degenerate tracking holds instead of sending an invalid command", () => {
  const m = new PinchMapper(profile);
  m.anchor(start, [0, 0, 0], 0.5);
  for (const r of [
    { landmarks: [] },
    { ...source, worldLandmarks: [] },
    { ...source, worldLandmarks: [Array(21).fill({ x: 0, y: 0, z: 0 })] },
    {
      ...source,
      worldLandmarks: [source.worldLandmarks[0].map((p) => ({ ...p, z: NaN }))],
    },
  ]) {
    const s = pinchSample(r);
    assert.equal(s.valid, false);
    assert.equal(m.map(s, 200), null);
  }
});

test("turning a partly open hand edge-on cannot masquerade as a pinch", () => {
  const original = hand(0.04),
    base = pinchSample(original),
    m = new PinchMapper(profile);
  m.anchor(base, [0, 0, 0], 0.5, [0, 0, 0]);
  let time = 0,
    reference;
  for (let i = 0; i < 80; i++) reference = m.map(base, (time += 33)).grip;
  for (let angle = 0; angle <= 1.45; angle += 0.025) {
    const c = Math.cos(angle),
      s = Math.sin(angle),
      r = structuredClone(original);
    r.worldLandmarks[0] = r.worldLandmarks[0].map((p) => ({
      x: c * p.x + s * p.z,
      y: p.y,
      z: -s * p.x + c * p.z,
    }));
    r.landmarks[0] = r.worldLandmarks[0].map((p) => ({
      x: 0.5 + p.x * 2,
      y: 0.45 + ((p.y * 640) / 480) * 2,
      z: p.z * 2,
    }));
    const sample = pinchSample(r);
    assert.ok(sample.valid);
    for (let i = 0; i < 3; i++)
      assert.ok(Math.abs(m.map(sample, (time += 33)).grip - reference) < 1e-8);
  }
});

test("reopening to a smaller gap restores the original point after an asymmetric grasp", () => {
  const neutral = pinchSample(hand(0.06)),
    m = new PinchMapper(profile),
    base = [0.1, 0.2, -0.3];
  m.anchor(neutral, base, 0.6, [0, 0.3, 0]);
  let time = 0;
  const shifted = (gap) => {
    const r = hand(gap);
    for (const i of [4, 8]) {
      r.worldLandmarks[0][i].x += 0.04;
      r.landmarks[0][i].x += 0.08;
    }
    return pinchSample(r);
  };
  for (let i = 0; i < 60; i++) m.map(shifted(0.09), (time += 33));
  for (let i = 0; i <= 90; i++)
    m.map(shifted(0.09 * (1 - i / 90)), (time += 33));
  for (let i = 0; i <= 90; i++)
    m.map(pinchSample(hand((0.06 * i) / 90)), (time += 33));
  let out;
  for (let i = 0; i < 60; i++) out = m.map(neutral, (time += 33));
  close(out.position, base, 1e-8);
  close(out.wrist, [0, 0.3, 0], 1e-8);
});
