// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Holds the leader arm inside the follower's reachable envelope.
//
// The leader turns freely; the follower's joint_1 stops at ±90° where the arm
// meets the body. When a guarded joint leaves the follower's band this drives
// the leader servo back to the boundary under a capped current — a soft wall
// the operator feels and can always overpower, rather than a trap.
//
// Every servo the guard energizes draws from the host's USB rail, so the sum of
// the goal currents it hands out never exceeds the operator's budget, and a
// watchdog on the servos' own Present Current walks that back if the real draw
// disagrees with the allocation.

import {
  ARM_COMMAND_STATE_TOPIC,
  ARM_CONSTRAINT_TOPIC,
  ARM_GET_PARAMETERS_SERVICE,
  ARM_POSITION_LIMITS_PARAMS,
  DIVERGENCE_DEADBAND_TICKS,
  DIVERGENCE_FLOOR_MA,
  DIVERGENCE_MA_PER_TICK,
  CONSTRAINT_NONE,
  DIVERGENCE_STALE_MS,
  J1_FRONT_ARC_HI,
  J1_FRONT_ARC_LO,
  J1_RAMP_HI,
  J1_RAMP_LO,
  J2_RESTRICTED_MIN_RAD,
  JOINT_DIRECTION_FLIPPED,
  JOINT_GUARD_ENABLED,
  LEADER_CURRENT_CEILING_MA,
  PARAMETER_DOUBLE_ARRAY,
} from "./constants.js";
import { OPERATING_MODE_CURRENT_POSITION } from "./dynamixel.js";
import { readBudget, onBudgetChange } from "./leaderBudget.js";
import {
  allocateCurrent,
  clampTick,
  divergenceCurrent,
  isOutside,
  joint2FloorRad,
  limitsToBand,
  radToTick,
  tickToRad,
  totalCurrent,
} from "./leaderLimits.js";

const J2_SHAPE = {
  restrictedMin: J2_RESTRICTED_MIN_RAD,
  arcLo: J1_FRONT_ARC_LO,
  arcHi: J1_FRONT_ARC_HI,
  rampLo: J1_RAMP_LO,
  rampHi: J1_RAMP_HI,
};

// Re-entry margin, ~3.5°. A joint resting exactly on the limit would otherwise
// toggle the wall every round.
const HYSTERESIS_TICKS = 40;
// Rounds over budget before the allocation is walked back. One round can spike
// on a direction change without the average being over.
const OVER_BUDGET_ROUNDS = 3;
const BACKOFF = 0.8;
// Below this a hold is too weak to be felt; release instead of pretending.
const MIN_USEFUL_MA = 40;

const DIVERGENCE_SHAPE = {
  deadband: DIVERGENCE_DEADBAND_TICKS,
  maPerTick: DIVERGENCE_MA_PER_TICK,
  floorMa: DIVERGENCE_FLOOR_MA,
  maxMa: LEADER_CURRENT_CEILING_MA,
};

/**
 * @typedef {Object} GuardDeps
 * @property {import("./dynamixel.js").DynamixelLeader} leader
 * @property {import("./rosClient.js").RosClient} rosClient
 */

