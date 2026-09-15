import { clamp, measureHand } from "./control.mjs";
import { WRIST_LIMITS } from "./orientation.mjs";
import { FingertipRotation } from "./fingertip-rotation.mjs";

const sub = (a, b) => a.map((v, i) => v - b[i]);
const add = (a, b) => a.map((v, i) => v + b[i]);
const dot = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0);
const norm = (a) => Math.hypot(...a);
const unit = (a) => a.map((v) => v / Math.max(norm(a), 1e-9));
const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const frame = (x, y, z) => [
  x[0],
  y[0],
  z[0],
  x[1],
  y[1],
  z[1],
  x[2],
  y[2],
  z[2],
];
const mean = (points) =>
  points[0].map((_, i) => points.reduce((s, p) => s + p[i], 0) / points.length);

export function pinchSample(result, width = 640, height = 480) {
  const s = measureHand(result, width, height),
    world = result.worldLandmarks?.[0];
  if (!s.valid) return s;
  const unavailable = {
    ...s,
    valid: false,
    reason: "Keep your thumb and index clearly in view",
  };
  if (!s.gripValid || world?.length !== 21) return unavailable;
  const p = world.map((p) => [p.z, p.x, p.y]);
  if (p.flat().some((v) => !Number.isFinite(v))) return unavailable;
  const im = s.raw.map((p) => [p.x * width, p.y * height]);
  const palm = Math.sqrt(
    (norm(sub(p[0], p[9])) ** 2 + norm(sub(p[5], p[17])) ** 2) / 2,
  );
  const ipalm = Math.sqrt(
    (norm(sub(im[0], im[9])) ** 2 + norm(sub(im[5], im[17])) ** 2) / 2,
  );
  const d = unit(sub(p[6], p[5])),
    b = unit(sub(p[5], p[2])),
    n = unit(cross(d, b));
  if (palm < 1e-5 || ipalm < 1 || norm(cross(d, b)) < 0.08) return unavailable;
  const stable = frame(d, unit(cross(n, d)), n);
  const gap = sub(p[8], p[4]),
    jaw = unit(gap),
    approach = sub(
      d,
      jaw.map((v) => v * dot(d, jaw)),
    );
  const jawFrame = frame(unit(approach), jaw, unit(cross(approach, jaw)));
  // Two tips determine the jaw line, but not twist around it. The direction
  // from their bases to their midpoint supplies that third frame axis.
  const tipForward = sub(mean([p[4], p[8]]), mean([p[2], p[5]]));
  const tipApproach = sub(
    tipForward,
    jaw.map((v) => v * dot(tipForward, jaw)),
  );
  const palmForward = unit(sub(mean([p[5], p[9]]), p[0]));
  const palmNormal = cross(palmForward, sub(p[5], p[17]));
  const palmFrame =
    norm(palmNormal) > palm * 0.08
      ? frame(
          palmForward,
          unit(cross(palmNormal, palmForward)),
          unit(palmNormal),
        )
      : null;
  const palmImage = mean([0, 5, 9, 13, 17].map((i) => im[i]));
  const mid = mean([im[4], im[8]]);
  // Follow the visible pinch midpoint directly. Reconstructing its position
  // from monocular world-bone lengths made pitching change apparent depth and
  // shrink vertical travel. Palm scale supplies only the forward/back channel.
  const cameraPoint = (image) => [
    Math.log(s.scale),
    image[0] / width,
    -image[1] / height,
  ];
  const visible = norm(sub(im[8], im[4])) / ipalm;
  return {
    ...s,
    x: 1 - mid[0] / width,
    y: mid[1] / height,
    point: cameraPoint(mid),
    palmPoint: cameraPoint(palmImage),
    stableFeatures: [
      ...stable,
      ...sub(im[6], im[5]).map((v) => v / ipalm),
      ...sub(im[5], im[2]).map((v) => v / ipalm),
    ],
    features: [...jawFrame, ...stable],
    visible,
    aperture: norm(gap) / palm,
    tipFrame:
      visible > 0.38 &&
      norm(gap) / palm > 0.25 &&
      norm(tipApproach) / palm > 0.12
        ? frame(unit(tipApproach), jaw, unit(cross(tipApproach, jaw)))
        : null,
    palmFrame,
    jawReliable:
      visible > 0.38 && norm(gap) / palm > 0.25 && norm(approach) > 0.15,
  };
}

export function evaluatePinch(features, model) {
  const weights = model.centers.map((c) =>
    Math.exp(
      -c.reduce((s, v, i) => s + (features[i] - v) ** 2, 0) /
        (2 * model.width ** 2),
    ),
  );
  return [0, 1, 2].map((j) =>
    weights.reduce((s, w, i) => s + w * model.coefficients[i][j], 0),
  );
}

