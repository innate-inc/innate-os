import test from "node:test";
import assert from "node:assert/strict";
import { Matrix4, Vector3 } from "three";
import { pinchSample, PinchMapper } from "../src/pinch.mjs";
import {
  FingertipRotation,
  alignJawRoll,
  canAlignJawRoll,
  frameQuaternion,
  wristQuaternion,
  continuousWrist,
} from "../src/fingertip-rotation.mjs";
import { hand } from "./hand-fixture.mjs";
const degrees = (v) => (v * Math.PI) / 180;
const matrix = (q) => {
  const e = new Matrix4().makeRotationFromQuaternion(q).elements;
  return [e[0], e[4], e[8], e[1], e[5], e[9], e[2], e[6], e[10]];
};
const base = pinchSample(hand()),
  frame = frameQuaternion(base.tipFrame);
function posed(angles, gap = 0.09, initial = [0, 0, 0]) {
  const r = hand(gap);
  // Rotate raw camera-space geometry about known camera axes, independent
  // of the hand-frame construction under test.
  const delta = wristQuaternion(angles).multiply(
      wristQuaternion(initial).invert(),
    ),
    points = r.worldLandmarks[0];
  const mid = new Vector3(
    (points[4].z + points[8].z) / 2,
    (points[4].x + points[8].x) / 2,
    (points[4].y + points[8].y) / 2,
  );
  r.worldLandmarks[0] = points.map((p) => {
    const v = new Vector3(p.z, p.x, p.y)
      .sub(mid)
      .applyQuaternion(delta)
      .add(mid);
    return { x: v.y, y: v.z, z: v.x };
  });
  r.landmarks[0] = r.worldLandmarks[0].map((p) => ({
    x: 0.5 + p.x * 2,
    y: 0.45 + ((p.y * 640) / 480) * 2,
    z: p.z * 2,
  }));
  const sample = pinchSample(r);
  assert.ok(sample.valid);
  return sample;
}
const close = (a, b, tolerance = 1e-7) =>
  a.forEach((v, i) =>
    assert.ok(Math.abs(v - b[i]) < tolerance, `${a} != ${b}`),
  );
const settle = (m, s) => {
  let out;
  for (let i = 0; i < 80; i++) out = m.update(s, 1 / 30);
  return out;
};
const profile = {
  workspace: { span: [0.045, 0.1, 0.13] },
  pinch: {
    rotation: "direct",
    positionGain: [0.08, 0.4, 0.4],
    grip: { closed: 0.18, open: 1, visibleClosed: 0.32 },
    orientation: {
      width: 1,
      centers: [base.features],
      coefficients: [[0, 0, 0]],
    },
    stable: {
      width: 1,
      centers: [base.stableFeatures],
      coefficients: [[0, 0, 0]],
    },
  },
};

test("calibration removes the quarter-turn offset by aligning the actual jaw line", () => {
  for (const roll of [-90, -60, -30, 0, 30, 60, 90]) {
    const sample = posed([degrees(roll), 0, 0]),
      m = new FingertipRotation();
    m.anchor(sample, [0, 0, 0], { alignRoll: true });
    const out = settle(m, sample),
      jaw = new Vector3(0, 1, 0).applyQuaternion(wristQuaternion(out)),
      fingers = new Vector3(0, 1, 0).applyQuaternion(
        frameQuaternion(sample.tipFrame),
      );
    assert.ok(Math.abs(jaw.dot(fingers)) > 1 - 1e-9);
    close(out.slice(1), [0, 0]);
  }
});

test("roll alignment preserves pitch and yaw and is not added again on recenter", () => {
  const wrist = [0.1, 0.5, -0.2],
    sample = posed([1.2, 0.3, -0.1]),
    aligned = alignJawRoll(sample, wrist);
  close(aligned.slice(1), wrist.slice(1));
  close(alignJawRoll(sample, aligned), aligned);
  const localJaw = new Vector3(0, 1, 0)
    .applyQuaternion(frameQuaternion(sample.tipFrame))
    .applyQuaternion(wristQuaternion(aligned).invert());
  assert.ok(Math.abs(localJaw.z) < 1e-9);
});

