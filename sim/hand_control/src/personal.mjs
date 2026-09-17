// Calibrated from locally labelled poses. The same features are used offline
// and live; raw recordings never need to be sent to the control page.
import { clamp, measureHand } from "./control.mjs";
import { WRIST_LIMITS } from "./orientation.mjs";

export const PERSONAL_WRIST_MAX = [
  WRIST_LIMITS[0],
  Math.PI / 2,
  WRIST_LIMITS[2],
];

const dot = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0);
const sub = (a, b) => a.map((v, i) => v - b[i]);
const norm = (a) => Math.hypot(...a);
const unit = (a) => a.map((v) => v / norm(a));
const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
export const multiply = (matrix, vector) =>
  matrix.map((row) => dot(row, vector));

export function thumbBaseFrame(points) {
  if (!points || points.length !== 21) return null;
  const p = points.map((p) => [p.z, p.x, p.y]);
  if (p.flat().some((v) => !Number.isFinite(v))) return null;
  const forward = sub(p[5], p[0]),
    base = sub(p[5], p[2]);
  if (norm(forward) < 1e-6) return null;
  const x = unit(forward),
    lateral = sub(
      base,
      x.map((v) => v * dot(base, x)),
    );
  if (norm(lateral) < norm(forward) * 0.08) return null;
  const y = unit(lateral),
    z = cross(x, y);
  return [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
}

export function rotationVector(current, reference) {
  const r = Array.from({ length: 9 }, (_, i) => {
    const row = Math.floor(i / 3),
      col = i % 3;
    return [0, 1, 2].reduce(
      (s, k) => s + current[row * 3 + k] * reference[col * 3 + k],
      0,
    );
  });
  const angle = Math.acos(clamp((r[0] + r[4] + r[8] - 1) / 2));
  // This calibrated workspace never asks for a half-turn. Holding avoids
  // the rotation-vector singularity rather than flipping a wrist at pi.
  if (angle > 2.7) return null;
  const factor = angle < 1e-6 ? 0.5 : angle / (2 * Math.sin(angle));
  return [r[7] - r[5], r[2] - r[6], r[3] - r[1]].map((v) => v * factor);
}

export function yawGeometry(current, reference) {
  const r = Array.from({ length: 9 }, (_, i) =>
    [0, 1, 2].reduce(
      (s, k) =>
        s + current[Math.floor(i / 3) * 3 + k] * reference[(i % 3) * 3 + k],
      0,
    ),
  );
  // Fused heading uses the whole thumb/index base frame. Unlike the projected
  // pointing direction, it remains defined when the fingers point downward.
  // Only an upside-down (180-degree tilt) frame has no observable heading.
  if (1 + r[8] < 0.02) return null;
  return { heading: -Math.atan2(r[3] - r[1], r[0] + r[4]), tilt: r.slice(6) };
}

export function yawFor(sample, profile) {
  const geometry = yawGeometry(sample.orientation, profile.reference);
  if (!geometry) return null;
  const model = profile.yaw;
  const features = [...geometry.tilt, ...(sample.yawShape || [0, 0])];
  // Tilt and base-triangle shape are invariant to camera-vertical rotation;
  // this correction therefore cannot suppress real yaw.
  const correction = model
    ? model.centers.reduce(
        (sum, center, i) =>
          sum +
          model.coefficients[i] *
            Math.exp(
              -features.reduce((d, v, k) => d + (v - center[k]) ** 2, 0) /
                (2 * model.width ** 2),
            ),
        0,
      )
    : 0;
  return Math.atan2(
    Math.sin(geometry.heading - correction),
    Math.cos(geometry.heading - correction),
  );
}

export function personalSample(result, width = 640, height = 480) {
  const sample = measureHand(result, width, height);
  const world = result.worldLandmarks?.[0];
  const orientation = thumbBaseFrame(world);
  if (!sample.valid || !sample.gripValid || !orientation)
    return {
      ...sample,
      valid: false,
      reason: sample.valid ? "Keep thumb and index in view" : sample.reason,
    };
  const p = world.map((p) => [p.x, p.y, p.z]);
  const forward = sub(p[5], p[0]),
    base = sub(p[5], p[2]);
  const yawShape = [
    norm(base) / norm(forward),
    dot(base, forward) / dot(forward, forward),
  ];
  const image = sample.raw.map((p) => [(p.x * width) / height, p.y]);
  const imagePalm = Math.sqrt(
    (norm(sub(image[0], image[9])) ** 2 + norm(sub(image[5], image[17])) ** 2) /
      2,
  );
  const palmDirection = sub(image[5], image[0]);
  const indexDirection = sub(image[8], image[5]);
  const palm = Math.sqrt(
    (norm(sub(p[0], p[9])) ** 2 + norm(sub(p[5], p[17])) ** 2) / 2,
  );
  if (!(palm > 1e-6) || imagePalm < 1e-6)
    return { ...sample, valid: false, reason: "Finding your hand" };
  return {
    ...sample,
    orientation,
    yawShape,
    aperture: norm(sub(p[4], p[8])) / palm,
    screenAperture: norm(sub(image[4], image[8])) / imagePalm,
    // Keep foreshortened image bones short; normalizing each to unit length
    // would turn tiny tracking errors into large orientation changes.
    imageAxes: [...palmDirection, ...indexDirection].map((v) => v / imagePalm),
    camera: [sample.x, sample.y, Math.log(sample.scale)],
  };
}

export function baseAnglesFor(sample, profile) {
  const vector = rotationVector(sample.orientation, profile.reference);
  if (!vector) return null;
  return multiply(profile.rotation.matrix, vector).map((v, i) => {
    const response = profile.rotation.response[i];
    return (
      Math.sign(v) *
      Math.max(0, Math.abs(v) - response.deadzone) *
      (v < 0 ? response.negative : response.positive)
    );
  });
}

export function floorFeatures(sample, profile) {
  const vector = rotationVector(sample.orientation, profile.reference);
  if (!vector || !sample.imageAxes || !Number.isFinite(sample.screenAperture))
    return null;
  return [...vector, ...sample.imageAxes, sample.screenAperture];
}

export function floorCorrection(sample, profile) {
  const model = profile.floor;
  const features = model && floorFeatures(sample, profile);
  if (!features) return [0, 0, 0, 0, 0];
  // Smooth local corrections fade outside the demonstrated hand shapes.
  // Features contain no absolute image position or hand size.
  const weights = model.centers.map((center) =>
    Math.exp(
      -features.reduce(
        (sum, v, i) => sum + ((v - center[i]) * model.scales[i]) ** 2,
        0,
      ) /
        (2 * model.width ** 2),
    ),
  );
  return [0, 1, 2, 3, 4].map((j) =>
    weights.reduce((sum, w, i) => sum + w * model.coefficients[i][j], 0),
  );
}

export function anglesFor(sample, profile) {
  const base = baseAnglesFor(sample, profile);
  if (!base) return null;
  const correction = floorCorrection(sample, profile);
  return base.map((v, i) => v + correction[i]);
}

export function shapeFeatures(angles, aperture, neutralAperture) {
  const [roll, pitch] = angles;
  return [
    Math.max(-roll, 0),
    Math.max(roll, 0),
    Math.max(-pitch, 0),
    Math.max(pitch, 0),
    aperture - neutralAperture,
  ];
}

export function correctedCamera(
  sample,
  profile,
  angles = baseAnglesFor(sample, profile),
) {
  if (!angles) return null;
  const correction = multiply(
    profile.position.compensation,
    shapeFeatures(angles, sample.aperture, profile.grip.knots[1]),
  );
  return sub(sample.camera, correction);
}

export function positionFor(sample, profile) {
  const camera = correctedCamera(sample, profile);
  if (!camera) return null;
  const correction = floorCorrection(sample, profile);
  return multiply(profile.position.matrix, camera).map(
    (v, i) => v + correction[i + 2],
  );
}

export function gripFor(aperture, profile, angles = [0, 0]) {
  if (profile.grip.rotationCompensation)
    aperture -= dot(
      profile.grip.rotationCompensation,
      shapeFeatures(angles, 0, 0).slice(0, 4),
    );
  const [closed, neutral, open] = profile.grip.knots;
  const middle = profile.grip.neutral;
  return clamp(
    aperture <= neutral
      ? (middle * (aperture - closed)) / (neutral - closed)
      : middle + ((1 - middle) * (aperture - neutral)) / (open - neutral),
    0,
    1,
  );
}

const interpolate = (x, xs, ys) => {
  if (x <= xs[0]) return ys[0];
  for (let i = 1; i < xs.length; i++) {
    if (x <= xs[i])
      return (
        ys[i - 1] +
        ((ys[i] - ys[i - 1]) * (x - xs[i - 1])) / (xs[i] - xs[i - 1])
      );
  }
  return ys.at(-1);
};

export function personalGrip(
  sample,
  profile,
  angles = anglesFor(sample, profile),
) {
  const base = gripFor(
    sample.aperture,
    profile,
    baseAnglesFor(sample, profile),
  );
  const knots = profile.floor?.grip;
  if (!knots || !angles || !Number.isFinite(sample.screenAperture)) return base;
  const pitch = angles[1];
  const [closed, neutral, opened] = ["closed", "neutral", "open"].map((key) =>
    interpolate(pitch, knots.pitch, knots[key]),
  );
  const gap = sample.screenAperture;
  const middle = profile.grip.neutral;
  const visible = clamp(
    gap <= neutral
      ? (middle * (gap - closed)) / (neutral - closed)
      : middle + ((1 - middle) * (gap - neutral)) / (opened - neutral),
    0,
    1,
  );
  const t = clamp((pitch - 0.15) / 0.5, 0, 1);
  // At a steep tilt, a hallucinated landmark depth must not reopen a visibly
  // closed thumb/index pinch. The existing grip response remains at level/up.
  const blend = t * t * (3 - 2 * t);
  return base + (visible - base) * blend;
}

export class PersonalMapper {
  constructor(profile) {
    this.profile = profile;
  }
  sensitivity = 1;
  anchor(sample, offset, grip, wrist = [0, 0, 0]) {
    const angles = anglesFor(sample, this.profile),
      position = positionFor(sample, this.profile),
      yaw = yawFor(sample, this.profile);
    if (!angles || !position || yaw === null) return false;
    this.origin = position;
    this.angleOrigin = angles;
    this.yawOrigin = yaw;
    this.base = [...offset];
    this.value = [...offset];
    this.wrist = [...wrist];
    this.wristBase = [...wrist];
    this.grip = grip;
    this.last = null;
    return true;
  }
  map(sample, now) {
    if (!this.origin || !sample.valid) return null;
    const p = this.profile,
      angles = anglesFor(sample, p),
      position = positionFor(sample, p),
      yaw = yawFor(sample, p);
    if (!angles || !position) return null;
    const dt =
      this.last === null ? 1 / 30 : clamp((now - this.last) / 1000, 0, 0.1);
    this.last = now;
    const delta = sub(position, this.origin); // robot x,y,z metres
    const wanted = [
      delta[1] / p.workspace.span[1],
      delta[2] / p.workspace.span[2],
      delta[0] / p.workspace.span[0],
    ];
    const alpha = 1 - Math.exp(-dt / 0.08);
    this.value = this.value.map(
      (v, i) =>
        v + (clamp(this.base[i] + wanted[i] * this.sensitivity) - v) * alpha,
    );
    this.wrist = this.wrist.map((v, i) => {
      if (i === 2 && yaw === null) return v;
      let delta =
        i === 2
          ? Math.atan2(
              Math.sin(yaw - this.yawOrigin),
              Math.cos(yaw - this.yawOrigin),
            )
          : angles[i] - this.angleOrigin[i];
      if (i === 2)
        delta = Math.sign(delta) * Math.max(0, Math.abs(delta) - 0.02);
      const target = clamp(
        this.wristBase[i] + delta,
        -WRIST_LIMITS[i],
        PERSONAL_WRIST_MAX[i],
      );
      // Keep the hand's neutral fixed even after reaching a wrist limit.
      return v + (target - v) * (1 - Math.exp(-dt / 0.08));
    });
    this.grip +=
      (personalGrip(sample, p, angles) - this.grip) *
      (1 - Math.exp(-dt / 0.055));
    return {
      position: [...this.value],
      wrist: [...this.wrist],
      grip: this.grip,
    };
  }
}