export class LeaderGuard {
  /** @type {import("./dynamixel.js").DynamixelLeader} */ #leader;
  /** @type {import("./rosClient.js").RosClient} */ #rosClient;
  /** @type {number[]} */ #ids;
  /** @type {Map<number, import("./leaderLimits.js").Band>} */ #bands = new Map();
  /** @type {Map<number, import("./leaderLimits.js").Band>} */ #effective = new Map();
  /** @type {Set<number>} */ #holding = new Set();
  /** @type {Set<(state: LeaderGuardState) => void>} */ #listeners = new Set();
  /** @type {LeaderGuardState} */ #state = {
    armed: false,
    holding: [],
    drawMa: 0,
    allocatedMa: 0,
    divergedJoint: 0,
    divergedTicks: 0,
    error: null,
  };
  #enabled = true;
  #modeReady = false;
  #overRounds = 0;
  // Latched after a power trip. The joint that caused it is still out of band,
  // so without this the next round re-holds and trips again, forever.
  #tripped = false;
  #budgetMa = readBudget();
  /** @type {(() => void) | null} */ #unsubBudget = null;
  /** @type {(() => void) | null} */ #unsubAccepted = null;
  /** @type {(() => void) | null} */ #unsubConstraint = null;
  /** @type {number[] | null} */ #constrained = null;
  #constrainedAt = 0;
  // The pose mars_arm last accepted, in leader ticks. Null until it reports.
  /** @type {number[] | null} */ #accepted = null;
  #acceptedAt = 0;

  /** @param {GuardDeps} deps @param {number[]} ids */
  constructor({ leader, rosClient }, ids) {
    this.#leader = leader;
    this.#rosClient = rosClient;
    this.#ids = ids;
    this.#unsubBudget = onBudgetChange((mA) => {
      this.#budgetMa = mA;
      this.#overRounds = 0;
      if (this.#holding.size) this.#applyCurrents();
    });
  }

  /** @returns {LeaderGuardState} */
  get state() {
    return this.#state;
  }

  /** @returns {boolean} */
  get enabled() {
    return this.#enabled;
  }

  /**
   * @param {(state: LeaderGuardState) => void} cb Fires immediately, then on change.
   * @returns {() => void} unsubscribe
   */
  onChange(cb) {
    this.#listeners.add(cb);
    cb(this.#state);
    return () => this.#listeners.delete(cb);
  }

  /** Ids the guard is configured to hold, in servo order. */
  get guardedIds() {
    return this.#ids.filter((_, i) => JOINT_GUARD_ENABLED[i]);
  }

  /**
   * The band as it stands this round — joint 2's moves with joint 1, so callers
   * must not cache it across rounds.
   * @param {number} id @returns {import("./leaderLimits.js").Band | undefined}
   */
  band(id) {
    return this.#effective.get(id) ?? this.#bands.get(id);
  }

  /**
   * Joint 2's floor rides on where joint 1 is: lowering it across the front arc
   * folds the arm into the frame, so the reachable band narrows there and opens
   * again as joint 1 swings clear. Static per-joint bands cannot express that,
   * which is how the arm reached the body while every joint was "in range".
   * @param {number[]} positions
   */
  #recomputeBands(positions) {
    this.#effective.clear();
    const joint1 = positions[0];
    for (const [id, band] of this.#bands) {
      if (this.#ids.indexOf(id) !== 1 || joint1 === undefined) {
        this.#effective.set(id, band);
        continue;
      }
      const floor = joint2FloorRad(tickToRad(joint1), tickToRad(band.min), J2_SHAPE);
      this.#effective.set(id, { min: Math.max(band.min, Math.ceil(radToTick(floor))), max: band.max });
    }
  }

  /**
   * Read the follower's real limits and prepare the guarded servos. Safe to call
   * repeatedly; a failed read simply leaves the guard disarmed and the arm limp.
   */
  async arm() {
    if (this.#rosClient.state !== "connected") return;
    /** @type {Record<string, unknown> | null} */
    let res = null;
    try {
      res = await this.#rosClient.callService(ARM_GET_PARAMETERS_SERVICE, {
        names: ARM_POSITION_LIMITS_PARAMS,
      });
    } catch {
      return; // robot not answering — stay disarmed rather than guess a band
    }
    const values = Array.isArray(res?.values) ? res.values : [];
    this.#bands.clear();
    values.forEach((value, i) => {
      const id = this.#ids[i];
      if (id === undefined || !JOINT_GUARD_ENABLED[i]) return;
      if (!value || value.type !== PARAMETER_DOUBLE_ARRAY) return;
      const band = limitsToBand(value.double_array_value ?? [], JOINT_DIRECTION_FLIPPED[i]);
      if (band) this.#bands.set(id, band);
    });
    this.#subscribeAccepted();
    if (!this.#bands.size) return;
    this.#prepareMode();
    this.#patch({ armed: true, error: null });
  }

  /**
   * Current-based position control, set once. It is an EEPROM write that the
   * servo rejects while torqued, so torque goes off first — which is also the
   * state we want the arm resting in.
   */
  #prepareMode() {
    if (this.#modeReady) return;
    // Every joint, not just the banded ones: divergence can need to push a joint
    // that has no band at all — joint_2 being clamped by the body keepout is
    // exactly that case, and it is the one the operator most needs to feel.
    this.#leader.writeTorque(this.#ids, false);
    this.#leader.writeOperatingMode(this.#ids, OPERATING_MODE_CURRENT_POSITION);
    this.#modeReady = true;
  }

  /**
   * Track what mars_arm actually accepted. Its message is in radians and already
   * un-flipped, so it converts straight to leader ticks.
   */
  #subscribeAccepted() {
    if (this.#unsubConstraint === null) {
      this.#unsubConstraint = this.#rosClient.subscribe(
        ARM_CONSTRAINT_TOPIC,
        (msg) => {
          const data = msg && Array.isArray(msg.data) ? msg.data : null;
          if (!data) return;
          this.#constrained = data.slice(0, this.#ids.length);
          this.#constrainedAt = performance.now();
        },
        undefined,
        "std_msgs/msg/Int32MultiArray",
      );
    }
    if (this.#unsubAccepted) return;
    this.#unsubAccepted = this.#rosClient.subscribe(
      ARM_COMMAND_STATE_TOPIC,
      (msg) => {
        const pos = msg && Array.isArray(msg.position) ? msg.position : null;
        if (!pos || pos.length < this.#ids.length) return;
        this.#accepted = pos.slice(0, this.#ids.length).map((/** @type {number} */ rad) => radToTick(rad));
        this.#acceptedAt = performance.now();
      },
      undefined,
      "sensor_msgs/msg/JointState",
    );
  }

  /**
   * Signed divergence per joint in ticks, leader minus what the robot accepted,
   * or null while that report is missing or stale. Stale must read as absent:
   * pushing toward a pose the arm may already have left is worse than going limp.
   * @param {number[]} positions
   * @returns {(number | null)[]}
   */
  #divergence(positions) {
    const accepted = this.#accepted;
    const now = performance.now();
    const fresh = accepted && now - this.#acceptedAt < DIVERGENCE_STALE_MS;
    // No reason report, or a stale one, means no constraint is known — and an
    // unexplained gap is exactly the case that must NOT be pushed on.
    const reasons = this.#constrained && now - this.#constrainedAt < DIVERGENCE_STALE_MS ? this.#constrained : null;
    if (!fresh || !reasons) return this.#ids.map(() => null);
    return this.#ids.map((_, i) => {
      if ((reasons[i] ?? CONSTRAINT_NONE) === CONSTRAINT_NONE) return null;
      const tick = positions[i];
      const want = accepted[i];
      return tick === undefined || want === undefined ? null : tick - want;
    });
  }

  /** @param {boolean} on Turning the guard off releases anything held. */
  setEnabled(on) {
    if (this.#enabled === on) return;
    this.#enabled = on;
    if (!on) this.releaseAll();
  }

  /** Drop every hold and leave the arm limp. */
  releaseAll() {
    if (this.#holding.size) {
      this.#leader.writeTorque([...this.#holding], false);
      this.#holding.clear();
    }
    this.#overRounds = 0;
    this.#patch({ holding: [], allocatedMa: 0 });
  }

  /**
   * One position round. Decides which joints are out of reach, holds them at the
   * boundary, and keeps the total draw inside the budget.
   * @param {LeaderArmState} state
   */
  update(state) {
    if (!state.positions || !state.currents) return;
    this.#recomputeBands(state.positions);
    const drawMa = totalCurrent(state.currents);

    if (!this.#enabled || !this.#state.armed) {
      this.#patch({ drawMa });
      return;
    }
    if (this.#tripped) {
      // Only bringing every guarded joint back inside re-arms the wall — the
      // operator has to undo the reach that overdrew before it will push again.
      if (!this.#allInside(state.positions)) {
        this.#patch({ drawMa });
        return;
      }
      this.#tripped = false;
      this.#patch({ error: null });
    }
    if (this.#trip(drawMa)) return;

    const before = this.#holding.size;
    const diverged = this.#divergence(state.positions);
    /** @type {Map<number, number>} */
    const goals = new Map();
    /** @type {Map<number, number>} */
    const wants = new Map();
    let worstJoint = 0;
    let worstTicks = 0;

    this.#ids.forEach((id, i) => {
      const tick = state.positions?.[i];
      if (tick === undefined) return;

      // Two reasons to push back, and divergence wins where both apply: it is
      // what the robot actually did, while the band is only our model of it.
      const error = diverged[i];
      const beyond = error !== null && Math.abs(error) > DIVERGENCE_DEADBAND_TICKS;
      const band = this.band(id);
      const outside = !!band && isOutside(tick, band, HYSTERESIS_TICKS, this.#holding.has(id));

      if (beyond) {
        this.#holding.add(id);
        // Drive to the pose the robot accepted — the operator is pushed toward a
        // reachable state, not merely stopped at a boundary.
        goals.set(id, tick - /** @type {number} */ (error));
        wants.set(id, divergenceCurrent(/** @type {number} */ (error), DIVERGENCE_SHAPE));
        if (Math.abs(/** @type {number} */ (error)) > Math.abs(worstTicks)) {
          worstTicks = /** @type {number} */ (error);
          worstJoint = i + 1;
        }
      } else if (outside && band) {
        this.#holding.add(id);
        goals.set(id, clampTick(tick, band));
      } else if (this.#holding.has(id)) {
        this.#holding.delete(id);
        this.#leader.writeTorque([id], false);
      }
    });

    // Re-allocate whenever the set changed OR anything held is still untorqued:
    // a servo swapped in while another swapped out leaves the size equal, and
    // torquing it without writing its goal current first would energize it at
    // the servo's 1750 mA default.
    const untorqued = [...this.#holding].some((id) => !this.#leader.torqued.has(id));
    if (this.#holding.size !== before || untorqued || wants.size) this.#applyCurrents(wants);
    this.#driveHolds(goals);
    this.#patch({
      holding: [...this.#holding],
      drawMa,
      divergedJoint: worstJoint,
      divergedTicks: Math.round(worstTicks),
    });
  }

  /**
   * Budget enforcement. Sustained overdraw walks the allocation down; a single
   * round past the hard ceiling cuts torque outright — the ceiling exists
   * because the host's rail, not the servo, is what fails first.
   * @param {number} drawMa
   * @returns {boolean} true if torque was cut and the round is over.
   */
  #trip(drawMa) {
    if (drawMa > LEADER_CURRENT_CEILING_MA) {
      this.releaseAll();
      this.#tripped = true;
      this.#patch({ drawMa, error: `over ${LEADER_CURRENT_CEILING_MA} mA — torque cut` });
      return true;
    }
    if (!this.#holding.size) {
      this.#overRounds = 0;
      return false;
    }
    if (drawMa <= this.#budgetMa) {
      this.#overRounds = 0;
      return false;
    }
    this.#overRounds += 1;
    if (this.#overRounds < OVER_BUDGET_ROUNDS) return false;
    this.#overRounds = 0;
    const backed = Math.floor(this.#state.allocatedMa * BACKOFF);
    if (backed < MIN_USEFUL_MA) {
      this.releaseAll();
      this.#tripped = true;
      this.#patch({ drawMa, error: "cannot hold within the current budget" });
      return true;
    }
    this.#writeCurrents(backed);
    return false;
  }

  /** @param {number[]} positions @returns {boolean} */
  #allInside(positions) {
    return this.#ids.every((id, i) => {
      const band = this.band(id);
      const tick = positions[i];
      if (!band || tick === undefined) return true;
      return !isOutside(tick, band, HYSTERESIS_TICKS, false);
    });
  }

  /**
   * Split the budget across whatever is holding. A joint with a divergence force
   * asks for a specific current; the rest share what is left equally. Every
   * request is capped so the total can never exceed the budget however hard the
   * operator pushes.
   * @param {Map<number, number>} [wants] Per-joint mA requested by divergence.
   */
  #applyCurrents(wants) {
    this.#writeCurrents(allocateCurrent(this.#holding.size, this.#budgetMa), wants);
  }

  /** @param {number} perServoMa @param {Map<number, number>} [wants] */
  #writeCurrents(perServoMa, wants) {
    if (!this.#holding.size) {
      this.#patch({ allocatedMa: 0 });
      return;
    }
    const share = allocateCurrent(this.#holding.size, this.#budgetMa);
    /** @type {Map<number, number>} */
    const byId = new Map();
    for (const id of this.#holding) {
      const asked = wants?.get(id);
      byId.set(id, Math.min(share, asked === undefined ? perServoMa : asked));
    }
    this.#leader.writeGoalCurrent(byId);
    this.#patch({ allocatedMa: Math.max(...byId.values()) });
  }

  /**
   * Point every held servo at its target — the pose the robot accepted where the
   * follower diverged, the band edge otherwise. Goal current is already set, so
   * the servo walks there under a capped push rather than snapping.
   * @param {Map<number, number>} goals Target tick per servo id.
   */
  #driveHolds(goals) {
    if (!goals.size) return;
    /** @type {number[]} */
    const fresh = [];
    for (const id of goals.keys()) if (!this.#leader.torqued.has(id)) fresh.push(id);
    // Torque after the goal current is queued, never before: a servo torqued at
    // its 1750 mA default, even for one round, is exactly the spike the budget
    // exists to prevent.
    if (fresh.length) this.#leader.writeTorque(fresh, true);
    this.#leader.writeGoalPosition(goals);
  }

  destroy() {
    this.#unsubBudget?.();
    this.#unsubBudget = null;
    this.#unsubAccepted?.();
    this.#unsubAccepted = null;
    this.#unsubConstraint?.();
    this.#unsubConstraint = null;
    this.releaseAll();
    this.#listeners.clear();
  }

  /** @param {Partial<LeaderGuardState>} patch */
  #patch(patch) {
    const next = { ...this.#state, ...patch };
    if (
      next.armed === this.#state.armed &&
      next.drawMa === this.#state.drawMa &&
      next.allocatedMa === this.#state.allocatedMa &&
      next.divergedJoint === this.#state.divergedJoint &&
      next.divergedTicks === this.#state.divergedTicks &&
      next.error === this.#state.error &&
      next.holding.join() === this.#state.holding.join()
    ) {
      return;
    }
    this.#state = next;
    for (const cb of [...this.#listeners]) {
      try {
        cb(this.#state);
      } catch (err) {
        console.error("[leaderGuard] listener threw:", err);
      }
    }
  }
}
