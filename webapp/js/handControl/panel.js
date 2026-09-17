// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The camera arm-control panel: your hand on the left, the claw on the robot.
// One component for both hosts -- a card on the Arm SDK page and a floating HUD
// over the teleop video -- because the interaction is the same and only the
// frame around it differs. The engine (session.js) owns everything that touches
// the robot; this file is the preview, the status and three buttons.

import { claimArmControl, armControlHolder, onArmControlChange } from "../armControlLock.js";
import { createHandControl } from "./session.js";

export const ARM_CONTROL_NAME = "Camera control";

const STYLE_ID = "hand-control-style";
const CSS = `
.handctl { display: flex; flex-direction: column; gap: 10px; }
.handctl-head { display: flex; align-items: center; gap: 8px; }
.handctl-head h2 { font-size: 11px; margin: 0; color: var(--muted); text-transform: uppercase;
  letter-spacing: .09em; font-weight: 600; }
.handctl-head .spacer { flex: 1; }
.handctl-pill { padding: 3px 10px; border-radius: 999px; font-size: 11px; border: 1px solid var(--hairline-strong);
  color: var(--muted); font-family: var(--mono, ui-monospace, monospace); white-space: nowrap; }
.handctl-pill.on { color: var(--ok); border-color: rgb(86 194 140 / 55%); }
.handctl-pill.warn { color: var(--danger); border-color: rgb(217 106 90 / 55%); }
.handctl-pill.live { color: var(--accent); border-color: var(--accent-dim); }

.handctl-frame { position: relative; aspect-ratio: 4 / 3; width: 100%; border-radius: 11px; overflow: hidden;
  background: #101216; border: 1px solid var(--hairline); display: grid; place-items: center; }
.handctl-frame video, .handctl-frame canvas { position: absolute; inset: 0; width: 100%; height: 100%;
  object-fit: cover; transform: scaleX(-1); }
.handctl-frame video { opacity: 0; transition: opacity 260ms ease; }
.handctl-frame.on video { opacity: 1; }
.handctl-placeholder { position: relative; z-index: 1; display: flex; flex-direction: column; align-items: center;
  gap: 8px; color: var(--muted); font-size: 12px; text-align: center; padding: 0 20px; line-height: 1.5; }
.handctl-frame.on .handctl-placeholder { display: none; }
.handctl-placeholder svg { width: 40px; height: 40px; stroke: currentColor; fill: none; stroke-width: 1.6;
  stroke-linecap: round; stroke-linejoin: round; opacity: .65; }

.handctl-hold { position: absolute; inset: 0; z-index: 2; display: none; place-content: center;
  justify-items: center; gap: 10px; background: rgb(10 12 15 / 55%); backdrop-filter: blur(3px); color: var(--text);
  font-size: 12px; letter-spacing: .02em; text-align: center; padding: 0 18px 30px; }
.handctl-hold.show { display: grid; }
.handctl-hold svg { width: 44px; height: 44px; }
.handctl-hold circle { fill: none; stroke-width: 3; }
.handctl-hold .track { stroke: rgb(255 255 255 / 18%); }
.handctl-hold .bar { stroke: var(--accent); stroke-linecap: round; stroke-dasharray: 126;
  transform: rotate(-90deg); transform-origin: 50% 50%; transition: stroke-dashoffset 90ms linear; }

.handctl-meters { position: absolute; left: 8px; right: 8px; bottom: 8px; z-index: 3; display: flex; gap: 6px;
  row-gap: 4px; flex-wrap: wrap; align-items: center; pointer-events: none; }
.handctl-meters span { background: var(--glass); border: 1px solid var(--hairline); border-radius: 7px;
  padding: 2px 8px; font-family: var(--mono, ui-monospace, monospace); font-size: 10.5px; color: var(--text);
  backdrop-filter: blur(6px); white-space: nowrap; }
.handctl-meters b { color: var(--muted); font-weight: 500; margin-right: 5px; }
.handctl-meters .handctl-jaw { display: flex; align-items: center; gap: 6px; padding-right: 6px; }
.handctl-grip { width: 46px; height: 5px; border-radius: 999px; background: rgb(255 255 255 / 14%);
  overflow: hidden; }
.handctl-grip i { display: block; height: 100%; background: var(--accent); width: 0; transition: width 90ms linear; }

.handctl-feedback { font-size: 12px; color: var(--text); line-height: 1.45; min-height: 2.6em; }
.handctl-error { font-size: 12px; color: var(--danger); line-height: 1.45; }
.handctl-error[hidden] { display: none; }
.handctl-actions { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }
.handctl button { background: var(--panel); color: var(--text); border: 1px solid var(--hairline-strong);
  border-radius: 8px; padding: 7px 12px; font: inherit; font-size: 12.5px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: border-color 120ms ease, color 120ms ease, background 120ms ease; }
.handctl button:hover:not(:disabled) { border-color: var(--accent-dim); color: var(--accent); }
.handctl button:disabled { opacity: .38; cursor: not-allowed; }
.handctl button svg { width: 13px; height: 13px; fill: currentColor; }
.handctl button.primary { border-color: rgb(86 194 140 / 45%); color: var(--ok); }
.handctl button.primary:hover:not(:disabled) { border-color: var(--ok); background: rgb(86 194 140 / 8%); }
.handctl button.primary.live { border-color: var(--accent); color: var(--accent); background: var(--accent-faint); }
.handctl-tune { display: flex; align-items: center; gap: 8px; font-size: 11.5px; color: var(--muted); }
.handctl-tune input[type="range"] { flex: 1; accent-color: var(--accent); min-width: 80px; }
.handctl-hint { font-size: 11px; color: var(--muted); line-height: 1.5; }
.handctl-hint kbd { border: 1px solid var(--hairline-strong); border-radius: 4px; padding: 0 4px;
  background: var(--bg); font-family: inherit; font-size: 10px; }

.handctl-close { background: none !important; border: none !important; color: var(--muted) !important;
  padding: 2px 4px !important; font-size: 15px !important; line-height: 1; }
.handctl-close:hover { color: var(--text) !important; }

/* Over the teleop video the arm overlay grows upward to hold the preview, so
   the panel sheds what the Arm SDK page has room for: the inference meter, the
   taller preview, the roomier buttons. */
.overlay-arm { max-width: calc(100vw - 28px); }
.overlay-arm > .handctl { width: 288px; padding: 12px 14px 2px; }
.overlay-arm > .handctl + .arm-panel { border-top: 1px solid var(--hairline); padding-top: 12px; }
.overlay-arm .handctl-frame { aspect-ratio: 16 / 10; }
.overlay-arm .handctl-roomy { display: none !important; }
.overlay-arm .handctl button { padding: 6px 9px; font-size: 11.5px; }
.overlay-arm .handctl-feedback { min-height: 3.4em; }
.arm-camera.active { border-color: var(--ctl-edge-accent); color: var(--ctl-accent); }
`;

