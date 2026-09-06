// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Telemetry strip — robot name, battery, link state. Battery comes from
// sensor_msgs/BatteryState at 0.2 Hz; name/version ride /robot/info's
// JSON-in-String payload.

import { BATTERY_STATE_TOPIC, ROBOT_INFO_TOPIC, WEBSOCKET_STATUS_TOPIC } from "../constants.js";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createTelemetry(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "telemetry";

  const name = item("robot", "robot", "—", ROBOT_INFO_TOPIC);
  const battery = item("battery", "battery", "—", BATTERY_STATE_TOPIC);
  const link = item("link", "link", "—", "this browser's rosbridge websocket to the robot — live means the page is receiving telemetry");
  const agent = item("agent", "agent", "—", `whether the brain can reach its model backend, i.e. whether the agent will run — ${WEBSOCKET_STATUS_TOPIC}`);
  wrap.append(name.el, battery.el, link.el, agent.el);
  parent.appendChild(wrap);
  const items = [name, battery, link, agent];
  /** @type {number | null} */
  let measureFrame = null;

  /**
   * @param {TelemetryKey} key
   * @param {string} labelText
   * @param {string} initial
   * @param {string} [title]
   */
  function item(key, labelText, initial, title = "") {
    const el = document.createElement("div");
    el.className = `telemetry-item telemetry-item-${key}`;
    if (title) el.title = title;
    const status = document.createElement("span");
    status.className = "telemetry-status-dot";
    status.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "microlabel";
    label.textContent = labelText;
    const value = document.createElement("span");
    value.className = "telemetry-value mono";
    value.title = initial;
    const meta = document.createElement("span");
    meta.className = "telemetry-meta mono";
    const track = document.createElement("span");
    track.className = "telemetry-value-track";
    const primary = document.createElement("span");
    primary.textContent = initial;
    const duplicate = document.createElement("span");
    duplicate.className = "telemetry-value-duplicate";
    duplicate.textContent = initial;
    duplicate.setAttribute("aria-hidden", "true");
    track.append(primary, duplicate);
    value.append(track);
    el.append(status, label, value, meta);
    return { el, value, primary, duplicate, meta };
  }

  /**
   * @param {ReturnType<typeof item>} target
   * @param {string} text
   * @param {TelemetryTone} [tone]
   * @param {string} [meta]
   */
  function update(target, text, tone = "", meta = "") {
    renderValue(target, text, tone, meta);
  }

  /**
   * @param {ReturnType<typeof item>} target
   * @param {string} text
   * @param {TelemetryTone} tone
   * @param {string} meta
   */
  function renderValue(target, text, tone, meta) {
    target.primary.textContent = text;
    target.duplicate.textContent = text;
    target.meta.textContent = meta;
    target.value.title = meta ? `${text} · ${meta}` : text;
    target.el.classList.toggle("live", tone === "live");
    target.el.classList.toggle("warn", tone === "warn");
    scheduleOverflowMeasure();
  }

  function scheduleOverflowMeasure() {
    if (measureFrame !== null) cancelAnimationFrame(measureFrame);
    measureFrame = requestAnimationFrame(() => {
      measureFrame = null;
      for (const target of items) {
        const overflow = Math.ceil(target.primary.scrollWidth - target.value.clientWidth);
        target.value.classList.toggle("overflowing", overflow > 1);
        target.value.style.setProperty("--telemetry-duration", `${Math.max(5, (target.primary.scrollWidth + 24) / 28)}s`);
      }
    });
  }

  const resizeObserver = new ResizeObserver(scheduleOverflowMeasure);
  resizeObserver.observe(wrap);
  scheduleOverflowMeasure();

  const unsubs = [
    rosClient.subscribe(ROBOT_INFO_TOPIC, (payload) => {
      if (typeof payload?.data !== "string") return;
      /** @type {RobotInfo} */
      let info;
      try {
        info = JSON.parse(payload.data);
      } catch {
        return;
      }
      const label = info.robot_name || info.hostname;
      if (label) {
        update(name, label, "", info.version ? `v${info.version}` : "");
      }
    }, undefined, "std_msgs/msg/String"),
    rosClient.onStateChange((state) => {
      const text = state === "connected" ? "live" : state;
      update(link, text, state === "connected" ? "live" : state === "disconnected" ? "warn" : "");
    }),
    rosClient.subscribe(
      WEBSOCKET_STATUS_TOPIC,
      (payload) => {
        if (typeof payload?.data !== "string") return;
        let s;
        try {
          s = JSON.parse(payload.data);
        } catch {
          return;
        }
        const state = String(s?.state ?? "");
        let text = state || "—";
        let ok = false;
        let warn = false;
        // Where the backend runs is not what the operator needs from this
        // readout -- only whether starting the agent will work right now.
        if (s?.connected === true) {
          text = "ready";
          ok = true;
        } else if (["connecting", "authenticating", "starting", "configured"].includes(state)) {
          text = "connecting";
          warn = true;
        } else if (state === "invalid_config") {
          text = "no key";
          warn = true;
        } else if (["connection_error", "backend_error", "disconnected", "error", "stopped"].includes(state)) {
          text = "offline";
          warn = true;
        }
        update(agent, text, ok ? "live" : warn ? "warn" : "");
      },
      500,
      "std_msgs/msg/String",
    ),
  ];

  unsubs.push(
    rosClient.subscribe(
      BATTERY_STATE_TOPIC,
      (/** @type {BatteryStateMsg} */ msg) => {
        const p = msg?.percentage;
        if (typeof p !== "number" || Number.isNaN(p)) return;
        // The robot publishes 0–100; tolerate a spec-compliant 0–1 source.
        const pct = p <= 1 ? p * 100 : p;
        update(battery, `${Math.round(pct)}%`, pct <= 15 ? "warn" : "");
      },
      1000,
      "sensor_msgs/msg/BatteryState",
    ),
  );

  return {
    destroy() {
      if (measureFrame !== null) cancelAnimationFrame(measureFrame);
      resizeObserver.disconnect();
      for (const unsub of unsubs) unsub();
      wrap.remove();
    },
  };
}

/** @typedef {"robot" | "battery" | "link" | "agent"} TelemetryKey */
/** @typedef {"" | "live" | "warn"} TelemetryTone */
