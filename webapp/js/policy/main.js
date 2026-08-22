// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Policy page — drive the nav policy from a sentence and watch it think.
//
// Composition:
//   waypointView.js  the model's 8 waypoints, top-down and robot-up
//   statusPanel.js   the inference readout and the observation window
//
// The goal goes straight to /innate_nav/navigate rather than through the
// skills server. That is the same action the innate_nav skill calls, so
// behaviour is identical, and it keeps Stop on this page wired to THIS run's
// goal handle instead of "whatever skill is running".

import { mountPage } from "../pageMount.js";
import { ros } from "../rosClient.js";
import {
  NAV_POLICY_ACTION,
  NAV_POLICY_ACTION_TYPE,
  NAV_POLICY_OBSERVATIONS_TOPIC,
  NAV_POLICY_PATH_TOPIC,
  NAV_POLICY_STATUS_TOPIC,
  ODOM_TOPIC,
} from "../constants.js";
import { createWaypointView } from "./waypointView.js";
import { createStatusPanel } from "./statusPanel.js";

// A path that stops being republished is dimmed rather than left looking live.
// Plans arrive at 2-4Hz, so this only fires once they have genuinely stopped.
const PATH_STALE_MS = 2500;

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "policy-page", (root) => {
    root.innerHTML = `
      <div class="policy-main">
        <form class="policy-bar">
          <input class="policy-instruction" type="text" autocomplete="off"
                 placeholder="Tell the robot where to go — “drive through the doorway into the next room”" />
          <input class="policy-server" type="text" autocomplete="off"
                 placeholder="policy server (optional)" />
          <button class="policy-go" type="submit">Go</button>
          <button class="policy-stop" type="button" disabled>Stop</button>
        </form>
        <div class="policy-scene"></div>
        <div class="policy-status" role="status"></div>
      </div>
      <aside class="policy-side"></aside>`;

    const form = /** @type {HTMLFormElement} */ (root.querySelector(".policy-bar"));
    const instruction = /** @type {HTMLInputElement} */ (root.querySelector(".policy-instruction"));
    const server = /** @type {HTMLInputElement} */ (root.querySelector(".policy-server"));
    const go = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-go"));
    const stop = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-stop"));
    const line = /** @type {HTMLElement} */ (root.querySelector(".policy-status"));

    const scene = createWaypointView(/** @type {HTMLElement} */ (root.querySelector(".policy-scene")));
    const panel = createStatusPanel(/** @type {HTMLElement} */ (root.querySelector(".policy-side")));

    /** @type {{ cancel: () => void } | null} */
    let run = null;
    let lastPathAt = 0;

    function setRunning(on, text) {
      go.disabled = on;
      stop.disabled = !on;
      instruction.disabled = on;
      server.disabled = on;
      if (text !== undefined) line.textContent = text;
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = instruction.value.trim();
      if (!text || run) return;
      setRunning(true, "starting…");
      const { promise, cancel } = ros.sendActionGoal(
        NAV_POLICY_ACTION, NAV_POLICY_ACTION_TYPE,
        { instruction: text, server: server.value.trim() },
        {},
      );
      run = { cancel };
      promise.then(
        (values) => {
          run = null;
          setRunning(false, values?.message || "done");
        },
        (err) => {
          run = null;
          setRunning(false, err?.message || "failed");
        },
      );
    });

    stop.addEventListener("click", () => {
      if (!run) return;
      line.textContent = "stopping…";
      run.cancel();
    });

    const unsubPath = ros.subscribe(NAV_POLICY_PATH_TOPIC, (msg) => {
      const poses = msg?.poses || [];
      lastPathAt = performance.now();
      scene.setWaypoints(poses.map((p) => ({
        x: p?.pose?.position?.x ?? 0, y: p?.pose?.position?.y ?? 0,
      })));
    }, undefined, "nav_msgs/msg/Path");

    const unsubOdom = ros.subscribe(ODOM_TOPIC, (msg) => {
      const p = msg?.pose?.pose;
      if (!p) return;
      scene.setPose({
        x: p.position.x, y: p.position.y,
        yaw: 2 * Math.atan2(p.orientation.z, p.orientation.w),
      });
    }, 100, "nav_msgs/msg/Odometry");

    const unsubStatus = ros.subscribe(NAV_POLICY_STATUS_TOPIC, (msg) => {
      if (typeof msg?.data !== "string") return;
      try {
        panel.setStatus(JSON.parse(msg.data));
      } catch {
        // a truncated payload is not worth tearing the page down over
      }
    }, undefined, "std_msgs/msg/String");

    const unsubObs = ros.subscribe(NAV_POLICY_OBSERVATIONS_TOPIC, (msg) => {
      if (typeof msg?.data === "string") panel.setStrip(msg.data);
    }, undefined, "sensor_msgs/msg/CompressedImage");

    const staleTimer = setInterval(() => {
      scene.setStale(performance.now() - lastPathAt > PATH_STALE_MS);
    }, 500);

    return {
      destroy() {
        clearInterval(staleTimer);
        // Leaving the page must not leave the robot driving.
        if (run) run.cancel();
        unsubPath();
        unsubOdom();
        unsubStatus();
        unsubObs();
        scene.destroy();
        panel.destroy();
      },
    };
  });
}