export class PinchMapper {
  constructor(profile) {
    this.profile = profile;
    this.sensitivity = 1;
    this.rotation =
      profile.pinch.rotation === "direct" ? new FingertipRotation() : null;
  }
  resetObservation() {
    this.lock = null;
    this.previous = null;
    this.history = [];
  }
  observe(sample) {
    const cfg = this.profile.pinch;
    const stable = this.rotation
      ? [0, 0, 0]
      : evaluatePinch(sample.stableFeatures, cfg.stable);
    const direct = this.rotation
      ? stable
      : sample.jawReliable
        ? evaluatePinch(sample.features, cfg.orientation)
        : stable;
    // Compare over several observations so slow pinches also enter the stable
    // frame. A per-frame threshold misses ordinary closing at webcam rates.
    const peak = Math.max(
      sample.visible,
      ...this.history.map((s) => s.visible),
    );
    const gapPeak = Math.max(
      sample.aperture,
      ...this.history.map((s) => s.aperture),
    );
    // Foreshortening alone is not a pinch: the 3D gap must also decrease.
    const closing =
      this.previous &&
      sample.visible < peak - 0.08 &&
      sample.aperture < gapPeak - 0.02;
    if (!this.lock && (closing || !sample.jawReliable)) {
      this.lock = {
        palm: [...sample.palmPoint],
        point: [...(this.previous?.point || sample.point)],
        stable,
        angles: [...(this.previous?.angles || direct)],
        low: sample.visible,
        open: peak,
        release: 0,
        closing: Boolean(
          closing ||
          (!this.previous && sample.visible < cfg.grip.visibleClosed),
        ),
      };
    }
    let point = sample.point,
      angles = direct;
    if (this.lock) {
      const l = this.lock;
      l.closing ||= Boolean(closing);
      point = add(l.point, sub(sample.palmPoint, l.palm));
      // Curling the index to close also changes its approach direction. Consume
      // that change during closure, then resume wrist tracking from the held
      // grasp frame. Otherwise a pinch steers the claw away from the object.
      if (!closing) l.angles = add(l.angles, sub(stable, l.stable));
      l.stable = stable;
      angles = l.angles;
      l.low = Math.min(l.low, sample.visible);
      l.release = sample.jawReliable && !closing ? l.release + 1 : 0;
      if (
        sample.jawReliable &&
        (sample.visible > l.low + 0.15 || l.release > 4)
      ) {
        // Rejoin the observed fingertip frame continuously on reopening. Never
        // accumulate a permanent offset: a complete cycle returns to its start.
        const opening = clamp(
          (sample.visible - l.low - 0.15) /
            Math.max(l.open - l.low - 0.15, 0.25),
          0,
          1,
        );
        // A user may reopen only halfway, or change their grasp. Once the
        // fingertips are observable and steady again, restore their absolute
        // frame instead of keeping an offset from an earlier wider opening.
        const blend = Math.max(opening, clamp((l.release - 4) / 8, 0, 1));
        const weight = blend * blend * (3 - 2 * blend);
        point = point.map((v, i) => v + (sample.point[i] - v) * weight);
        angles = angles.map((v, i) => v + (direct[i] - v) * weight);
        if (blend >= 1) this.lock = null;
      }
    }
    // Use a palm-normalized 3D gap so a rigid turn doesn't close the jaws.
    // During an observed pinch, the image wins near contact: monocular depth
    // often invents a remaining gap between visibly touching fingertips.
    let grip = clamp(
      (sample.aperture - cfg.grip.closed) / (cfg.grip.open - cfg.grip.closed),
      0,
      1,
    );
    if (this.lock?.closing) {
      grip *= clamp((sample.visible - cfg.grip.visibleClosed) / 0.4, 0, 1);
    }
    this.previous = { point, angles, visible: sample.visible };
    this.history.push({ visible: sample.visible, aperture: sample.aperture });
    if (this.history.length > 12) this.history.shift();
    // A twist around touching fingertips moves the palm, but not the grasp
    // point. Direct control must keep following the visible tip midpoint.
    return { point: this.rotation ? sample.point : point, angles, grip };
  }
  anchor(sample, offset, grip, wrist = [0, 0, 0], options) {
    if (!sample.valid) return false;
    if (this.rotation && !this.rotation.anchor(sample, wrist, options))
      return false;
    this.resetObservation();
    const value = this.observe(sample);
    this.origin = value.point;
    this.angleOrigin = value.angles;
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
    const q = this.observe(sample),
      p = this.profile;
    const dt =
      this.last === null ? 1 / 30 : clamp((now - this.last) / 1000, 0, 0.1);
    this.last = now;
    const delta = sub(q.point, this.origin).map(
      (v, i) => v * p.pinch.positionGain[i] * this.sensitivity,
    );
    const wanted = [
      delta[1] / p.workspace.span[1],
      delta[2] / p.workspace.span[2],
      delta[0] / p.workspace.span[0],
    ];
    const alpha = 1 - Math.exp(-dt / 0.065);
    this.value = this.value.map(
      (v, i) => v + (clamp(this.base[i] + wanted[i]) - v) * alpha,
    );
    this.wrist = this.rotation
      ? this.rotation.update(sample, dt)
      : this.wrist.map(
          (v, i) =>
            v +
            (clamp(
              this.wristBase[i] + q.angles[i] - this.angleOrigin[i],
              -WRIST_LIMITS[i],
              i === 1 ? Math.PI / 2 : WRIST_LIMITS[i],
            ) -
              v) *
              alpha,
        );
    this.grip += (q.grip - this.grip) * (1 - Math.exp(-dt / 0.045));
    return {
      position: [...this.value],
      wrist: [...this.wrist],
      grip: this.grip,
    };
  }
}
