// All angles are radians. The finger frame and its relative angles are the
// webapp's; the studio keeps symmetric limits and consumes blocked turn on
// pitch only.
import { relativeAngles } from "../../../webapp/js/handControl/orientation.js";

export {
  fingerFrame,
  relativeAngles,
} from "../../../webapp/js/handControl/orientation.js";
export const WRIST_LIMITS = [1.2, 0.65, 0.45];
const clamp = (v, low, high) => Math.max(low, Math.min(high, v));
const wrap = (angle) => Math.atan2(Math.sin(angle), Math.cos(angle));

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