const ICON_CAMERA =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="3" y="6" width="12" height="12" rx="3"/><path d="m15 10 6-3v10l-6-3"/></svg>';
const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="m9 5 11 7-11 7Z"/></svg>';
const ICON_STOP = '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
const HAND_ART =
  '<svg viewBox="0 0 120 120"><path d="M37 67V35c0-8 10-8 10 0v23-35c0-8 10-8 10 0v35-39c0-8 10-8 10 0v39-29c0-8 10-8 10 0v40l8-12c5-7 14-1 10 6L80 90c-7 14-27 17-39 5L23 76c-7-8 1-16 8-10l6 6Z"/></svg>';

const FINGERS = [
  [0, 1, 2, 3, 4],
  [0, 5, 6, 7, 8],
  [0, 9, 10, 11, 12],
  [0, 13, 14, 15, 16],
  [0, 17, 18, 19, 20],
];

/** @typedef {{ shortcuts?: boolean, onClose?: () => void }} HandControlPanelOptions
 *   shortcuts binds Space (follow) and R (recentre) page-wide -- only for a host
 *   whose keyboard is otherwise free; the teleop page already gives Space to the
 *   skill launcher. Escape always stops. */

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {HandControlPanelOptions} [opts]
 * @returns {{ el: HTMLElement, destroy: () => void }}
 */
