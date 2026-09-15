import { Euler, Matrix4, Quaternion, Vector3 } from "three";

export const DIRECT_WRIST_LIMITS = [Math.PI / 2, Math.PI / 2, Math.PI / 2];
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const near = (v, reference) =>
  reference + Math.atan2(Math.sin(v - reference), Math.cos(v - reference));

export function frameQuaternion(frame) {
  if (!frame || frame.length !== 9 || !frame.every(Number.isFinite))
    return null;
  // Only accept a right-handed orthonormal frame, never a collapsed gap.
  for (let a = 0; a < 3; a++)
    for (let b = 0; b < 3; b++) {
      const product = [0, 1, 2].reduce(
        (s, i) => s + frame[3 * i + a] * frame[3 * i + b],
        0,
      );
      if (Math.abs(product - (a === b ? 1 : 0)) > 0.01) return null;
    }
  const m = new Matrix4().set(
    frame[0],
    frame[1],
    frame[2],
    0,
    frame[3],
    frame[4],
    frame[5],
    0,
    frame[6],
    frame[7],
    frame[8],
    0,
    0,
    0,
    0,
    1,
  );
  if (m.determinant() < 0.99) return null;
  return new Quaternion().setFromRotationMatrix(m).normalize();
}

export const wristQuaternion = ([roll, pitch, yaw]) =>
  new Quaternion().setFromEuler(new Euler(roll, pitch, yaw, "ZYX"));

function projectedJaw(sample, wrist) {
  const tip = frameQuaternion(sample.tipFrame);
  if (!tip) return null;
  // The URDF jaws close along local Y. Project the observed thumb-index
  // line into the claw's YZ plane, keeping its existing pitch and yaw.
  const jaw = new Vector3(0, 1, 0)
    .applyQuaternion(tip)
    .applyQuaternion(wristQuaternion([0, wrist[1], wrist[2]]).invert());
  return Math.hypot(jaw.y, jaw.z) < 0.2 ? null : jaw;
}

export const canAlignJawRoll = (sample, wrist) =>
  projectedJaw(sample, wrist) !== null;

export function alignJawRoll(sample, wrist) {
  const jaw = projectedJaw(sample, wrist);
  if (!jaw) return [...wrist];
  const angle = Math.atan2(jaw.z, jaw.y);
  // The two jaws are interchangeable. Choose a physically reachable roll;
  // never wrap across the joint stops while the hand is moving.
  const candidates = [angle, angle - Math.PI, angle + Math.PI]
    .filter((v) => Math.abs(v) <= DIRECT_WRIST_LIMITS[0] + 1e-9)
    .sort((a, b) => {
      const difference = Math.abs(a - wrist[0]) - Math.abs(b - wrist[0]);
      return Math.abs(difference) < 1e-9 ? 0 : difference;
    });
  return [
    clamp(candidates[0], -DIRECT_WRIST_LIMITS[0], DIRECT_WRIST_LIMITS[0]),
    wrist[1],
    wrist[2],
  ];
}

export function continuousWrist(q, previous) {
  const e = new Euler().setFromQuaternion(q, "ZYX");
  const candidates = [
    [e.x, e.y, e.z],
    [e.x + Math.PI, Math.PI - e.y, e.z + Math.PI],
  ];
  // At vertical pitch, roll and yaw share an axis. Keep the existing roll
  // rather than making two servos jump to another equivalent representation.
  if (Math.abs(Math.sin(e.y)) >= 0.9999999) {
    candidates.push([previous[0], e.y, e.z + Math.sign(e.y) * previous[0]]);
  }
  return candidates
    .map((a) => a.map((v, i) => near(v, previous[i])))
    .sort(
      (a, b) =>
        a.reduce((s, v, i) => s + (v - previous[i]) ** 2, 0) -
        b.reduce((s, v, i) => s + (v - previous[i]) ** 2, 0),
    )[0];
}

/** One rigid alignment at reanchor; no learned angular gains or pose snapping. */
export class FingertipRotation {
  anchor(sample, wrist, { alignRoll = false } = {}) {
    if (alignRoll && !canAlignJawRoll(sample, wrist)) return false;
    const support = frameQuaternion(sample.palmFrame);
    const observed = (frameQuaternion(sample.tipFrame) || support)?.clone();
    if (!observed || !support) return false;
    this.referenceInverse = observed.clone().invert();
    const aligned = alignRoll ? alignJawRoll(sample, wrist) : wrist;
    this.base = wristQuaternion(aligned);
    this.observed = observed;
    this.support = support;
    this.value = wristQuaternion(wrist);
    this.angles = [...wrist];
    this.unbounded = [...aligned];
    this.recovering = !sample.tipFrame;
    this.needsTipReference = !sample.tipFrame;
    this.limited = false;
    return true;
  }

  update(sample, dt) {
    if (!this.referenceInverse) return null;
    const support = frameQuaternion(sample.palmFrame);
    const tip = frameQuaternion(sample.tipFrame);
    if (!support) return [...this.angles];
    // A zero-length fingertip line has no direction. Transport the last
    // observed frame with the rigid palm until the line is visible again.
    let observed = support
      .clone()
      .multiply(this.support.clone().invert())
      .multiply(this.observed);
    if (tip && this.needsTipReference) {
      // Opening after a closed-hand reanchor reveals an axis we could not
      // observe before. Align it once without moving the held claw.
      const held = observed
        .clone()
        .multiply(this.referenceInverse)
        .multiply(this.base);
      this.referenceInverse = tip
        .clone()
        .invert()
        .multiply(held)
        .multiply(this.base.clone().invert());
      observed = tip;
      this.needsTipReference = false;
      this.recovering = false;
    }
    if (tip) {
      if (this.recovering) {
        observed.slerp(tip, 1 - Math.exp(-dt / 0.08));
        if (observed.angleTo(tip) < 0.005) this.recovering = false;
      } else observed = tip;
    } else this.recovering = true;
    this.observed = observed;
    this.support = support;

    // Express the delta in fixed camera/robot axes, not in the initial hand's
    // local axes. F * F0^-1 maps a screen twist to robot roll even when the
    // user's fingers initially point up. F0^-1 * F incorrectly made it yaw.
    const target = observed
      .clone()
      .multiply(this.referenceInverse)
      .multiply(this.base);
    this.unbounded = continuousWrist(target, this.unbounded);
    const bounded = this.unbounded.map((v, i) =>
      clamp(v, -DIRECT_WRIST_LIMITS[i], DIRECT_WRIST_LIMITS[i]),
    );
    this.limited = bounded.some(
      (v, i) => Math.abs(v - this.unbounded[i]) > 1e-6,
    );
    // Smooth the rigid rotation, not its three Euler components. Filtering
    // changes response time, never the final angle or relative axis gain.
    this.value.slerp(wristQuaternion(bounded), 1 - Math.exp(-dt / 0.045));
    this.angles = continuousWrist(this.value, this.angles).map((v, i) =>
      clamp(v, -DIRECT_WRIST_LIMITS[i], DIRECT_WRIST_LIMITS[i]),
    );
    this.value.copy(wristQuaternion(this.angles));
    return [...this.angles];
  }
}
