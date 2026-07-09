// Why is the follower doing that? -- derived MPPI decision state for the
// ?navdebug HUD.
//
// MPPI publishes its command and its tracked path, but never the per-critic
// costs behind them. What it *does* expose is everything those critics are
// gated on: distance to goal, heading error to the path, and the local
// costmap. Each critic switches on/off at a fixed distance from the goal, so
// knowing the distance tells you which objectives are actually steering the
// robot at this instant -- usually more informative than the raw cost would be.
//
// The thresholds below MIRROR mars_nav/config/controller.yaml (InnateFollowPath).
// They are duplicated, not imported: the viewer is a separately built bundle
// with no access to the ROS param server. Keep them in sync by hand.

/** Distance-to-goal (m) under which the path critics stop being applied
 * (PathAlign/PathFollow/PathAngle `threshold_to_consider`). */
const PATH_CRITICS_OFF_WITHIN = 0.6;
/** Distance-to-goal (m) under which GoalCritic starts being applied. */
const GOAL_CRITIC_ON_WITHIN = 0.6;
/** Distance-to-goal (m) under which GoalAngleCritic starts (yaw settle). */
const GOAL_ANGLE_ON_WITHIN = 0.3;
/** Distance-to-goal (m) under which PreferForwardCritic stops being applied. */
const PREFER_FORWARD_OFF_WITHIN = 0.5;
/** PathAngleCritic only penalizes once heading error exceeds this (rad). */
const PATH_ANGLE_MAX_ANGLE = 1.0;
/** PathAlignCritic disables itself when more of the path than this is occupied. */
const PATH_ALIGN_MAX_OCCUPANCY = 0.05;
/** PathAngleCritic's carrot: this many path points ahead of the closest one. */
const PATH_ANGLE_OFFSET = 4;

/** Command magnitudes below which the controller is effectively commanding
 * nothing (matches the arrow overlay's deadband in scene.ts). */
export const STALL_VX = 0.033;
export const STALL_WZ = 0.057;

// nav2 costmap cost semantics.
const COST_INSCRIBED = 253; // >= this: the robot's inscribed circle collides
const COST_LETHAL = 254;
const COST_UNKNOWN = 255;

export interface Costmap {
  resolution: number;
  sizeX: number;
  sizeY: number;
  originX: number;
  originY: number;
  data: Uint8Array;
}

export interface Pose {
  x: number;
  y: number;
  yaw: number;
}

/** Raw cost (0-255) at a world point, or null when outside the costmap. */
export function costAt(cm: Costmap, x: number, y: number): number | null {
  const col = Math.floor((x - cm.originX) / cm.resolution);
  const row = Math.floor((y - cm.originY) / cm.resolution);
  if (col < 0 || row < 0 || col >= cm.sizeX || row >= cm.sizeY) return null;
  return cm.data[row * cm.sizeX + col] ?? null;
}

/** Human label for a raw cost value. */
export function costLabel(cost: number | null): string {
  if (cost === null) return "off-map";
  if (cost === COST_UNKNOWN) return "unknown";
  if (cost >= COST_LETHAL) return "LETHAL";
  if (cost >= COST_INSCRIBED) return "INSCRIBED";
  if (cost > 0) return "inflated";
  return "free";
}

/** Fraction of the path's points sitting in inscribed-or-worse cells. This is
 * what PathAlignCritic tests against max_path_occupancy_ratio before giving up
 * on aligning the robot to a path that runs through obstacles. */
export function pathOccupancyRatio(cm: Costmap, path: Float32Array): number {
  const n = path.length / 2;
  if (n === 0) return 0;
  let blocked = 0;
  for (let i = 0; i < n; i++) {
    const cost = costAt(cm, path[i * 2], path[i * 2 + 1]);
    if (cost !== null && cost >= COST_INSCRIBED && cost !== COST_UNKNOWN) blocked++;
  }
  return blocked / n;
}

/** Worst cost encountered along the first `meters` of the path -- the thing
 * that makes every forward rollout expensive when the corridor is tight. */
export function maxCostAhead(cm: Costmap, path: Float32Array, meters: number): number | null {
  const n = path.length / 2;
  let travelled = 0;
  let worst: number | null = null;
  for (let i = 0; i < n && travelled <= meters; i++) {
    if (i > 0) {
      travelled += Math.hypot(path[i * 2] - path[(i - 1) * 2], path[i * 2 + 1] - path[(i - 1) * 2 + 1]);
    }
    const cost = costAt(cm, path[i * 2], path[i * 2 + 1]);
    if (cost !== null && cost !== COST_UNKNOWN && (worst === null || cost > worst)) worst = cost;
  }
  return worst;
}

/** Signed heading error (rad) from the robot to PathAngleCritic's carrot point:
 * the angle it must turn through to face the path. A diff-drive can only fix
 * this by rotating, which is why a straight path can still produce pure spin. */
