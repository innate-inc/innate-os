// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Nav telemetry sidebar — raw readouts of every navigation sensor, grouped
// into small panels: pose (AMCL map-frame + raw odom), velocity (commanded
// /cmd_vel vs measured /odom), lidar summary, nav/battery state, and a
// per-topic receive-rate table. Values fill in as messages arrive; a topic
// that never publishes just keeps its "—".

import { ros } from "../rosClient.js";
import { createVelocityTracker } from "./odomVelocity.js";
import {
  AMCL_POSE_TOPIC,
  BATTERY_STATE_TOPIC,
  CMD_VEL_TOPIC,
  LOCALIZATION_STATUS_TOPIC,
  MAP_TOPIC,
  NAV_CURRENT_MAP_TOPIC,
  NAV_CURRENT_MODE_TOPIC,
  ODOM_TOPIC,
  SCAN_TOPIC,
} from "../constants.js";

const DASH = "—";
const RATE_WINDOW_MS = 5000;

/** @param {number} rad */
function deg(rad) {
  return (rad * 180) / Math.PI;
}

/** @param {any} q quaternion → yaw radians, or null */
function yawOf(q) {
  if (!q) return null;
  return Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
}

/** Message-arrival tracker for the rates table (Hz over a sliding window). */
function makeRate() {
  /** @type {number[]} */
  const stamps = [];
  /** @type {number | null} */
  let firstTick = null;
  return {
    tick() {
      const now = performance.now();
      if (firstTick === null) firstTick = now;
      stamps.push(now);
    },
    hz() {
      const now = performance.now();
      while (stamps.length && now - stamps[0] > RATE_WINDOW_MS) stamps.shift();
      if (stamps.length < 2) return null;
      // Divide by the span actually observed, not the full window — else a
      // 10 Hz topic reads 2 Hz one second in and only ramps to truth at 5 s.
      const spanMs = Math.min(RATE_WINDOW_MS, now - (firstTick ?? now));
      if (spanMs <= 0) return null;
      return stamps.length / (spanMs / 1000);
    },
  };
}

/**
 * @param {HTMLElement} root the sidebar container.
 * @param {ReturnType<typeof import("./navStore.js").createNavStore>} store nav-state
 *   rows (mode, active map) read from the shared store rather than duplicate
 *   topic subscriptions.
 * @returns {{ destroy: () => void }}
 */