test("after a quarter-turn alignment, twists retain unit gain and the midpoint stays fixed", () => {
  const m = new PinchMapper(profile),
    initial = posed([degrees(90), 0, 0]);
  m.anchor(initial, [0, 0, 0], 1, [0, 0, 0], { alignRoll: true });
  let now = 0,
    out;
  for (const roll of [90, 60, 30, 0, -30]) {
    const sample = posed([degrees(roll), 0, 0]);
    for (let i = 0; i < 80; i++) out = m.map(sample, (now += 33));
    close(out.wrist, [degrees(roll), 0, 0]);
    close(out.position, [0, 0, 0]);
  }
  // Resume/reacquire anchors to the achieved wrist, without recalibrating roll.
  const resumed = posed([0.4, 0, 0]),
    held = [...out.wrist];
  m.anchor(resumed, out.position, out.grip, held);
  for (let i = 0; i < 80; i++) out = m.map(resumed, (now += 33));
  close(out.wrist, held);
});

test("unobservable jaw lines preserve the held roll during calibration", () => {
  const wrist = [0.2, 0.3, 0.4];
  close(alignJawRoll({ ...base, tipFrame: null }, wrist), wrist);
  // Jaw along the approach axis has no observable roll about that axis.
  close(alignJawRoll(posed([0, 0, -Math.PI / 2]), [0.2, 0, 0]), [0.2, 0, 0]);
  const closed = { ...base, tipFrame: null },
    m = new FingertipRotation();
  assert.equal(canAlignJawRoll(closed, wrist), false);
  assert.equal(m.anchor(closed, wrist, { alignRoll: true }), false);
  assert.equal(m.anchor(closed, wrist), true);
  close(settle(m, closed), wrist);
  assert.equal(canAlignJawRoll(base, wrist), true);
});

test("each fixed camera axis has unit gain regardless of the starting finger direction", () => {
  for (let axis = 0; axis < 3; axis++)
    for (const angle of [-60, -40, -20, 20, 40, 60]) {
      const m = new FingertipRotation();
      m.anchor(base, [0, 0, 0]);
      const target = [0, 0, 0];
      target[axis] = degrees(angle);
      close(settle(m, posed(target)), target);
    }
});

test("mixed rotations at a nonzero starting wrist preserve the entire rigid frame", () => {
  const initial = [0.3, 0.5, -0.2],
    m = new FingertipRotation();
  m.anchor(base, initial);
  for (const target of [
    [-0.4, 0.9, 0.5],
    [0.7, 1.2, -0.6],
    [0.1, -0.7, 0.4],
    initial,
  ]) {
    const out = settle(m, posed(target, 0.09, initial));
    assert.ok(wristQuaternion(out).angleTo(wristQuaternion(target)) < 1e-7);
  }
});

test("the jaw line follows the fingertips exactly while all palm and knuckle landmarks stay still", () => {
  const source = hand(),
    axis = new Vector3(
      ...[base.tipFrame[0], base.tipFrame[3], base.tipFrame[6]],
    );
  const mid = new Vector3(0, 0, -0.025),
    angle = degrees(25);
  for (const i of [4, 8]) {
    const p = source.worldLandmarks[0][i];
    const v = new Vector3(p.z, p.x, p.y)
      .sub(mid)
      .applyAxisAngle(axis, angle)
      .add(mid);
    source.worldLandmarks[0][i] = { x: v.y, y: v.z, z: v.x };
  }
  const m = new FingertipRotation();
  m.anchor(base, [0, 0, 0]);
  const current = pinchSample(source),
    out = settle(m, current);
  const expected = new Vector3(
    current.tipFrame[1],
    current.tipFrame[4],
    current.tipFrame[7],
  );
  const actual = new Vector3(0, 1, 0).applyQuaternion(wristQuaternion(out));
  assert.ok(actual.distanceTo(expected) < 1e-9);
  assert.ok(Math.abs(actual.angleTo(new Vector3(0, 1, 0)) - angle) < 1e-9);
});

test("pitch follows the tip midpoint relative to the bases, even with the proximal index fixed", () => {
  const source = hand(),
    axis = new Vector3(base.tipFrame[1], base.tipFrame[4], base.tipFrame[7]);
  const p = source.worldLandmarks[0],
    pivot = new Vector3(
      (p[2].z + p[5].z) / 2,
      (p[2].x + p[5].x) / 2,
      (p[2].y + p[5].y) / 2,
    );
  for (const i of [4, 8]) {
    const v = new Vector3(p[i].z, p[i].x, p[i].y)
      .sub(pivot)
      .applyAxisAngle(axis, 0.35)
      .add(pivot);
    p[i] = { x: v.y, y: v.z, z: v.x };
  }
  const m = new FingertipRotation();
  m.anchor(base, [0, 0, 0]);
  close(settle(m, pinchSample(source)), [0, 0.35, 0]);
});