export function headingErrorToCarrot(pose: Pose, path: Float32Array): number | null {
  const n = path.length / 2;
  if (n === 0) return null;
  // The tracked path is already pruned to start near the robot, so the carrot
  // is simply a fixed offset along it.
  const i = Math.min(PATH_ANGLE_OFFSET, n - 1);
  const dx = path[i * 2] - pose.x;
  const dy = path[i * 2 + 1] - pose.y;
  if (Math.hypot(dx, dy) < 1e-4) return null;
  const bearing = Math.atan2(dy, dx);
  return Math.atan2(Math.sin(bearing - pose.yaw), Math.cos(bearing - pose.yaw));
}

export interface CriticState {
  name: string;
  active: boolean;
  /** Why it's on or off, in the reader's terms. */
  note: string;
}

/** Which MPPI objectives are actually steering the robot right now, given how
 * far it is from the goal and whether the path is passable. */
export function criticStates(
  distToGoal: number | null,
  headingErr: number | null,
  occupancy: number | null,
): CriticState[] {
  const d = distToGoal;
  const near = (limit: number) => (d === null ? null : d < limit);
  const pathCriticsOn = near(PATH_CRITICS_OFF_WITHIN) === false;
  const alignKilled = occupancy !== null && occupancy > PATH_ALIGN_MAX_OCCUPANCY;

  const unknown = (name: string): CriticState => ({ name, active: false, note: "no goal" });
  if (d === null) return ["PathFollow", "PathAlign", "PathAngle", "Goal", "GoalAngle", "PreferFwd"].map(unknown);

  return [
    {
      name: "PathFollow",
      active: pathCriticsOn,
      note: pathCriticsOn ? "pulling along path" : `off: within ${PATH_CRITICS_OFF_WITHIN}m of goal`,
    },
    {
      name: "PathAlign",
      active: pathCriticsOn && !alignKilled,
      note: alignKilled
        ? `off: path ${(occupancy! * 100).toFixed(0)}% blocked (>${PATH_ALIGN_MAX_OCCUPANCY * 100}%)`
        : pathCriticsOn
          ? "holding robot on the line"
          : `off: within ${PATH_CRITICS_OFF_WITHIN}m of goal`,
    },
    {
      name: "PathAngle",
      active: pathCriticsOn && headingErr !== null && Math.abs(headingErr) > PATH_ANGLE_MAX_ANGLE,
      note:
        !pathCriticsOn
          ? `off: within ${PATH_CRITICS_OFF_WITHIN}m of goal`
          : headingErr === null
            ? "no path"
            : Math.abs(headingErr) > PATH_ANGLE_MAX_ANGLE
              ? `FORCING TURN: heading err ${((headingErr * 180) / Math.PI).toFixed(0)}deg > ${((PATH_ANGLE_MAX_ANGLE * 180) / Math.PI).toFixed(0)}deg`
              : `dormant: heading err ${((headingErr * 180) / Math.PI).toFixed(0)}deg`,
    },
    {
      name: "Goal",
      active: d < GOAL_CRITIC_ON_WITHIN,
      note: d < GOAL_CRITIC_ON_WITHIN ? "driving to goal point" : `off: >${GOAL_CRITIC_ON_WITHIN}m away`,
    },
    {
      name: "GoalAngle",
      active: d < GOAL_ANGLE_ON_WITHIN,
      note: d < GOAL_ANGLE_ON_WITHIN ? "SETTLING YAW (rotates in place)" : `off: >${GOAL_ANGLE_ON_WITHIN}m away`,
    },
    {
      name: "PreferFwd",
      active: d > PREFER_FORWARD_OFF_WITHIN,
      note: d > PREFER_FORWARD_OFF_WITHIN ? "penalizing reverse" : `off: within ${PREFER_FORWARD_OFF_WITHIN}m of goal`,
    },
  ];
}

/** Tracks recent yaw-rate sign changes: a controller wedged between equally
 * bad options flips wz every few frames instead of committing. */
export class OscillationDetector {
  #signs: number[] = [];
  #lastSign = 0;
  #flips = 0;

  push(wz: number): void {
    const sign = Math.abs(wz) < STALL_WZ ? 0 : Math.sign(wz);
    if (sign !== 0 && this.#lastSign !== 0 && sign !== this.#lastSign) this.#flips++;
    if (sign !== 0) this.#lastSign = sign;
    this.#signs.push(this.#flips);
    if (this.#signs.length > 40) this.#signs.shift(); // ~4s at 10Hz
  }

  /** Sign changes observed across the retained window. */
  get flipsInWindow(): number {
    if (this.#signs.length < 2) return 0;
    return this.#signs[this.#signs.length - 1] - this.#signs[0];
  }
}
