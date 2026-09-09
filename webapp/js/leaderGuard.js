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
  ARM_GET_PARAMETERS_SERVICE,
  ARM_POSITION_LIMITS_PARAMS,
  JOINT_GUARD_ENABLED,
  LEADER_CURRENT_CEILING_MA,
  PARAMETER_DOUBLE_ARRAY,
} from "./constants.js";
import { OPERATING_MODE_CURRENT_POSITION } from "./dynamixel.js";
import { readBudget, onBudgetChange } from "./leaderBudget.js";
import { allocateCurrent, clampTick, isOutside, limitsToBand, totalCurrent } from "./leaderLimits.js";

// Re-entry margin, ~3.5°. A joint resting exactly on the limit would otherwise
// toggle the wall every round.
const HYSTERESIS_TICKS = 40;
// Rounds over budget before the allocation is walked back. One round can spike
// on a direction change without the average being over.
const OVER_BUDGET_ROUNDS = 3;
const BACKOFF = 0.8;
// Below this a hold is too weak to be felt; release instead of pretending.
const MIN_USEFUL_MA = 40;

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
  /** @type {Set<number>} */ #holding = new Set();
  /** @type {Set<(state: LeaderGuardState) => void>} */ #listeners = new Set();
  /** @type {LeaderGuardState} */ #state = {
    armed: false,
    holding: [],
    drawMa: 0,
    allocatedMa: 0,
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

  /** @param {number} id @returns {import("./leaderLimits.js").Band | undefined} */
  band(id) {
    return this.#bands.get(id);
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
      const band = limitsToBand(value.double_array_value ?? []);
      if (band) this.#bands.set(id, band);
    });
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
    const ids = [...this.#bands.keys()];
    this.#leader.writeTorque(ids, false);
    this.#leader.writeOperatingMode(ids, OPERATING_MODE_CURRENT_POSITION);
    this.#modeReady = true;
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
    this.#ids.forEach((id, i) => {
      const band = this.#bands.get(id);
      const tick = state.positions?.[i];
      if (!band || tick === undefined) return;
      const outside = isOutside(tick, band, HYSTERESIS_TICKS, this.#holding.has(id));
      if (outside) this.#holding.add(id);
      else if (this.#holding.has(id)) {
        this.#holding.delete(id);
        this.#leader.writeTorque([id], false);
      }
    });

    // Re-allocate whenever the set changed OR anything held is still untorqued:
    // a servo swapped in while another swapped out leaves the size equal, and
    // torquing it without writing its goal current first would energize it at
    // the servo's 1750 mA default.
    const untorqued = [...this.#holding].some((id) => !this.#leader.torqued.has(id));
    if (this.#holding.size !== before || untorqued) this.#applyCurrents();
    this.#driveHolds(state.positions);
    this.#patch({ holding: [...this.#holding], drawMa });
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
      const band = this.#bands.get(id);
      const tick = positions[i];
      if (!band || tick === undefined) return true;
      return !isOutside(tick, band, HYSTERESIS_TICKS, false);
    });
  }

  /** Split the budget across whatever is holding right now. */
  #applyCurrents() {
    this.#writeCurrents(allocateCurrent(this.#holding.size, this.#budgetMa));
  }

  /** @param {number} perServoMa */
  #writeCurrents(perServoMa) {
    if (!this.#holding.size) {
      this.#patch({ allocatedMa: 0 });
      return;
    }
    this.#leader.writeGoalCurrent(new Map([...this.#holding].map((id) => [id, perServoMa])));
    this.#patch({ allocatedMa: perServoMa });
  }

  /**
   * Point every held servo at its boundary tick. Goal current is already set, so
   * the servo walks back to the edge of the reachable band under a capped push
   * rather than snapping to it.
   * @param {number[]} positions
   */
  #driveHolds(positions) {
    if (!this.#holding.size) return;
    /** @type {Map<number, number>} */
    const goals = new Map();
    /** @type {number[]} */
    const fresh = [];
    this.#ids.forEach((id, i) => {
      const band = this.#bands.get(id);
      const tick = positions[i];
      if (!band || tick === undefined || !this.#holding.has(id)) return;
      goals.set(id, clampTick(tick, band));
      if (!this.#leader.torqued.has(id)) fresh.push(id);
    });
    // Torque after the goal current is queued, never before: a servo torqued at
    // its 1750 mA default, even for one round, is exactly the spike the budget
    // exists to prevent.
    if (fresh.length) this.#leader.writeTorque(fresh, true);
    this.#leader.writeGoalPosition(goals);
  }

  destroy() {
    this.#unsubBudget?.();
    this.#unsubBudget = null;
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
