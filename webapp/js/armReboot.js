// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// One reboot behaviour for every entry point (teleop arm panel, servo-protection
// alert, profiling). The reboot service deliberately leaves the arm limp, but a
// limp arm is never what the operator wanted — every caller re-enabled torque by
// hand — so re-enabling is part of the flow here.

import { ARM_REBOOT_SERVICE, ARM_REBOOT_TIMEOUT_MS, ARM_TORQUE_ON_SERVICE } from "./constants.js";

// The servos re-initialize for a beat after the power-cycle; torque_on issued
// too early lands on a servo that is not listening yet. Matches the settle in
// Manipulation.recover().
const SERVO_REINIT_MS = 2000;

/**
 * Reboot the arm servos, then re-enable torque.
 * @param {import("./rosClient.js").RosClient} rosClient
 * @returns {Promise<{ ok: boolean, torqueOn: boolean, message: string }>}
 *   `ok` covers the reboot itself; `torqueOn` says whether the arm came back
 *   energized, so a caller can surface a limp arm rather than imply success.
 *   Never rejects — a thrown service error is reported as a failed reboot.
 */
export async function rebootArmAndEnableTorque(rosClient) {
  try {
    const res = await rosClient.callService(ARM_REBOOT_SERVICE, {}, ARM_REBOOT_TIMEOUT_MS);
    if (res && res.success === false) {
      return { ok: false, torqueOn: false, message: res.message || "Reboot failed" };
    }
  } catch (err) {
    return { ok: false, torqueOn: false, message: err instanceof Error ? err.message : "Reboot failed" };
  }

  await new Promise((resolve) => setTimeout(resolve, SERVO_REINIT_MS));

  try {
    const res = await rosClient.callService(ARM_TORQUE_ON_SERVICE, {});
    if (res && res.success === false) {
      return { ok: true, torqueOn: false, message: res.message || "Rebooted — torque re-enable failed" };
    }
  } catch {
    return { ok: true, torqueOn: false, message: "Rebooted — torque re-enable failed" };
  }
  return { ok: true, torqueOn: true, message: "Servos rebooted, torque on" };
}
