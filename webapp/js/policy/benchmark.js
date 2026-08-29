// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The navigation benchmark, run one scenario at a time from the page.
//
// A scenario is a spawn pose, an instruction, and a goal. Scenarios come in
// sets that share a spawn and differ only in the instruction, so what
// separates them is whether the policy did what it was told — which is not
// something a single episode can show you.
//
// Running one places the robot (sim only) and sends the instruction, then
// watches the distance to the goal. The verdict is the eval harness's: did it
// STOP inside the radius, and did it ever get inside it.

import {
  NAV_POLICY_ACTION,
  NAV_POLICY_ACTION_TYPE,
  ODOM_TOPIC,
  SIM_SET_POSE_TOPIC,
} from "../constants.js";

const SETTLE_MS = 1500;
const TURN_LABEL = { straight: "straight", left: "left", right: "right", back: "turn around" };

/** @param {any} ros @param {HTMLElement} parent */
export function createBenchmark(parent, ros) {
  const root = document.createElement("div");
  root.className = "policy-bench";
  root.innerHTML = `
    <div class="policy-panel-head">Benchmark</div>
    <div class="bench-note">loading scenarios…</div>
    <div class="bench-list"></div>`;
  parent.appendChild(root);

  const note = /** @type {HTMLElement} */ (root.querySelector(".bench-note"));
  const list = /** @type {HTMLElement} */ (root.querySelector(".bench-list"));

  /** @type {any[]} */ let scenarios = [];
  let radius = 0.75;
  /** @type {{x:number,y:number}|null} */ let pose = null;
  /** @type {{cancel:()=>void}|null} */ let active = null;

  const unadvertise = ros.advertise(SIM_SET_POSE_TOPIC, "geometry_msgs/msg/Pose2D");
  const unsubOdom = ros.subscribe(ODOM_TOPIC, (msg) => {
    const p = msg?.pose?.pose?.position;
    if (p) pose = { x: p.x, y: p.y };
  }, 100, "nav_msgs/msg/Odometry");

  const dist = (gx, gy) => (pose ? Math.hypot(pose.x - gx, pose.y - gy) : NaN);

  /** @param {any} sc @param {HTMLElement} row @param {HTMLElement} out */
  async function run(sc, row, out) {
    if (active) return;
    row.classList.add("running");
    out.textContent = "placing…";
    // Sim only: put the robot where the scenario starts. Driving there first
    // would arrive with a different heading and a different history.
    ros.publish(SIM_SET_POSE_TOPIC, {
      x: sc.spawn[0], y: sc.spawn[1], theta: (sc.spawn[2] * Math.PI) / 180 });
    await new Promise((r) => setTimeout(r, SETTLE_MS));

    let best = Infinity;
    const tick = setInterval(() => {
      const d = dist(sc.goal[0], sc.goal[1]);
      if (!Number.isNaN(d)) {
        best = Math.min(best, d);
        out.textContent = `${d.toFixed(2)} m away (best ${best.toFixed(2)})`;
      }
    }, 200);

    out.textContent = "driving…";
    const { promise, cancel } = ros.sendActionGoal(
      NAV_POLICY_ACTION, NAV_POLICY_ACTION_TYPE,
      { instruction: sc.instruction, server: "" }, {});
    active = { cancel };
    try {
      await promise;
    } catch {
      // a cancel or a failed goal is a result too — the distance is the verdict
    }
    clearInterval(tick);
    active = null;
    const final = dist(sc.goal[0], sc.goal[1]);
    const ok = final <= radius;
    row.classList.remove("running");
    row.classList.toggle("pass", ok);
    row.classList.toggle("fail", !ok);
    out.textContent = `${ok ? "reached" : "missed"} — stopped ${final.toFixed(2)} m away`
      + (best <= radius && !ok ? `, was within ${best.toFixed(2)} m` : "");
  }

  fetch("public/nav_benchmark.json")
    .then((r) => r.json())
    .then((doc) => {
      scenarios = doc.scenarios || [];
      radius = doc.goal_radius_m ?? radius;
      note.textContent = `${scenarios.length} scenarios · goal radius ${radius} m · sim only`;
      for (const sc of scenarios) {
        const row = document.createElement("div");
        row.className = "bench-row";
        row.innerHTML = `
          <button class="bench-run" type="button" title="place the robot and send the instruction">▶</button>
          <div class="bench-body">
            <div class="bench-instruction"></div>
            <div class="bench-meta"></div>
          </div>`;
        /** @type {HTMLElement} */ (row.querySelector(".bench-instruction")).textContent = sc.instruction;
        const meta = /** @type {HTMLElement} */ (row.querySelector(".bench-meta"));
        meta.textContent = `${sc.id} · ${TURN_LABEL[sc.turn] ?? sc.turn} · ${sc.path_m} m`;
        row.querySelector(".bench-run")?.addEventListener("click", () => run(sc, row, meta));
        list.appendChild(row);
      }
    })
    .catch((err) => { note.textContent = `could not load scenarios: ${err?.message || err}`; });

  return {
    destroy() {
      active?.cancel();
      unadvertise();
      unsubOdom();
      root.remove();
    },
  };
}