test("closed tips use palm transport without losing one-to-one rotation or reopening continuity", () => {
  const m = new FingertipRotation();
  m.anchor(base, [0, 0, 0]);
  close(settle(m, posed([0, 0, 0], 0)), [0, 0, 0]);
  for (const target of [
    [0.3, 0.6, -0.3],
    [-0.4, 1.3, 0.35],
    [0, 0, 0],
  ]) {
    close(settle(m, posed(target, 0)), target);
    close(settle(m, posed(target, 0.09)), target);
  }
});

test("vertical pitch and boundary crossings do not flip roll or yaw", () => {
  const m = new FingertipRotation(),
    initial = [0.3, degrees(80), 0.2];
  m.anchor(base, initial);
  for (const pitch of [85, 89, 90, 91, 100, 89, 80]) {
    const target = [0.3, degrees(pitch), 0.2],
      f = wristQuaternion(target)
        .multiply(wristQuaternion(initial).invert())
        .multiply(frame);
    const sample = { ...base, tipFrame: matrix(f), palmFrame: matrix(f) };
    close(settle(m, sample), [0.3, degrees(Math.min(90, pitch)), 0.2], 2e-6);
  }
  const q = wristQuaternion([0.4, Math.PI / 2, 0.1]);
  close(
    continuousWrist(q, [0.4, Math.PI / 2 - 0.001, 0.1]),
    [0.4, Math.PI / 2, 0.1],
    2e-6,
  );
});

test("rotation stays live during pinching without palm motion shifting the visible grasp midpoint", () => {
  const direct = new PinchMapper(profile),
    old = new PinchMapper({
      ...profile,
      pinch: { ...profile.pinch, rotation: undefined },
    });
  direct.anchor(base, [0, 0, 0], 1, [0, 0, 0]);
  old.anchor(base, [0, 0, 0], 1, [0, 0, 0]);
  let now = 0,
    out;
  for (let i = 0; i <= 60; i++) {
    const s = posed(
      [(degrees(20) * i) / 60, (degrees(15) * i) / 60, (degrees(-10) * i) / 60],
      0.09 - (0.05 * i) / 60,
    );
    out = direct.map(s, (now += 33));
    const prior = old.map(s, now);
    close(out.position, [0, 0, 0]);
    close([out.grip], [prior.grip]);
  }
  const s = posed([degrees(20), degrees(15), degrees(-10)], 0.04);
  for (let i = 0; i < 80; i++) out = direct.map(s, (now += 33));
  close(out.wrist, [degrees(20), degrees(15), degrees(-10)]);
  direct.sensitivity = 2;
  for (let i = 0; i < 80; i++) out = direct.map(s, (now += 33));
  close(out.wrist, [degrees(20), degrees(15), degrees(-10)]);
  direct.anchor(s, out.position, out.grip, out.wrist);
  close(direct.map(s, now + 33).wrist, out.wrist);
});

test("opposite thumb/index ordering cannot invert pitch and yaw", () => {
  const swapped = (s) => {
    const t = [...s.tipFrame];
    for (let i = 0; i < 3; i++) {
      t[3 * i + 1] *= -1;
      t[3 * i + 2] *= -1;
    }
    return { ...s, tipFrame: t };
  };
  const m = new FingertipRotation();
  m.anchor(swapped(base), [0, 0, 0]);
  for (const target of [
    [0, 0.4, 0],
    [0, 0, -0.5],
    [0.3, 0.4, -0.2],
  ])
    close(settle(m, swapped(posed(target))), target);
});

test("a closed-hand reanchor can reveal its fingertip line without snapping the claw", () => {
  const m = new FingertipRotation(),
    initial = [0.2, 0.4, -0.3];
  m.anchor(posed([0, 0, 0], 0), initial);
  close(settle(m, base), initial);
  const next = [0.2, 0.25, -0.3],
    handDelta = wristQuaternion(next).multiply(
      wristQuaternion(initial).invert(),
    );
  const newFrame = handDelta.multiply(frame);
  close(settle(m, { ...base, tipFrame: matrix(newFrame) }), next);
});

test("collapsed or nonfinite rotation frames hold safely", () => {
  for (const f of [
    null,
    Array(9).fill(0),
    Array(9).fill(NaN),
    [1, 0, 0, 0, 1, 0, 0, 0, -1],
  ])
    assert.equal(frameQuaternion(f), null);
  const m = new FingertipRotation();
  m.anchor(base, [0.1, 0.2, 0.3]);
  close(
    m.update({ ...base, palmFrame: null, tipFrame: null }, 0.033),
    [0.1, 0.2, 0.3],
  );
});
