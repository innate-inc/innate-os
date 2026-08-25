// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Policy page — drive the nav policy from a sentence and watch it think.
//
// The scene is the same one teleop renders (SimSession in sim, WebRTC video on
// a robot), opened with the waypoints overlay already lit: a page about the
// policy that hides the policy's output would be asking every visitor to go
// find a chip first.
//
// The goal goes straight to /innate_nav/navigate rather than through the
// skills server. That is the same action the innate_nav skill calls, so
// behaviour is identical, and it keeps Stop wired to THIS run's goal handle
// instead of "whatever skill is running".

import { mountPage } from "../pageMount.js";
import { ros } from "../rosClient.js";
import { robotSessionFactory } from "../robotSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import {
  NAV_POLICY_ACTION,
  NAV_POLICY_ACTION_TYPE,
  NAV_POLICY_CHECK_SERVICE,
  NAV_POLICY_OBSERVATIONS_TOPIC,
  NAV_POLICY_STATUS_TOPIC,
} from "../constants.js";
import { createMap } from "../map/mapWidget.js";
import { createObsBar } from "./obsBar.js";
import { createStatusPanel } from "./statusPanel.js";
import { createTuningPanel } from "./tuningPanel.js";

const { createSession, createStage } = await robotSessionFactory();

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "policy-page", (root) => {
    root.innerHTML = `
      <div class="policy-obs-slot"></div>
      <div class="policy-main">
        <form class="policy-bar">
          <input class="policy-instruction" type="text" autocomplete="off"
                 placeholder="Tell the robot where to go — “drive through the doorway into the next room”" />
          <input class="policy-server" type="text" autocomplete="off"
                 placeholder="policy server (optional)" />
          <button class="policy-test" type="button">Test</button>
          <button class="policy-go" type="submit">Go</button>
          <button class="policy-stop" type="button" disabled>Stop</button>
        </form>
        <div class="policy-views">
          <div class="policy-scene"></div>
          <div class="policy-map"></div>
        </div>
        <div class="policy-status" role="status"></div>
      </div>
      <aside class="policy-side"></aside>`;

    const form = /** @type {HTMLFormElement} */ (root.querySelector(".policy-bar"));
    const instruction = /** @type {HTMLInputElement} */ (root.querySelector(".policy-instruction"));
    const server = /** @type {HTMLInputElement} */ (root.querySelector(".policy-server"));
    const go = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-go"));
    const test = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-test"));
    const stop = /** @type {HTMLButtonElement} */ (root.querySelector(".policy-stop"));
    const line = /** @type {HTMLElement} */ (root.querySelector(".policy-status"));
    const sceneRoot = /** @type {HTMLElement} */ (root.querySelector(".policy-scene"));

    // The map carries the waypoints on hardware, where the stage is camera
    // video and there is no floor to draw them on.
    const map = createMap(/** @type {HTMLElement} */ (root.querySelector(".policy-map")), {
      zoom: 8,
      layers: { policy: true },
    });

    const obsBar = createObsBar(/** @type {HTMLElement} */ (root.querySelector(".policy-obs-slot")));

    const session = createSession();
    const scene = createStage
      ? createStage(sceneRoot, session, { chipsOn: ["waypoints"] })
      : createVideoStage(sceneRoot, session);
    const side = /** @type {HTMLElement} */ (root.querySelector(".policy-side"));
    const panel = createStatusPanel(side);
    const tuning = createTuningPanel(side);
    // The stage only draws what the session streams it; without this the scene
    // mounts empty and the orbit camera has nothing to orbit.
    session.start();

    /** @type {{ cancel: () => void } | null} */
    let run = null;

    function setRunning(on, text) {
      go.disabled = on;
      stop.disabled = !on;
      instruction.disabled = on;
      server.disabled = on;
      test.disabled = on;
      if (text !== undefined) {
        line.textContent = text;
        line.classList.remove("bad");
      }
    }

    // The probe runs on the robot, not here: this page is served over HTTPS and
    // the server is plain HTTP on the LAN, and it is the robot's route that
    // matters anyway.
    test.addEventListener("click", async () => {
      test.disabled = true;
      line.classList.remove("bad");
      line.textContent = "testing…";
      try {
        const res = await ros.callService(
          NAV_POLICY_CHECK_SERVICE, { server: server.value.trim() }, 8000);
        line.textContent = res?.message || "no answer from the robot";
        line.classList.toggle("bad", !res?.success);
      } catch (err) {
        line.textContent = err?.message || "the robot did not answer";
        line.classList.add("bad");
      } finally {
        test.disabled = false;
      }
    });

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
      const finish = (text) => {
        run = null;
        setRunning(false, text);
        panel.clear();
        obsBar.clear();
      };
      promise.then(
        (values) => finish(values?.message || "done"),
        (err) => finish(err?.message || "failed"),
      );
    });

    stop.addEventListener("click", () => {
      if (!run) return;
      line.textContent = "stopping…";
      run.cancel();
    });

    const unsubStatus = ros.subscribe(NAV_POLICY_STATUS_TOPIC, (msg) => {
      if (typeof msg?.data !== "string") return;
      try {
        const status = JSON.parse(msg.data);
        panel.setStatus(status);
        obsBar.setStatus(status);
      } catch {
        // a truncated payload is not worth tearing the page down over
      }
    }, undefined, "std_msgs/msg/String");

    const unsubObs = ros.subscribe(NAV_POLICY_OBSERVATIONS_TOPIC, (msg) => {
      if (typeof msg?.data === "string") obsBar.setStrip(msg.data, msg?.header?.frame_id);
    }, undefined, "sensor_msgs/msg/CompressedImage");

    return {
      destroy() {
        // Leaving the page must not leave the robot driving.
        if (run) run.cancel();
        unsubStatus();
        unsubObs();
        scene.destroy();
        session.destroy();
        map.destroy();
        panel.destroy();
        tuning.destroy();
        obsBar.destroy();
      },
    };
  });
}
