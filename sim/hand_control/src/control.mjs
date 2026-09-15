// The hand measurement and the hold/continuity guards ship in the robot's
// webapp; the studio adds only its ground-line calibration and vertical mode.
import {
  Calibration,
  HandMapper as BaseHandMapper,
  clamp,
} from "../../../webapp/js/handControl/handSample.js";
import { median } from "../../../webapp/js/handControl/math.js";

export {
  measureHand,
  clamp,
  Calibration,
  continuousHand,
} from "../../../webapp/js/handControl/handSample.js";

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

export class HandMapper extends BaseHandMapper {
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
  wanted(sample) {
    const wanted = super.wanted(sample);
    if (this.heightRange) wanted[1] = this.vertical(sample);
    return wanted;
  }
}
