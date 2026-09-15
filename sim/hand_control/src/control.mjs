// The hand measurement and the hold/continuity guards ship in the robot's
// webapp; the studio adds only its ground-line calibration and vertical mode.
import {
  Calibration,
  clamp,
} from "../../../webapp/js/handControl/handSample.js";

export {
  measureHand,
  clamp,
  Calibration,
  continuousHand,
} from "../../../webapp/js/handControl/handSample.js";
const median = (v) => [...v].sort((a, b) => a - b)[Math.floor(v.length / 2)];

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
