// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// DriveController — owns the /joystick wire contract and input arbitration.
//
// Wire contract (deadman, matched to the robot's mars_app consumer):
//   - while engaged: re-publish the latched vector every 150 ms, plus an
//     immediate publish on change (rate-limited so pointermove spam can't
//     flood rosbridge)
//   - on full disengage: exactly one {x:0, y:0}
//   - while idle: silence, so a released stick never fights other /cmd_vel
//     publishers (e.g. skill playback)
//
// Arbitration: the on-screen joystick wins while engaged; keyboard drive
// resumes the moment the stick is released.

import { ros } from "./rosClient.js";
import { JOYSTICK_TOPIC } from "./constants.js";

const HEARTBEAT_MS = 150;
// Cap change-publishes to ~60 Hz (one per frame, matching the mobile app, which
// publishes on every touch-move). A trailing publish guarantees the final
// resting value still goes out within this window rather than waiting for the
// next heartbeat — that tail was the bulk of the felt drive latency.
const CHANGE_PUBLISH_MIN_GAP_MS = 16;
const SIM_ENVIRONMENT_SWITCH_EVENT = "innate:sim-environment-switch-state";

/** @param {number} v */
const round3 = (v) => Math.round(v * 1000) / 1000;

export class DriveController {
  /** @type {Record<DriveSource, { x: number, y: number, engaged: boolean }>} */
  #inputs = {
    joystick: { x: 0, y: 0, engaged: false },
    keyboard: { x: 0, y: 0, engaged: false },
  };
  /** @type {DriveState} */
  #active = { source: null, x: 0, y: 0, engaged: false };
  /** @type {number | null} */ #heartbeat = null;
  /** @type {number | null} */ #changeTimer = null;
  #lastPublishAt = 0;
  /** @type {Set<(state: DriveState) => void>} */ #listeners = new Set();
  #environmentSwitching = false;
  /** @type {Set<DriveSource>} */ #needsRelease = new Set();

  constructor() {
    // The page shell owns a durable class as well as the transition event.
    // A controller can be constructed after the overlay went up (for example
    // when the SPA route changes mid-switch), so inherit the lock instead of
    // relying only on an event that has already happened.
    this.#environmentSwitching =
      document.documentElement?.classList?.contains("sim-environment-switching") === true;
    if (this.#environmentSwitching) this.#needsRelease = new Set(["joystick", "keyboard"]);
    window.addEventListener("blur", () => this.haltAll());
    window.addEventListener("pagehide", () => this.haltAll());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.haltAll();
    });
    // The simulator keeps this document alive while physics, Nav2, and ROS are
    // replaced underneath it. A full-screen transition overlay blocks pointer
    // input, but it cannot release a keyboard key that was already held. Make
    // the shared arbitration layer the safety boundary: publish one stop at
    // transition start and refuse every source until the new world is ready.
    document.addEventListener(SIM_ENVIRONMENT_SWITCH_EVENT, (event) => {
      const active = event instanceof CustomEvent && event.detail?.active === true;
      if (active === this.#environmentSwitching) return;
      this.#environmentSwitching = active;
      if (active) {
        for (const source of /** @type {DriveSource[]} */ (["joystick", "keyboard"])) {
          if (this.#inputs[source].engaged) this.#needsRelease.add(source);
        }
        this.haltAll();
      }
    });
    ros.onStateChange((state) => {
      if (state !== "connected") this.haltAll();
    });
  }

  /**
   * Latch an input source's vector. Robot frame: y > 0 = forward (callers
   * apply any screen-coordinate flip before this point).
   * @param {DriveSource} source
   * @param {number} x
   * @param {number} y
   * @param {boolean} engaged
   */
  setInput(source, x, y, engaged) {
    if (this.#environmentSwitching) {
      // Keep callers' key/pointer bookkeeping independent from the interlock;
      // they may continue reporting while an old key is physically down, but
      // none of it becomes latched drive state until that source has returned
      // to neutral after the transition. This prevents a key held across a
      // long restart from moving the newly spawned robot immediately.
      if (engaged) this.#needsRelease.add(source);
      else this.#needsRelease.delete(source);
      return;
    }
    if (this.#needsRelease.has(source)) {
      if (!engaged) this.#needsRelease.delete(source);
      return;
    }
    const input = this.#inputs[source];
    input.x = x;
    input.y = y;
    input.engaged = engaged;
    this.#recompute();
  }

  /** Zero everything. One zero goes out if we were engaged, then silence. */
  haltAll() {
    this.#inputs.joystick = { x: 0, y: 0, engaged: false };
    this.#inputs.keyboard = { x: 0, y: 0, engaged: false };
    this.#recompute();
  }

  /**
   * Observe the arbitrated drive state (the on-screen knob mirrors keyboard
   * drive through this). Fires immediately with the current state.
   * @param {(state: DriveState) => void} cb
   * @returns {() => void} unsubscribe
   */
  onActiveChange(cb) {
    this.#listeners.add(cb);
    cb(this.#active);
    return () => this.#listeners.delete(cb);
  }

  #recompute() {
    const { joystick, keyboard } = this.#inputs;
    const source = joystick.engaged ? "joystick" : keyboard.engaged ? "keyboard" : null;
    const input = source ? this.#inputs[source] : null;
    /** @type {DriveState} */
    const next = input
      ? { source, x: input.x, y: input.y, engaged: true }
      : { source: null, x: 0, y: 0, engaged: false };
    const prev = this.#active;
    const changed =
      next.source !== prev.source ||
      next.x !== prev.x ||
      next.y !== prev.y ||
      next.engaged !== prev.engaged;
    this.#active = next;

    if (next.engaged) {
      this.#startHeartbeat();
      if (changed) this.#publishChange();
    } else if (prev.engaged) {
      this.#stopHeartbeat();
      this.#clearChangeTimer();
      this.#publish(0, 0); // the single release zero
    }

    if (changed) {
      for (const cb of [...this.#listeners]) {
        try {
          cb(next);
        } catch (err) {
          console.error("[drive] listener threw:", err);
        }
      }
    }
  }

  // Leading-edge publish on change, capped at one per CHANGE_PUBLISH_MIN_GAP_MS;
  // if a change lands inside that window, a single trailing timer flushes the
  // latest value at the window's end so it never waits for the next heartbeat.
  #publishChange() {
    const elapsed = performance.now() - this.#lastPublishAt;
    if (elapsed >= CHANGE_PUBLISH_MIN_GAP_MS) {
      this.#clearChangeTimer();
      this.#publishActive();
    } else if (this.#changeTimer === null) {
      this.#changeTimer = setTimeout(() => {
        this.#changeTimer = null;
        if (this.#active.engaged) this.#publishActive();
      }, CHANGE_PUBLISH_MIN_GAP_MS - elapsed);
    }
  }

  #clearChangeTimer() {
    if (this.#changeTimer !== null) {
      clearTimeout(this.#changeTimer);
      this.#changeTimer = null;
    }
  }

  #startHeartbeat() {
    if (this.#heartbeat !== null) return;
    this.#heartbeat = setInterval(() => this.#publishActive(), HEARTBEAT_MS);
  }

  #stopHeartbeat() {
    if (this.#heartbeat === null) return;
    clearInterval(this.#heartbeat);
    this.#heartbeat = null;
  }

  #publishActive() {
    this.#publish(this.#active.x, this.#active.y);
  }

  /**
   * @param {number} x
   * @param {number} y
   */
  #publish(x, y) {
    if (ros.state !== "connected") return;
    ros.publish(JOYSTICK_TOPIC, { x: round3(x), y: round3(y) });
    this.#lastPublishAt = performance.now();
  }
}

/** The one controller every drive input shares. */
export const drive = new DriveController();
