// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Leader-arm panel — plug the leader arm into THIS computer's USB, click
// connect once (Chromium remembers the grant; later sessions auto-attach),
// watch the joint dots follow the physical arm, then ENGAGE to publish.
//
// While engaged, every position round goes to /leader_positions over the
// shared rosbridge socket — the same Int32MultiArray path the mobile app's
// non-UDP fallback uses; the robot's mars_app converts it to arm commands.
// Engage is an explicit step because the follower arm snaps to the leader's
// pose the moment data flows. Auto-disengage on tab hide, rosbridge loss,
// or serial failure.

import { DynamixelLeader } from "../dynamixel.js";
import { copyToButton, ICON_COPY } from "../clipboard.js";
import {
  LEADER_POSITIONS_TOPIC,
  ARM_REBOOT_CONFIRM,
  ARM_TORQUE_ON_SERVICE,
  ARM_TORQUE_OFF_SERVICE,
  ARM_STATUS_TOPIC,
} from "../constants.js";
import { rebootArmAndEnableTorque } from "../armReboot.js";

const PUBLISH_MIN_GAP_MS = 15;
const TICK_CENTER = 2048;
const TICK_SPAN = 2048; // ±half a revolution shown on the joint dots

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{ onState?: (s: { engaged: boolean, reading: boolean, rate: number }) => void, hideServices?: boolean }} [opts]
 *   onState reports the leader-arm link on every change — used by the Collect
 *   page to gate recording the way the mobile app gates on arm publishing + rate.
 *   hideServices drops the reboot/torque row (sim: the services are no-ops and
 *   /mars/arm/status never publishes).
 * @returns {{ destroy: () => void }}
 */
