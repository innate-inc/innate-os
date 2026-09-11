// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The leader arm's total current budget — how much all six servos together may
// draw while the guard holds a joint at its limit.
//
// This is per-device, not per-robot: the arm is bus-powered by whatever machine
// it is plugged into, and a laptop port and a phone port do not have the same
// headroom. So it lives in localStorage rather than the robot's settings.yaml,
// and the Settings page edits it here. Both the guard and that page import this
// module, so there is one clamp and one default between them.

import {
  LEADER_CURRENT_BUDGET_DEFAULT_MA,
  LEADER_CURRENT_BUDGET_KEY,
  LEADER_CURRENT_BUDGET_MIN_MA,
  LEADER_CURRENT_CEILING_MA,
} from "./constants.js";

/** @type {Set<(budgetMa: number) => void>} */
const listeners = new Set();

/** @param {number} mA @returns {number} */
export function clampBudget(mA) {
  if (!Number.isFinite(mA)) return LEADER_CURRENT_BUDGET_DEFAULT_MA;
  return Math.round(Math.min(LEADER_CURRENT_CEILING_MA, Math.max(LEADER_CURRENT_BUDGET_MIN_MA, mA)));
}

/** @returns {number} The stored budget, or the default where none is readable. */
export function readBudget() {
  let raw = null;
  try {
    raw = localStorage.getItem(LEADER_CURRENT_BUDGET_KEY);
  } catch {
    return LEADER_CURRENT_BUDGET_DEFAULT_MA; // private mode / storage disabled
  }
  if (raw === null) return LEADER_CURRENT_BUDGET_DEFAULT_MA;
  return clampBudget(Number(raw));
}

/**
 * Persist a new budget and tell every live listener. Returns what was actually
 * stored after clamping, which is what the caller should render.
 * @param {number} mA
 * @returns {number}
 */
export function writeBudget(mA) {
  const budget = clampBudget(mA);
  try {
    localStorage.setItem(LEADER_CURRENT_BUDGET_KEY, String(budget));
  } catch {
    // Unpersisted is still worth applying — the guard reads the live value below.
  }
  for (const cb of [...listeners]) {
    try {
      cb(budget);
    } catch (err) {
      console.error("[leaderBudget] listener threw:", err);
    }
  }
  return budget;
}

/**
 * @param {(budgetMa: number) => void} cb Fires on every write, so a panel open
 *   in one tab follows a change made on the Settings page in another.
 * @returns {() => void} unsubscribe
 */
export function onBudgetChange(cb) {
  listeners.add(cb);
  return () => listeners.delete(cb);
}
