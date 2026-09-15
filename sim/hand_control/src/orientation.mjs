// All angles are radians. The finger frame and its relative angles are the
// webapp's; the studio keeps symmetric limits and consumes blocked turn on
// pitch only.
import { WristMapper as BaseWristMapper } from "../../../webapp/js/handControl/orientation.js";

export {
  fingerFrame,
  relativeAngles,
} from "../../../webapp/js/handControl/orientation.js";
export const WRIST_LIMITS = [1.2, 0.65, 0.45];

export class WristMapper extends BaseWristMapper {
  limits = WRIST_LIMITS.map((l) => [-l, l]);
  consume = [1];
}