export function createArmPanel(parent, rosClient, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "arm-panel";

  // Collapsible header. On a phone the arm controls are mostly dead weight
  // (no WebSerial for the leader arm), so the whole panel starts collapsed and
  // the operator taps to reveal it. On desktop the header is hidden (CSS) and
  // the panel is always open — unchanged.
  const header = document.createElement("button");
  header.type = "button";
  header.className = "arm-header";
  header.title = "Show/hide the leader-arm controls";
  header.innerHTML =
    '<span class="microlabel">arm</span>' +
    '<svg class="arm-caret" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';
  wrap.appendChild(header);

  let collapsed = window.matchMedia("(max-width: 640px)").matches;
  function applyCollapsed() {
    wrap.classList.toggle("collapsed", collapsed);
    header.setAttribute("aria-expanded", String(!collapsed));
  }
  header.addEventListener("click", () => {
    collapsed = !collapsed;
    applyCollapsed();
  });
  applyCollapsed();

  const label = document.createElement("p");
  label.className = "microlabel";
  label.textContent = "leader arm";
  wrap.appendChild(label);
  parent.appendChild(wrap);

  // Robot-arm services (reboot + torque toggle) — rosbridge calls independent
  // of the leader-arm USB link, so they're available even where WebSerial is not.
  const armSvc = opts.hideServices ? null : buildArmServices(rosClient);

  const serial = navigator.serial;
  if (!serial) {
    const hint = document.createElement("p");
    hint.className = "arm-hint";
    /** @type {HTMLElement[]} */
    const extras = [];
    if (!window.isSecureContext) {
      // WebSerial needs a secure origin. The robot serves the same app over
      // HTTPS (self-signed), so point the operator there — the cert warning is
      // a one-time click-through. (Localhost is already secure, so plain http
      // only happens off-box, which is exactly where this redirect helps.)
      hint.textContent = "Leader arm needs HTTPS — switch over and continue past the certificate warning.";
      const switchBtn = document.createElement("button");
      switchBtn.className = "arm-button";
      switchBtn.type = "button";
      switchBtn.textContent = "Switch to HTTPS";
      switchBtn.title = "Reload over HTTPS — WebSerial needs a secure origin";
      switchBtn.addEventListener("click", () => {
        const url = new URL(location.href);
        url.protocol = "https:";
        url.port = ""; // 80 -> default 443; the TLS front door listens there
        location.href = url.href;
      });
      extras.push(switchBtn);
    } else {
      hint.textContent = "Needs Chrome or Edge (WebSerial).";
    }
    wrap.append(hint, ...extras, ...(armSvc ? [divider(), armSvc.el] : []));
    // No leader-arm link possible here — tell the gate it will never be ready.
    opts.onState?.({ engaged: false, reading: false, rate: 0 });
    return {
      destroy() {
        armSvc?.destroy();
        wrap.remove();
      },
    };
  }

  // ---- DOM ------------------------------------------------------------

  const status = document.createElement("p");
  status.className = "arm-status mono";
  status.title = "Leader-arm read rate over USB";

  const joints = document.createElement("div");
  joints.className = "arm-joints";
  joints.title = "Live leader-arm joint positions (Dynamixel ticks, ±half turn)";
  /** @type {HTMLElement[]} */
  const dots = [];
  for (let i = 0; i < 6; i++) {
    const joint = document.createElement("div");
    joint.className = "arm-joint";
    const dot = document.createElement("div");
    dot.className = "arm-joint-dot";
    joint.appendChild(dot);
    joints.appendChild(joint);
    dots.push(dot);
  }

  const connectBtn = document.createElement("button");
  connectBtn.className = "arm-button";
  connectBtn.type = "button";
  connectBtn.textContent = "Connect arm";
  connectBtn.title = "Pick the leader arm's USB serial port (WebSerial)";

  const engageBtn = document.createElement("button");
  engageBtn.className = "arm-button arm-engage";
  engageBtn.type = "button";
  engageBtn.title = `the follower mirrors the leader live — ${LEADER_POSITIONS_TOPIC}`;

  // Copy the live joint ticks — handy for pasting a pose into a skill.
  const copyBtn = document.createElement("button");
  copyBtn.className = "arm-button arm-copy";
  copyBtn.type = "button";
  copyBtn.title = "Copy joint positions";
  copyBtn.setAttribute("aria-label", "Copy joint positions");
  copyBtn.innerHTML = ICON_COPY;

  // Engage + copy share a row; the row (not the button) is what hides when
  // there's nothing to read.
  const engageRow = document.createElement("div");
  engageRow.className = "arm-engage-row";
  engageRow.append(engageBtn, copyBtn);

  const note = document.createElement("p");
  note.className = "arm-note microlabel";

  wrap.append(status, joints, connectBtn, engageRow, note, ...(armSvc ? [divider(), armSvc.el] : []));

  // ---- state ------------------------------------------------------------

  let engaged = false;
  let lastPublishAt = 0;
  let destroyed = false;
  const leader = new DynamixelLeader();

  /** @param {boolean} on */
  function setEngaged(on) {
    if (engaged === on) return;
    engaged = on;
    render(leader.state);
  }

  /** @param {number[]} positions */
  function publish(positions) {
    const now = performance.now();
    if (now - lastPublishAt < PUBLISH_MIN_GAP_MS) return;
    lastPublishAt = now;
    rosClient.publish(LEADER_POSITIONS_TOPIC, {
      layout: { dim: [], data_offset: 0 },
      data: positions,
    });
  }

  /** @param {LeaderArmState} state */
  function render(state) {
    const reading = state.connected && state.positions !== null;

    if (state.error) {
      status.textContent = state.error;
      status.classList.add("warn");
    } else if (!state.connected) {
      status.textContent = "no arm connected";
      status.classList.remove("warn");
    } else {
      status.textContent = reading ? `${state.rate} Hz` : "listening…";
      status.classList.remove("warn");
    }

    joints.hidden = !reading;
    if (state.positions) {
      state.positions.forEach((tick, i) => {
        const frac = Math.max(-1, Math.min(1, (tick - TICK_CENTER) / TICK_SPAN));
        dots[i].style.top = `${(1 - (frac + 1) / 2) * 100}%`;
      });
    }

    connectBtn.hidden = state.connected;
    connectBtn.disabled = false;
    connectBtn.textContent = state.error ? "Reconnect arm" : "Connect arm";

    engageRow.hidden = !reading;
    engageBtn.textContent = engaged ? "Live — click to stop" : "Engage follow";
    engageBtn.classList.toggle("active", engaged);

    note.hidden = !reading || engaged;
    note.textContent = "follower snaps to leader pose";

    // The Collect gate mirrors the mobile app's isArmPublishing && rate > 0.
    opts.onState?.({ engaged, reading, rate: state.rate });
  }

  // ---- wiring -----------------------------------------------------------

  /** @param {SerialPort} port */
  async function openPort(port) {
    connectBtn.disabled = true;
    connectBtn.textContent = "Opening…";
    try {
      await leader.open(port);
    } catch (err) {
      render({
        ...leader.state,
        error: err instanceof Error ? err.message : "Couldn't open port",
      });
    }
  }

  connectBtn.addEventListener("click", async () => {
    try {
      // QinHeng CH9102 USB-serial bridge on the leader arm ("USB Single
      // Serial") — filtering narrows the chooser to just it.
      const port = await serial.requestPort({
        filters: [{ usbVendorId: 0x1a86, usbProductId: 0x55d3 }],
      });
      await openPort(port);
    } catch {
      render(leader.state); // chooser dismissed — not an error
    }
  });

  engageBtn.addEventListener("click", () => setEngaged(!engaged));

  copyBtn.addEventListener("click", () => {
    const positions = leader.state.positions;
    if (!positions) return; // row is hidden without a read, but be safe
    void copyToButton(`[${positions.join(", ")}]`, copyBtn, "copied");
  });

  const unsubLeader = leader.onChange((state) => {
    if (destroyed) return;
    if (engaged && (!state.connected || state.error)) {
      engaged = false;
    } else if (engaged && state.positions && rosClient.state === "connected") {
      publish(state.positions);
    }
    render(state);
  });

  const unsubRos = rosClient.onStateChange((rosState) => {
    if (rosState !== "connected") setEngaged(false);
  });

  const onVisibility = () => {
    if (document.visibilityState === "hidden") setEngaged(false);
  };
  document.addEventListener("visibilitychange", onVisibility);

  // Previously granted port (or one plugged in later) attaches by itself —
  // returning operators never touch the chooser again.
  const onSerialConnect = (/** @type {Event} */ e) => {
    if (!leader.state.connected && e.target) {
      void openPort(/** @type {SerialPort} */ (e.target));
    }
  };
  serial.addEventListener("connect", onSerialConnect);
  void serial.getPorts().then((ports) => {
    if (!destroyed && ports[0] && !leader.state.connected) void openPort(ports[0]);
  });

  return {
    destroy() {
      destroyed = true;
      setEngaged(false);
      unsubLeader();
      unsubRos();
      document.removeEventListener("visibilitychange", onVisibility);
      serial.removeEventListener("connect", onSerialConnect);
      armSvc?.destroy();
      void leader.close();
      wrap.remove();
    },
  };
}

