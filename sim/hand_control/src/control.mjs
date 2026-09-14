import { fingerFrame } from "./orientation.mjs";
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
export const clamp = (v, lo = -1, hi = 1) => Math.max(lo, Math.min(hi, v));
const median = (v) => [...v].sort((a, b) => a - b)[Math.floor(v.length / 2)];
const PALM = [0, 5, 9, 13, 17];

export function measureHand(result, width = 640, height = 480) {
  if (result.landmarks.length !== 1)
    return {
      valid: false,
      reason: result.landmarks.length
        ? "Use one hand"
        : "Bring your hand back into view",
    };
  const raw = result.landmarks[0];
  if (
    raw.length !== 21 ||
    raw.some((p) => ![p.x, p.y, p.z].every(Number.isFinite))
  )
    return { valid: false, reason: "Finding your hand" };
  const p = raw.map((p) => ({
    x: p.x * width,
    y: p.y * height,
    z: p.z * width,
  }));
  const center = PALM.reduce(
    (a, i) => ({ x: a.x + raw[i].x / 5, y: a.y + raw[i].y / 5 }),
    { x: 0, y: 0 },
  );
  // Palm bones remain the same length when fingers close. Including relative
  // landmark depth reduces apparent reach changes when the palm turns sideways.
  const palmSize = Math.sqrt(
    (distance(p[0], p[9]) ** 2 + distance(p[5], p[17]) ** 2) / 2,
  );
  const scale = palmSize / width;
  // Only the thumb/index fingertip gap controls the jaws. Normalize by
  // palm size so moving toward the camera doesn't change the opening.
  const grip = clamp((distance(p[4], p[8]) / palmSize - 0.18) / 0.95, 0, 1);
  const inside = (p) => p.x > 0.01 && p.x < 0.99 && p.y > 0.01 && p.y < 0.99;
  const gripValid = [4, 8].every((i) => inside(raw[i]));
  const world = result.worldLandmarks?.[0];
  const orientationPoints =
    world?.length === 21 &&
    world.every((p) => [p.x, p.y, p.z].every(Number.isFinite))
      ? world
      : p;
  const orientation = gripValid ? fingerFrame(orientationPoints) : null;
  const side = result.handedness?.[0]?.[0]?.categoryName || "";
  const sample = {
    x: 1 - center.x,
    y: center.y,
    scale,
    side,
    raw,
    grip,
    gripValid,
    orientation,
  };
  if (scale < 0.035)
    return {
      ...sample,
      valid: false,
      reason: "Bring your hand a little closer",
    };
  if (!PALM.every((i) => inside(raw[i])))
    return { ...sample, valid: false, reason: "Keep your palm in the picture" };
  return { ...sample, valid: true, reason: "Hand tracked" };
}

export class Calibration {
  samples = [];
  constructor(duration = 800, minFrames = 8) {
    this.duration = duration;
    this.minFrames = minFrames;
  }
  reset() {
    this.samples = [];
  }
  update(sample, now) {
    if (!sample.valid) {
      this.reset();
      return { progress: 0, ready: false };
    }
    this.samples.push({ ...sample, now });
    this.samples = this.samples.filter(
      (s) => now - s.now <= this.duration + 400,
    );
    const center = {
      x: median(this.samples.map((s) => s.x)),
      y: median(this.samples.map((s) => s.y)),
      scale: median(this.samples.map((s) => s.scale)),
    };
    const stable = this.samples.every(
      (s) =>
        Math.abs(s.x - center.x) < 0.035 &&
        Math.abs(s.y - center.y) < 0.045 &&
        Math.abs(Math.log(s.scale / center.scale)) < 0.12,
    );
    if (!stable) {
      this.samples = [{ ...sample, now }];
      return { progress: 0, ready: false };
    }
    const duration = now - this.samples[0].now;
    return {
      progress: clamp(duration / this.duration, 0, 1),
      ready: duration >= this.duration && this.samples.length >= this.minFrames,
      neutral: center,
    };
  }
}

