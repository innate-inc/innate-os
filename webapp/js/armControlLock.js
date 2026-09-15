// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// One arm, one driver. The teleop page can have the leader-arm USB panel and
// the camera-control HUD open at once, and both stream joint targets; whichever
// claims first keeps the arm until it releases. This is an in-page interlock,
// not a robot-wide lock -- another client still serializes on the arm server's
// motion lock, and the panels say so.

/** @type {string | null} */
let holder = null;
/** @type {Set<(holder: string | null) => void>} */
const listeners = new Set();

/** Take arm control for `name`, or null if someone else already has it.
 * @param {string} name @returns {(() => void) | null} release */
export function claimArmControl(name) {
  if (holder !== null && holder !== name) return null;
  holder = name;
  for (const cb of listeners) cb(holder);
  return () => {
    if (holder !== name) return;
    holder = null;
    for (const cb of listeners) cb(holder);
  };
}

/** @returns {string | null} */
export const armControlHolder = () => holder;

/** @param {(holder: string | null) => void} cb @returns {() => void} */
export function onArmControlChange(cb) {
  listeners.add(cb);
  cb(holder);
  return () => listeners.delete(cb);
}