/** A hairline separator between the leader-arm controls and the reboot row. */
function divider() {
  const el = document.createElement("div");
  el.className = "arm-divider";
  return el;
}

/**
 * Robot-arm service controls, mirroring the mobile app's DevTab: a Reboot
 * button and a live Torque ON/OFF toggle side by side. All over the shared
 * rosbridge socket (no WebSerial). Torque state is read from /mars/arm/status
 * (ArmStatus, ~0.2 Hz) so the toggle reflects reality even when changed
 * elsewhere; clicking it calls torque_on/torque_off. Reboot power-cycles the
 * servos, then re-enables torque automatically (see armReboot.js).
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ el: HTMLElement, destroy: () => void }}
 */
function buildArmServices(rosClient) {
  const box = document.createElement("div");
  box.className = "arm-services";

  const heading = document.createElement("p");
  heading.className = "microlabel";
  heading.textContent = "robot arm";

  const actions = document.createElement("div");
  actions.className = "arm-actions";

  const rebootBtn = document.createElement("button");
  rebootBtn.className = "arm-button";
  rebootBtn.type = "button";
  rebootBtn.title = "Reboot the follower arm's servos, then torque back on";

  const torqueBtn = document.createElement("button");
  torqueBtn.className = "arm-button arm-torque";
  torqueBtn.type = "button";
  torqueBtn.title = `${ARM_TORQUE_ON_SERVICE} / ${ARM_TORQUE_OFF_SERVICE} — state from ${ARM_STATUS_TOPIC}`;
  torqueBtn.innerHTML =
    '<span class="arm-torque-cap">Torque</span><span class="arm-torque-state"></span>';
  const torqueState = /** @type {HTMLElement} */ (torqueBtn.querySelector(".arm-torque-state"));

  actions.append(rebootBtn, torqueBtn);

  const msg = document.createElement("p");
  msg.className = "arm-note microlabel";
  msg.hidden = true;

  box.append(heading, actions, msg);

  /** @type {boolean | null} */
  let torqueOn = null; // unknown until the first /mars/arm/status
  let rebooting = false;
  let toggling = false;
  /** @type {number | undefined} */
  let resetTimer;

  /**
   * @param {string} text
   * @param {boolean} warn
   */
  function flash(text, warn) {
    clearTimeout(resetTimer);
    msg.textContent = text;
    msg.classList.toggle("warn", warn);
    msg.hidden = false;
    resetTimer = setTimeout(() => {
      msg.hidden = true;
    }, 4000);
  }

  function render() {
    const connected = rosClient.state === "connected";
    const busy = rebooting || toggling;

    rebootBtn.disabled = busy || !connected;
    rebootBtn.textContent = rebooting ? "Rebooting…" : "Reboot";

    // The arm is limp throughout a reboot regardless of the last status.
    const effectiveOn = rebooting ? false : torqueOn;
    torqueBtn.disabled = busy || !connected || torqueOn === null;
    torqueBtn.classList.toggle("on", effectiveOn === true);
    torqueBtn.classList.toggle("off", effectiveOn === false);
    // Reflect the (optimistic) state immediately — no "…" wait. The button is
    // briefly disabled while the call is in flight; /mars/arm/status confirms.
    torqueState.textContent = effectiveOn === null ? "—" : effectiveOn ? "ON" : "OFF";
  }

  rebootBtn.addEventListener("click", async () => {
    if (rebooting || toggling) return;
    if (!window.confirm(ARM_REBOOT_CONFIRM)) {
      return;
    }
    rebooting = true;
    msg.hidden = true;
    render();
    try {
      const res = await rebootArmAndEnableTorque(rosClient);
      if (res.torqueOn) torqueOn = true;
      flash(res.message, !res.ok || !res.torqueOn);
    } catch (err) {
      flash(err instanceof Error ? err.message : "Reboot failed", true);
    } finally {
      rebooting = false;
      render();
    }
  });

  torqueBtn.addEventListener("click", async () => {
    if (rebooting || toggling || torqueOn === null) return;
    const turnOn = !torqueOn;
    const prev = torqueOn;
    // Optimistic: show the new state right away, then confirm/revert on the
    // service result. The robot's torque_on walks 6 servos (~600 ms) before it
    // replies, so waiting for the reply felt laggy.
    torqueOn = turnOn;
    toggling = true;
    render();
    try {
      const res = await rosClient.callService(
        turnOn ? ARM_TORQUE_ON_SERVICE : ARM_TORQUE_OFF_SERVICE,
        {},
      );
      if (res && res.success === false) {
        torqueOn = prev; // revert — the robot rejected it
        flash(res.message || "Torque toggle failed", true);
      } else {
        torqueOn = turnOn; // re-assert in case a stale status arrived mid-call
      }
    } catch (err) {
      torqueOn = prev; // revert on timeout / disconnect
      flash(err instanceof Error ? err.message : "Torque toggle failed", true);
    } finally {
      toggling = false;
      render();
    }
  });

  // Live torque state from the robot — also catches changes made by the reboot
  // or by the mobile app, so the toggle never drifts out of sync.
  const unsubStatus = rosClient.subscribe(ARM_STATUS_TOPIC, (m) => {
    if (m && typeof m.is_torque_enabled === "boolean") {
      torqueOn = m.is_torque_enabled;
      if (!toggling && !rebooting) render();
    }
  }, undefined, "mars_msgs/msg/ArmStatus");

  const unsubState = rosClient.onStateChange(render);
  render();

  return {
    el: box,
    destroy() {
      clearTimeout(resetTimer);
      unsubStatus();
      unsubState();
      box.remove();
    },
  };
}
