// All angles are radians. The camera-axis permutation maps screen twist to
// wrist roll, hand tilt to pitch, and turning sideways to the base swivel.
export const WRIST_LIMITS = [1.2, 0.65, 0.45];
const sub = (a, b) => a.map((v, i) => v - b[i]);
const dot = (a, b) => a.reduce((sum, v, i) => sum + v * b[i], 0);
const scale = (a, s) => a.map((v) => v * s);
const length = (a) => Math.hypot(...a);
const unit = (a) => scale(a, 1 / length(a));
const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const project = (a, axis) => sub(a, scale(axis, dot(a, axis)));
const clamp = (v, low, high) => Math.max(low, Math.min(high, v));
const wrap = (angle) => Math.atan2(Math.sin(angle), Math.cos(angle));

export function fingerFrame(points) {
  // Three non-collinear landmarks are necessary for a 3D frame. The wrist
  // and thumb/index bases stabilize it when the fingertips meet in a pinch.
  const p = points.map((p) => [p.z, p.x, p.y]);
  const palmForward = sub(p[5], p[0]);
  const palm = length(palmForward);
  if (palm < 1e-6) return null;
  const palmAxis = unit(palmForward);
  const base = project(sub(p[5], p[2]), palmAxis);
  if (length(base) < palm * 0.08) return null;
  let gap = project(sub(p[8], p[4]), palmAxis);
  const gapLength = length(gap);
  const baseAxis = unit(base);
  let y = baseAxis;
  if (gapLength > palm * 0.05) {
    gap = unit(gap);
    if (dot(gap, baseAxis) < 0) gap = scale(gap, -1);
    const weight = clamp((gapLength / palm - 0.05) / 0.35, 0, 1);
    y = unit(baseAxis.map((v, i) => v * (1 - weight) + gap[i] * weight));
  }
  // The claw points from the finger bases toward the midpoint of the two
  // tips. Removing the jaw-axis component makes opening the pinch lateral
  // to that axis leave the pointing direction unchanged. Palm-only forward
  // ignored a finger tilt when the wrist/MCP landmarks stayed in place.
  const fingerForward = p[8].map(
    (v, i) => (v + p[4][i] - p[5][i] - p[2][i]) / 2,
  );
  const pointing = project(fingerForward, y);
  if (length(pointing) < palm * 0.12) return null;
  const x = unit(pointing);
  const z = unit(cross(x, y));
  y = cross(z, x);
  return [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2]];
}

export function relativeAngles(current, reference) {
  const r = Array.from({ length: 9 }, (_, i) => {
    const row = Math.floor(i / 3),
      column = i % 3;
    return [0, 1, 2].reduce(
      (s, k) => s + current[row * 3 + k] * reference[column * 3 + k],
      0,
    );
  });
  return [
    Math.atan2(r[7], r[8]),
    Math.asin(clamp(-r[6], -1, 1)),
    Math.atan2(r[3], r[0]),
  ];
}

export class WristMapper {
  reference = null;
  base = [0, 0, 0];
  value = [0, 0, 0];
  unwrapped = [0, 0, 0];
  last = null;
  anchor(frame, wrist = [0, 0, 0]) {
    this.reference = frame;
    this.base = [...wrist];
    this.value = [...wrist];
    this.unwrapped = [0, 0, 0];
    this.last = null;
  }
  update(frame, now) {
    if (!frame) return [...this.value];
    if (!this.reference) this.reference = frame;
    const relative = relativeAngles(frame, this.reference);
    const dt =
      this.last === null ? 1 / 30 : clamp((now - this.last) / 1000, 0, 0.1);
    this.last = now;
    const alpha = 1 - Math.exp(-dt / 0.1);
    this.value = relative.map((angle, i) => {
      this.unwrapped[i] += wrap(angle - this.unwrapped[i]);
      const delta =
        Math.sign(this.unwrapped[i]) *
        Math.max(0, Math.abs(this.unwrapped[i]) - 0.025);
      const wanted = clamp(
        this.base[i] + delta,
        -WRIST_LIMITS[i],
        WRIST_LIMITS[i],
      );
      // Pitch follows angle changes. Once its input limit is reached, discard
      // excess motion so a small reversal responds immediately.
      if (i === 1 && Math.abs(this.base[i] + delta) > WRIST_LIMITS[i])
        this.base[i] = wanted - delta;
      return this.value[i] + (wanted - this.value[i]) * alpha;
    });
    return [...this.value];
  }
}