// The palm and the two control fingertips must fit above the lower image edge.
// Freeze these limits after calibration; raising, turning, or resuming must not
// silently move the ground reference.
export function visibleHeightRange(sample) {
  const points = [0, 4, 5, 8, 9, 13, 17].map((i) => sample.raw[i].y);
  const bottom = 0.96 - (Math.max(...points) - sample.y);
  const top = 0.04 + (sample.y - Math.min(...points));
  return { bottom, top, usable: bottom - top >= 0.18 };
}

export class GroundCalibration extends Calibration {
  bounds = null;
  reset() {
    super.reset();
    this.bounds = null;
  }
  update(sample, now) {
    if (!sample.valid) return super.update(sample, now);
    const bounds = visibleHeightRange(sample);
    this.bounds = bounds;
    if (!bounds.usable || sample.y < bounds.bottom - 0.04) {
      this.samples = [];
      return {
        ready: false,
        progress: 0,
        bounds,
        reason: bounds.usable
          ? "Lower your palm to the ground line"
          : "Move a little farther from the webcam",
      };
    }
    const result = super.update(
      { ...sample, floorY: bounds.bottom, ceilingY: bounds.top },
      now,
    );
    if (result.ready)
      result.bounds = {
        bottom: median(this.samples.map((s) => s.floorY)),
        top: median(this.samples.map((s) => s.ceilingY)),
        usable: true,
      };
    return {
      ...result,
      bounds: result.bounds || bounds,
      reason: "Hold here to set ground level",
    };
  }
}

export function continuousHand(previous, sample) {
  // Handedness classification can flip as a hand turns. Position/scale
  // continuity is a better guard against jumping to a newly detected hand.
  return (
    !previous ||
    (Math.hypot(sample.x - previous.x, sample.y - previous.y) < 0.22 &&
      Math.abs(Math.log(sample.scale / previous.scale)) < 0.55)
  );
}

export class HandMapper {
  neutral = null;
  value = [0, 0, 0];
  base = [0, 0, 0];
  grip = 1;
  last = null;
  sensitivity = 1;
  heightRange = null;
  setGround(bounds) {
    this.heightRange = { ...bounds };
  }
  vertical(sample) {
    const { bottom, top } = this.heightRange;
    // A short band at the bottom makes ground easy to hold. Sensitivity
    // changes the lift, never the zero point; below the line always stays zero.
    const lift = clamp(
      (bottom - sample.y - 0.04) / (bottom - top - 0.04),
      0,
      1,
    );
    return -1 + 2 * clamp(lift * this.sensitivity, 0, 1);
  }
  anchor(sample, offset, grip = this.grip) {
    this.neutral = { ...sample };
    this.base = [...offset];
    this.value = [...offset];
    this.grip = grip;
    this.last = null;
  }
  map(sample, now) {
    if (!this.neutral || !sample.valid) return null;
    const n = this.neutral;
    const deadzone = (value, dead) =>
      Math.sign(value) * Math.max(0, Math.abs(value) - dead);
    // Mirrored sideways, image height, then approach/retract. All three axes
    // work together; approaching the webcam now reaches the robot forward.
    const movement = [
      deadzone((sample.x - n.x) * 3.8, 0.025),
      deadzone((n.y - sample.y) * 4, 0.025),
      deadzone(Math.log(sample.scale / n.scale) / 0.5, 0.05),
    ];
    const wanted = movement.map((v, i) =>
      clamp(this.base[i] + v * this.sensitivity),
    );
    if (this.heightRange) wanted[1] = this.vertical(sample);
    const dt =
      this.last === null ? 1 / 30 : clamp((now - this.last) / 1000, 0, 0.1);
    this.last = now;
    const alpha = 1 - Math.exp(-dt / 0.07);
    this.value = this.value.map((v, i) => v + (wanted[i] - v) * alpha);
    if (sample.gripValid)
      this.grip += (sample.grip - this.grip) * (1 - Math.exp(-dt / 0.055));
    return { position: [...this.value], grip: this.grip };
  }
}