export function createHandControlPanel(parent, ros, opts = {}) {
  if (!document.getElementById(STYLE_ID)) {
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  const root = document.createElement("div");
  root.className = "handctl";
  root.innerHTML = `
    <div class="handctl-head">
      <h2>Camera control</h2>
      <span class="spacer"></span>
      <span class="handctl-pill" data-el="status">camera off</span>
      ${opts.onClose ? '<button class="handctl-close" data-el="close" title="Close camera control" aria-label="Close camera control">✕</button>' : ""}
    </div>
    <div class="handctl-frame" data-el="frame">
      <video data-el="video" autoplay playsinline muted></video>
      <canvas data-el="overlay"></canvas>
      <div class="handctl-placeholder">
        ${HAND_ART}
        <span>A webcam and one hand.<br />Move, turn, tilt and pinch.</span>
      </div>
      <div class="handctl-hold" data-el="hold">
        <svg viewBox="0 0 44 44"><circle class="track" cx="22" cy="22" r="20"/><circle class="bar" data-el="ring" cx="22" cy="22" r="20" stroke-dashoffset="126"/></svg>
        <span data-el="holdLabel">Hand tracking on hold</span>
      </div>
      <div class="handctl-meters">
        <span title="hand results per second in this browser"><b>track</b><span data-el="rate">—</span>/s</span>
        <span class="handctl-roomy" title="mean hand-tracking inference time in this browser"><b>infer</b><span data-el="infer">—</span>ms</span>
        <span class="handctl-jaw" title="measured jaw opening" role="meter" aria-label="Gripper opening" data-el="gripBar"><b>jaws</b><span class="handctl-grip"><i data-el="grip"></i></span></span>
      </div>
    </div>
    <p class="handctl-feedback" data-el="feedback"></p>
    <p class="handctl-error" data-el="error" hidden></p>
    <div class="handctl-actions">
      <button data-el="camera" class="primary">${ICON_CAMERA}<span>Enable camera</span></button>
      <button data-el="follow" class="primary" disabled>${ICON_PLAY}<span>Start following</span></button>
      <button data-el="recentre" disabled title="Anchor your hand again, wherever it is now">Recentre</button>
    </div>
    <label class="handctl-tune" title="How far the claw travels for a given hand movement">
      feel<input type="range" data-el="sens" min="0.4" max="2" step="0.1" value="1"><span data-el="sensValue">1.0×</span>
    </label>
    <p class="handctl-hint">
      <span>Video is processed in this browser and never sent to the robot — only claw targets are.
      Your pinch takes over the gripper the moment you start.</span>
      ${opts.shortcuts ? "<span><kbd>Space</kbd> follow · <kbd>R</kbd> recentre · <kbd>Esc</kbd> stop.</span>" : ""}
    </p>`;
  parent.appendChild(root);

  /** @param {string} name */
  const el = (name) => /** @type {HTMLElement} */ (root.querySelector(`[data-el="${name}"]`));
  const video = /** @type {HTMLVideoElement} */ (el("video"));
  const overlay = /** @type {HTMLCanvasElement} */ (el("overlay"));
  const context = overlay.getContext("2d");

  /** @type {import("./session.js").HandControlState | null} */
  let last = null;

  const control = createHandControl({
    ros,
    video,
    onSample: draw,
    onState: render,
    claim: () => claimArmControl(ARM_CONTROL_NAME),
    blockedBy: () => {
      const holder = armControlHolder();
      return holder === ARM_CONTROL_NAME ? null : holder;
    },
  });

  /** @param {import("./handSample.js").HandSample | null} sample */
  function draw(sample) {
    if (!context) return;
    // Assigning a canvas dimension reallocates its bitmap even when unchanged.
    const width = video.videoWidth || 640;
    const height = video.videoHeight || 480;
    if (overlay.width !== width) overlay.width = width;
    if (overlay.height !== height) overlay.height = height;
    context.clearRect(0, 0, overlay.width, overlay.height);
    if (!sample?.raw) return;
    const points = sample.raw.map((p) => [p.x * overlay.width, p.y * overlay.height]);
    context.strokeStyle = sample.valid ? "#7fe0b0" : "#e8a33d";
    context.lineWidth = 2;
    context.lineCap = "round";
    for (const finger of FINGERS) {
      context.beginPath();
      finger.forEach((i, n) => (n ? context.lineTo(points[i][0], points[i][1]) : context.moveTo(points[i][0], points[i][1])));
      context.stroke();
    }
    context.fillStyle = "#fff";
    for (const [x, y] of points) {
      context.beginPath();
      context.arc(x, y, 2.6, 0, Math.PI * 2);
      context.fill();
    }
    // The jaw line: the only gesture that opens and closes the gripper.
    if (sample.gripValid) {
      context.save();
      context.strokeStyle = "#d4ff72";
      context.lineWidth = 3;
      context.setLineDash([5, 5]);
      context.beginPath();
      context.moveTo(points[4][0], points[4][1]);
      context.lineTo(points[8][0], points[8][1]);
      context.stroke();
      context.restore();
    }
  }

  /** render runs on every tracker result; rewriting a button's markup that often
   * is wasted layout. @param {HTMLElement} node @param {string} html */
  function setContent(node, html) {
    if (node.innerHTML !== html) node.innerHTML = html;
  }
  /** @param {HTMLElement} node @param {string} text */
  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  /** @param {import("./session.js").HandControlState} state */
  function render(state) {
    last = state;
    const live = state.phase === "following";
    el("frame").classList.toggle("on", state.cameraOn);

    const status = el("status");
    const [text, cls] = !state.connected
      ? ["robot offline", "warn"]
      : state.torque === false
        ? ["arm limp", "warn"]
        : !state.cameraOn
          ? ["camera off", ""]
          : live
            ? [state.limited ? "at a limit" : "following", "live"]
            : state.phase === "holding"
              ? ["holding", ""]
              : state.handTracked
                ? ["hand tracked", "on"]
                : ["no hand", ""];
    setText(status, text);
    const pill = `handctl-pill${cls ? ` ${cls}` : ""}`;
    if (status.className !== pill) status.className = pill;

    setText(el("feedback"), state.feedback);
    el("error").hidden = !state.error;
    setText(el("error"), state.error);
    setText(el("rate"), state.rate ? String(state.rate) : "—");
    setText(el("infer"), state.inferenceMs ? String(Math.round(state.inferenceMs)) : "—");
    const grip = Math.round(state.grip * 100);
    const gripWidth = `${grip}%`;
    if (el("grip").style.width !== gripWidth) el("grip").style.width = gripWidth;
    el("gripBar").setAttribute("aria-valuenow", String(grip));

    const holding = state.phase === "holding";
    el("hold").classList.toggle("show", holding);
    setText(el("holdLabel"), state.progress > 0 ? "Hold still to resume" : "Tracking on hold · arm holds");
    const ring = el("ring");
    const dashOffset = String(126 * (1 - state.progress));
    if (ring.getAttribute("stroke-dashoffset") !== dashOffset) ring.setAttribute("stroke-dashoffset", dashOffset);

    const camera = /** @type {HTMLButtonElement} */ (el("camera"));
    setContent(camera, `${ICON_CAMERA}<span>${state.cameraOn ? "Camera off" : "Enable camera"}</span>`);
    camera.disabled = state.phase === "loading";
    camera.classList.toggle("primary", !state.cameraOn);

    const follow = /** @type {HTMLButtonElement} */ (el("follow"));
    const engaged = live || holding;
    setContent(follow, `${engaged ? ICON_STOP : ICON_PLAY}<span>${engaged ? "Stop" : "Start following"}</span>`);
    follow.disabled = !state.cameraOn || state.phase === "loading" || (!engaged && (!state.connected || !!state.blockedBy));
    follow.classList.toggle("live", engaged);

    // Recentring only means something while the claw is following a hand.
    /** @type {HTMLButtonElement} */ (el("recentre")).disabled = !engaged;
  }

  // ---- wiring -------------------------------------------------------------

  el("camera").addEventListener("click", () => (last?.cameraOn ? control.disableCamera() : void control.enableCamera()));
  el("follow").addEventListener("click", () => {
    if (last && (last.phase === "following" || last.phase === "holding")) control.stop();
    else control.start();
  });
  el("recentre").addEventListener("click", () => control.recenter());
  el("close")?.addEventListener("click", () => opts.onClose?.());
  const sens = /** @type {HTMLInputElement} */ (el("sens"));
  sens.addEventListener("input", () => {
    control.setSensitivity(+sens.value);
    el("sensValue").textContent = `${(+sens.value).toFixed(1)}×`;
  });

  /** @param {KeyboardEvent} event */
  const onKey = (event) => {
    const engaged = last?.phase === "following" || last?.phase === "holding";
    if (event.key === "Escape" && engaged) {
      control.stop();
      return;
    }
    const target = /** @type {HTMLElement} */ (event.target);
    if (/INPUT|TEXTAREA|SELECT/.test(target.tagName) || target.isContentEditable) return;
    if (!opts.shortcuts || event.repeat || !last?.cameraOn) return;
    if (event.code === "Space" && target.tagName !== "BUTTON") {
      event.preventDefault();
      if (engaged) control.stop();
      else control.start();
    }
    // R is the Arm SDK page's own +z jog; only claim it while we hold the arm.
    if (event.key.toLowerCase() === "r" && engaged) control.recenter();
  };
  document.addEventListener("keydown", onKey);

  // Another surface taking the arm (leader-arm follow) must show here at once.
  const unsubLock = onArmControlChange((holder) => {
    if (last) render({ ...last, blockedBy: holder === ARM_CONTROL_NAME ? null : holder });
  });

  return {
    el: root,
    destroy() {
      document.removeEventListener("keydown", onKey);
      unsubLock();
      control.destroy();
      root.remove();
    },
  };
}
