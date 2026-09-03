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
const FAMILY_LABEL = { pointnav: "pointnav", objectnav: "objectnav", r2r: "vln" };

/** @param {HTMLElement} parent @param {any} ros
 *  @param {(sc:any|null)=>void} onSelect shows the scenario on the map */
export function createBenchmark(parent, ros, onSelect = () => {}) {
  const root = document.createElement("div");
  root.className = "policy-bench";
  root.innerHTML = `
    <div class="policy-panel-head">Benchmark</div>
    <div class="bench-note">loading scenarios…</div>
    <div class="bench-suites"></div>
    <div class="bench-selected" hidden></div>
    <div class="bench-now" hidden></div>
    <div class="bench-list"></div>`;
  parent.appendChild(root);

  const note = /** @type {HTMLElement} */ (root.querySelector(".bench-note"));
  const now = /** @type {HTMLElement} */ (root.querySelector(".bench-now"));
  const list = /** @type {HTMLElement} */ (root.querySelector(".bench-list"));
  const suites = /** @type {HTMLElement} */ (root.querySelector(".bench-suites"));
  const selBox = /** @type {HTMLElement} */ (root.querySelector(".bench-selected"));
  /** @type {Map<string, HTMLElement>} scenario id -> its row */
  const rows = new Map();
  /** The row this page started, if any. Runs started elsewhere (the headless
   *  runner) are matched by instruction instead, which is looser: the paired
   *  suite reuses wordings, so several rows can light up for one run. */
  let startedId = null;
  /** @type {any} the scenario shown on the map and armed for the Run button */
  let selected = null;
  /** @type {any[]} queued scenarios for a batch; empty when idle */
  let queue = [];
  let stopBatch = false;

  /** @type {any[]} */ let scenarios = [];
  let radius = 0.75;
  /** @type {{x:number,y:number}|null} */ let pose = null;
  /** @type {{cancel:()=>void}|null} */ let active = null;

  const unadvertise = ros.advertise(SIM_SET_POSE_TOPIC, "geometry_msgs/msg/Pose2D");
  const unsubOdom = ros.subscribe(ODOM_TOPIC, (msg) => {
    const p = msg?.pose?.pose?.position;
    if (p) pose = { x: p.x, y: p.y };
  }, 100, "nav_msgs/msg/Odometry");

  /** Nearest acceptable goal — objectnav counts any instance of the category. */
  const dist = (goals) =>
    pose ? Math.min(...goals.map(([gx, gy]) => Math.hypot(pose.x - gx, pose.y - gy))) : NaN;

  /** @param {any} sc @param {HTMLElement} row @param {HTMLElement} out */
  async function run(sc, row, out) {
    if (active) return;
    startedId = sc.id;
    row.classList.add("running");
    out.textContent = "placing…";
    // Sim only: put the robot where the scenario starts. Driving there first
    // would arrive with a different heading and a different history.
    ros.publish(SIM_SET_POSE_TOPIC, {
      x: sc.spawn[0], y: sc.spawn[1], theta: (sc.spawn[2] * Math.PI) / 180 });
    await new Promise((r) => setTimeout(r, SETTLE_MS));

    let best = Infinity;
    const tick = setInterval(() => {
      const d = dist(sc.goals);
      if (!Number.isNaN(d)) {
        best = Math.min(best, d);
        out.textContent = `${d.toFixed(2)} m away (best ${best.toFixed(2)})`;
      }
    }, 200);

    // The family selects the history window, and the action carries only the
    // instruction — so it is set on the node before the goal goes out.
    await ros.callService("/innate_nav_node/set_parameters", {
      parameters: [{ name: "task_family", value: {
        type: 4, bool_value: false, integer_value: 0, double_value: 0,
        string_value: sc.family || "r2r" } }],
    }).catch(() => {});
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
    startedId = null;
    const final = dist(sc.goals);
    const ok = final <= radius;
    row.classList.remove("running");
    row.classList.toggle("pass", ok);
    row.classList.toggle("fail", !ok);
    out.textContent = `${ok ? "reached" : "missed"} — stopped ${final.toFixed(2)} m away`
      + (best <= radius && !ok ? `, was within ${best.toFixed(2)} m` : "");
  }

  function select(sc) {
    selected = sc;
    for (const [id, row] of rows) row.classList.toggle("selected", id === sc?.id);
    onSelect(sc);
    selBox.hidden = !sc;
    if (!sc) return;
    selBox.replaceChildren();
    const text = document.createElement("div");
    text.className = "bench-selected-text";
    text.textContent = `${sc.id} · ${sc.instruction}`;
    const go = document.createElement("button");
    go.type = "button";
    go.className = "bench-go";
    go.textContent = "Run this";
    go.addEventListener("click", () => runOne(sc));
    selBox.append(text, go);
  }

  async function runOne(sc) {
    const row = rows.get(sc.id);
    if (!row) return;
    await run(sc, row, /** @type {HTMLElement} */ (row.querySelector(".bench-meta")));
  }

  /** Run a set back to back. Stoppable, because 60 scenarios is an hour. */
  async function runBatch(list_) {
    if (queue.length) { stopBatch = true; return; }
    queue = [...list_];
    stopBatch = false;
    renderSuites();
    for (const sc of queue) {
      if (stopBatch) break;
      select(sc);
      await runOne(sc);
    }
    queue = [];
    renderSuites();
  }

  function renderSuites() {
    const running = queue.length > 0;
    const groups = [
      ["everything", scenarios],
      ["pointnav", scenarios.filter((s) => s.family === "pointnav")],
      ["objectnav", scenarios.filter((s) => s.family === "objectnav")],
      ["vln", scenarios.filter((s) => s.suite === "families" && s.family === "r2r")],
      ["paired", scenarios.filter((s) => s.suite === "paired")],
    ];
    suites.replaceChildren();
    for (const [label, group] of groups) {
      if (!group.length) continue;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "bench-suite";
      b.textContent = running ? "Stop" : `${label} (${group.length})`;
      b.disabled = running && label !== "everything";
      b.addEventListener("click", () => runBatch(group));
      suites.appendChild(b);
    }
  }

  /** @param {any} status the parsed /nav_policy/status payload */
  function setStatus(status) {
    const live = Boolean(status?.running) && typeof status?.instruction === "string";
    // The page knows which row it started; a run driven from anywhere else is
    // matched on its wording, which is what the status carries.
    const hits = !live ? [] : startedId
      ? [startedId]
      : scenarios.filter((sc) => sc.instruction === status.instruction).map((sc) => sc.id);

    for (const [id, row] of rows) row.classList.toggle("running", hits.includes(id));
    if (!hits.length) {
      now.hidden = true;
      return;
    }
    const first = scenarios.find((sc) => sc.id === hits[0]);
    now.hidden = false;
    now.textContent = hits.length > 1
      ? `running: ${status.instruction} (${hits.length} scenarios share this wording)`
      : `running ${first.id}: ${first.instruction}`;
    // Keep it in view — the list is a hundred rows long and the running one is
    // usually scrolled off it.
    rows.get(hits[0])?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  fetch("public/nav_benchmark.json")
    .then((r) => r.json())
    .then((doc) => {
      scenarios = doc.scenarios || [];
      radius = doc.goal_radius_m ?? radius;
      note.textContent = `${scenarios.length} scenarios · goal radius ${radius} m · sim only`;
      renderSuites();
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
        const kind = FAMILY_LABEL[sc.family] ?? sc.family;
        const detail = sc.suite === "paired" ? (TURN_LABEL[sc.turn] ?? sc.turn) : kind;
        meta.textContent = `${sc.id} · ${detail} · ${sc.path_m} m`
          + (sc.goals.length > 1 ? ` · ${sc.goals.length} valid spots` : "");
        row.querySelector(".bench-run")?.addEventListener("click", (e) => {
          e.stopPropagation();
          run(sc, row, meta);
        });
        row.addEventListener("click", () => select(sc));
        rows.set(sc.id, row);
        list.appendChild(row);
      }
    })
    .catch((err) => { note.textContent = `could not load scenarios: ${err?.message || err}`; });

  return {
    setStatus,
    destroy() {
      stopBatch = true;
      active?.cancel();
      unadvertise();
      unsubOdom();
      root.remove();
    },
  };
}