export function createNavPanels(root, store) {
  /** @param {string} title @param {string} [hint] tooltip on the panel label @returns {{ row: (label: string, topic?: string) => HTMLElement }} */
  function panel(title, hint = "") {
    const section = document.createElement("section");
    section.className = "nav-panel";
    const label = document.createElement("p");
    label.className = "microlabel";
    label.textContent = title;
    if (hint) label.title = hint;
    section.appendChild(label);
    root.appendChild(section);
    return {
      /** @param {string} name @param {string} [topic] tooltip on the label: where the value comes from */
      row(name, topic = "") {
        const row = document.createElement("div");
        row.className = "nav-row";
        const key = document.createElement("span");
        key.className = "nav-row-label";
        key.textContent = name;
        // On the label, not the row — the value span carries its own titles
        // (e.g. the localization hint) and must not be shadowed.
        if (topic) key.title = topic;
        const value = document.createElement("span");
        value.className = "nav-row-value mono";
        value.textContent = DASH;
        row.append(key, value);
        section.appendChild(row);
        return value;
      },
    };
  }

  const pose = panel("Pose");
  const mapXY = pose.row("map x · y", AMCL_POSE_TOPIC);
  const mapYaw = pose.row("map heading", AMCL_POSE_TOPIC);
  const odomXY = pose.row("odom x · y", ODOM_TOPIC);
  const odomYaw = pose.row("odom heading", ODOM_TOPIC);

  const vel = panel("Velocity");
  const velActual = vel.row("measured v · ω", `${ODOM_TOPIC} (differentiated)`);
  const velCmd = vel.row("commanded v · ω", CMD_VEL_TOPIC);

  const lidar = panel("Lidar");
  const scanPoints = lidar.row("points", SCAN_TOPIC);
  const scanNearest = lidar.row("nearest", SCAN_TOPIC);

  const nav = panel("Nav state");
  const navMode = nav.row("mode", NAV_CURRENT_MODE_TOPIC);
  const navMap = nav.row("map", NAV_CURRENT_MAP_TOPIC);
  const navLoc = nav.row("localization", `${AMCL_POSE_TOPIC} (covariance) · ${LOCALIZATION_STATUS_TOPIC}`);
  const battery = nav.row("battery", BATTERY_STATE_TOPIC);

  // Localization health: grid_localizer's latched /localization/status is the
  // verdict (it watches the lidar against the map and says "lost" while the
  // robot's pose no longer explains the scan). AMCL's pose covariance only
  // refines a "localized" verdict — the particle filter stays tightly
  // converged on a stale place when the robot is carried off, so on its own
  // it reads "confident" exactly when the robot is lost.
  // A mislocalized robot is exactly what makes goals abort with "start in
  // lethal space", so this must be visible, not log-only.
  const CONFIDENT_VAR = 0.1; // m², same threshold as the mobile app
  const UNCERTAIN_HINT = "The robot may not be where the map thinks — use Locate (or place manually), or remap.";

  /** @type {Record<string, [string, string, string]>} */
  const LOC_STATES = {
    processing_map: ["processing map…", "", ""],
    localized: ["localized", "ok", ""],
    localized_low_confidence: ["low confidence", "warn", UNCERTAIN_HINT],
    lost: ["lost", "fail", "The lidar no longer matches the map at the robot's position — it is searching the map for itself."],
    error: ["error", "fail", "Localization failed — see robot logs, then use Locate or Manual placement."],
  };
  /** grid_localizer's latest verdict; "" until it has spoken */
  let locStatus = "";
  let inNavigation = true;

  // Outside navigation mode the localizer is deactivated, so its last verdict
  // is stale: show a dash, and the verdict again once navigation resumes (the
  // latched topic does not replay on a mode change).
  function renderLocalization() {
    if (!inNavigation || !locStatus) return setLocalization(DASH, "", "");
    const [text, kind, hint] = LOC_STATES[locStatus] ?? [locStatus, "", ""];
    setLocalization(text, kind, hint);
  }

  /** @param {string} text @param {string} kind @param {string} hint */
  function setLocalization(text, kind, hint) {
    navLoc.textContent = text;
    navLoc.className = `nav-row-value mono${kind ? ` ${kind}` : ""}`;
    navLoc.title = hint;
  }

  const rates = panel("Received rates", "Message arrival rate per topic, averaged over a 5 s sliding window");

  // ---- subscriptions -------------------------------------------------------
  /** @type {Array<() => void>} */
  const unsubs = [];
  /** @type {Array<{ label: string, value: HTMLElement, rate: ReturnType<typeof makeRate> }>} */
  const rateRows = [];

  /**
   * Subscribe + count arrivals for the rates table in one go.
   * @param {string} topic @param {(msg: any) => void} handler
   * @param {number} [throttle] @param {string} [type]
   */
  function watch(topic, handler, throttle, type) {
    const rate = makeRate();
    rateRows.push({ label: topic, value: rates.row(topic), rate });
    unsubs.push(
      ros.subscribe(
        topic,
        (msg) => {
          rate.tick();
          handler(msg);
        },
        throttle,
        type,
      ),
    );
  }

  // Measured motion is differentiated from the pose — Odometry.twist is never
  // populated by this robot (see odomVelocity.js), so reading it would peg
  // this readout at a permanent 0.00 m/s.
  const velTracker = createVelocityTracker();

  watch(ODOM_TOPIC, (msg) => {
    const p = msg?.pose?.pose?.position;
    const yaw = yawOf(msg?.pose?.pose?.orientation);
    if (typeof p?.x === "number" && typeof p?.y === "number") odomXY.textContent = `${p.x.toFixed(2)}, ${p.y.toFixed(2)} m`;
    if (yaw !== null) odomYaw.textContent = `${deg(yaw).toFixed(0)}°`;
    const measured = velTracker.update(msg);
    // null is the tracker's "unknown" (first sample, or resuming after a
    // gap) — show the dash rather than holding a stale velocity.
    velActual.textContent = measured ? `${measured.v.toFixed(2)} m/s · ${deg(measured.w).toFixed(0)}°/s` : DASH;
  }, 100);

  watch(AMCL_POSE_TOPIC, (msg) => {
    const p = msg?.pose?.pose?.position;
    const yaw = yawOf(msg?.pose?.pose?.orientation);
    if (typeof p?.x === "number" && typeof p?.y === "number") mapXY.textContent = `${p.x.toFixed(2)}, ${p.y.toFixed(2)} m`;
    if (yaw !== null) mapYaw.textContent = `${deg(yaw).toFixed(0)}°`;
    const cov = msg?.pose?.covariance;
    if (inNavigation && locStatus === "localized" && Array.isArray(cov) && cov.length >= 36) {
      const maxVar = Math.max(cov[0], cov[7]); // x/y position variance
      const detail = `position variance ${maxVar.toFixed(2)} m²`;
      if (maxVar > CONFIDENT_VAR) setLocalization("converging", "warn", `${detail} — ${UNCERTAIN_HINT}`);
      else setLocalization("localized", "ok", detail);
    }
  }, 0, "geometry_msgs/msg/PoseWithCovarianceStamped");

  watch(CMD_VEL_TOPIC, (msg) => {
    const v = msg?.linear?.x;
    const w = msg?.angular?.z;
    if (typeof v === "number" && typeof w === "number") {
      velCmd.textContent = `${v.toFixed(2)} m/s · ${deg(w).toFixed(0)}°/s`;
    }
  }, 100, "geometry_msgs/msg/Twist");

  watch(SCAN_TOPIC, (msg) => {
    const ranges = msg?.ranges;
    if (!Array.isArray(ranges)) return;
    let count = 0;
    let best = Infinity;
    let bestI = -1;
    for (let i = 0; i < ranges.length; i++) {
      const r = ranges[i];
      if (!Number.isFinite(r) || r < msg.range_min || r > msg.range_max) continue;
      count++;
      if (r < best) {
        best = r;
        bestI = i;
      }
    }
    scanPoints.textContent = `${count} / ${ranges.length}`;
    scanNearest.textContent =
      bestI >= 0 ? `${best.toFixed(2)} m @ ${deg(msg.angle_min + bestI * msg.angle_increment).toFixed(0)}°` : DASH;
  }, 150, "sensor_msgs/msg/LaserScan");

  // /map barely changes — it's in the rates table for liveness, not content.
  watch(MAP_TOPIC, () => {}, 250);

  unsubs.push(
    store.onChange((s) => {
      if (s.mode) navMode.textContent = s.mode;
      if (s.currentMap) navMap.textContent = s.currentMap;
      if (s.mode) {
        inNavigation = s.mode === "navigation";
        renderLocalization();
      }
    }),
    ros.subscribe(LOCALIZATION_STATUS_TOPIC, (msg) => {
      if (typeof msg?.data !== "string" || !msg.data) return;
      locStatus = msg.data;
      renderLocalization();
    }, 0, "std_msgs/msg/String"),
    ros.subscribe(BATTERY_STATE_TOPIC, (msg) => {
      const p = msg?.percentage;
      if (typeof p !== "number" || Number.isNaN(p)) return;
      // The robot publishes the spec's 0–1 (battery.py get_percentage returns
      // percentage/100), so <=1 must scale up: 1.0 is full, not 1%. Values >1
      // pass through in case a 0–100 source ever appears.
      const pct = p <= 1 ? p * 100 : p;
      const volts = typeof msg?.voltage === "number" && msg.voltage > 0 ? ` · ${msg.voltage.toFixed(1)} V` : "";
      battery.textContent = `${Math.round(pct)}%${volts}`;
    }, 1000),
  );

  const rateTimer = setInterval(() => {
    for (const { value, rate } of rateRows) {
      const hz = rate.hz();
      value.textContent = hz === null ? DASH : `${hz.toFixed(1)} Hz`;
    }
  }, 1000);

  return {
    destroy() {
      clearInterval(rateTimer);
      for (const unsub of unsubs) unsub();
      root.innerHTML = "";
    },
  };
}
