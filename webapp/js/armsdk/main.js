// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Arm SDK page — a jog console for the Manipulation API, with a live 3D view
// of the robot (see viz.js). Commands ride rosbridge like every other page:
// an ExecuteArmCommand action into brain_client's arm_sdk_server (which runs
// the actual Python Manipulation class against the live arm), plus a slider
// stream topic — so what you exercise here is the real SDK code path
// (IK topics + goto_js services), not a JS reimplementation of it. Pose,
// joints and torque come straight off the driver topics, so a page that is
// just watching the arm costs the robot nothing.

import { copyToButton, ICON_COPY } from "../clipboard.js";
import { ARM_STATUS_TOPIC } from "../constants.js";
import { ros } from "../rosClient.js";

const ARM_ACTION = "/armsdk/command";
const ARM_ACTION_TYPE = "brain_messages/action/ExecuteArmCommand";
const STREAM_TOPIC = "/armsdk/stream_joints";
const ARM_STATE_TOPIC = "/mars/arm/state";
const FK_POSE_TOPIC = "/fk_pose";
const STATE_THROTTLE_MS = 100;

/** createArmViz with its module fetched on demand — viz.js pulls in the vendored three.js, by far the
 * heaviest module in the app, and only this page ever renders it.
 * @type {typeof import("./viz.js").createArmViz} */
const buildViz = (container, callbacks) => import("./viz.js").then((m) => m.createArmViz(container, callbacks));

const deg = (/** @type {number} */ r) => (r * 180) / Math.PI;
const rad = (/** @type {number} */ d) => (d * Math.PI) / 180;
const fmt = (/** @type {number | null | undefined} */ v, n = 3) => (v == null ? "—" : v.toFixed(n));

/** (roll, pitch, yaw) from a quaternion — mirrors common/geometry.quat_to_rpy (ZYX).
 * @param {{ x: number, y: number, z: number, w: number }} q */
function quatToRpy(q) {
  const roll = Math.atan2(2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y));
  const sinp = 2 * (q.w * q.y - q.z * q.x);
  const pitch = Math.abs(sinp) >= 1 ? Math.sign(sinp) * Math.PI / 2 : Math.asin(sinp);
  const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
  return [roll, pitch, yaw];
}

// Matches Manipulation.JOINT_NAMES order. Ranges are the URDF joint limits
// (mars.urdf, the same file the IK solves against), so the sliders span what
// the arm can actually reach; j6 additionally extends below closed to
// -GRIPPER_MAX_STRENGTH for grip preload.
const JOINTS = [
  { name: "j1 · base yaw", lo: -1.57, hi: 1.57 },
  { name: "j2 · shoulder", lo: -1.57, hi: 1.22 },
  { name: "j3 · elbow", lo: -1.57, hi: 1.75 },
  { name: "j4 · wrist pitch", lo: -1.92, hi: 1.75 },
  { name: "j5 · wrist roll", lo: -1.57, hi: 1.57 },
  { name: "j6 · gripper", lo: -0.6, hi: 0.87 },
];

const STYLE_ID = "armsdk-style";
const CSS = `
.armsdk { padding: 18px 22px 26px; overflow-y: auto; height: 100%; font-size: 14px; }
.armsdk-head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 4px; }
.armsdk-sub { color: var(--muted); font-size: 12px; letter-spacing: .04em; }
.armsdk-banner { display: none; margin: 10px 0; padding: 10px 14px; border: 1px solid var(--danger);
  border-radius: 9px; color: var(--danger); background: var(--panel); font-size: 13px; }
.armsdk-banner.show { display: block; }
.armsdk-banner code { color: var(--text); }
.armsdk-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 16px; }
.armsdk-toolbar .spacer { flex: 1; }
.armsdk-grid { display: grid; grid-template-columns: minmax(380px, 1.25fr) minmax(320px, .85fr); gap: 14px; }
.armsdk-bottom { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr); gap: 14px; margin-top: 14px; }
@media (max-width: 980px) { .armsdk-grid, .armsdk-bottom { grid-template-columns: 1fr; } }
.armsdk-card { background: linear-gradient(180deg, rgb(255 255 255 / 2.5%), transparent 40%), var(--panel);
  border: 1px solid var(--hairline); border-radius: 12px; padding: 14px 16px; }
.armsdk-card h2 { font-size: 11px; margin: 0 0 10px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .09em; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.armsdk-copy { pointer-events: auto; background: var(--glass); color: var(--muted);
  border: 1px solid var(--hairline-strong); border-radius: 6px; padding: 3px 7px; line-height: 0;
  cursor: pointer; transition: color 120ms ease, border-color 120ms ease; }
.armsdk-copy:hover { color: var(--accent); border-color: var(--accent-dim); }
.armsdk-copy.copied { color: var(--ok); border-color: var(--ok); }
.armsdk-dirty { color: var(--accent); font-size: 10px; letter-spacing: .05em; text-transform: none;
  border: 1px solid var(--accent-dim); border-radius: 999px; padding: 2px 9px; font-weight: 500; }
.armsdk button { background: var(--panel); color: var(--text); border: 1px solid var(--hairline-strong);
  border-radius: 8px; padding: 7px 12px; font: inherit; font-size: 13px; cursor: pointer;
  transition: border-color 120ms ease, color 120ms ease, background 120ms ease; }
.armsdk button:hover:not(:disabled) { border-color: var(--accent-dim); color: var(--accent); }
.armsdk button:disabled { opacity: .38; cursor: wait; }
.armsdk button.armsdk-go { border-color: rgb(86 194 140 / 45%); color: var(--ok); }
.armsdk button.armsdk-go:hover:not(:disabled) { border-color: var(--ok); background: rgb(86 194 140 / 8%); }
.armsdk button.armsdk-warn { border-color: rgb(217 106 90 / 45%); color: var(--danger); }
.armsdk button.armsdk-warn:hover:not(:disabled) { border-color: var(--danger); background: rgb(217 106 90 / 8%); }
.armsdk-pill { padding: 3px 12px; border-radius: 999px; font-size: 12px; border: 1px solid var(--hairline-strong);
  color: var(--muted); font-family: var(--mono, ui-monospace, monospace); }
.armsdk-pill.on { color: var(--ok); border-color: rgb(86 194 140 / 55%); }
.armsdk-pill.off { color: var(--danger); border-color: rgb(217 106 90 / 55%); }
.armsdk-pill.busy { color: var(--accent); border-color: var(--accent-dim); }

/* --- viewport ----------------------------------------------------------- */
.armsdk-viz { position: relative; padding: 0; overflow: hidden; min-height: 480px; }
.armsdk-viz-canvas { position: absolute; inset: 0; }
.armsdk-viz-canvas canvas { width: 100%; height: 100%; }
.armsdk-viz-hint { position: absolute; top: 12px; right: 14px; font-size: 11px; color: var(--muted);
  background: var(--glass); border: 1px solid var(--hairline); border-radius: 999px; padding: 3px 12px;
  pointer-events: none; backdrop-filter: blur(6px); }
.armsdk-viz-title { position: absolute; top: 12px; left: 14px; font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .09em; pointer-events: none; }
.armsdk-viz-legend { position: absolute; top: 32px; left: 14px; font-size: 11px; color: var(--muted);
  pointer-events: none; }
.armsdk-viz-loading { position: absolute; inset: 0; display: grid; place-items: center;
  color: var(--muted); font-size: 12px; letter-spacing: .05em; }
.armsdk-pose { position: absolute; left: 12px; right: 12px; bottom: 12px; display: flex; flex-wrap: wrap;
  gap: 6px; pointer-events: none; }
.armsdk-pose span { background: var(--glass); border: 1px solid var(--hairline); border-radius: 8px;
  padding: 4px 10px; font-family: var(--mono, ui-monospace, monospace); font-size: 12px; color: var(--text);
  backdrop-filter: blur(6px); }
.armsdk-pose b { color: var(--muted); font-weight: 500; margin-right: 6px; }
.armsdk-pose .num { color: var(--accent); }

/* --- controls ----------------------------------------------------------- */
.armsdk-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 7px 0; }
.armsdk-row label { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.armsdk input[type="number"] { width: 74px; background: var(--bg); color: var(--text);
  border: 1px solid var(--hairline-strong); border-radius: 7px; padding: 5px 8px; font: inherit; font-size: 13px; }
.armsdk input[type="number"]:focus, .armsdk select:focus { outline: none; border-color: var(--accent-dim); }
.armsdk input[type="range"] { flex: 1; accent-color: var(--accent); }
.armsdk select { background: var(--bg); color: var(--text); border: 1px solid var(--hairline-strong);
  border-radius: 7px; padding: 5px 7px; font: inherit; font-size: 13px; }
.armsdk-jog { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 8px 0; }
.armsdk-jog button { padding: 9px 6px; font-family: var(--mono, ui-monospace, monospace); font-size: 12.5px; }
.armsdk-jog kbd { font-size: 9.5px; color: var(--muted); border: 1px solid var(--hairline-strong);
  border-radius: 4px; padding: 0 4px; margin-left: 5px; background: var(--bg); font-family: inherit; }
.armsdk-jog button:hover:not(:disabled) kbd { color: var(--accent); border-color: var(--accent-dim); }
.armsdk-jog button.key-flash { border-color: var(--accent); color: var(--accent); background: var(--accent-faint); }
.armsdk-jog button.key-flash kbd { color: var(--accent); border-color: var(--accent); }
.armsdk-hint { color: var(--muted); font-size: 11.5px; margin-top: 6px; }
.armsdk-jname { width: 108px; display: inline-block; color: var(--muted); font-size: 12px; }
.armsdk-jval { width: 56px; display: inline-block; text-align: right; color: var(--accent); font-size: 12px;
  font-family: var(--mono, ui-monospace, monospace); }
.armsdk-log { height: 200px; overflow-y: auto; background: var(--bg); border: 1px solid var(--hairline);
  border-radius: 9px; padding: 9px 11px; font-size: 12px; white-space: pre-wrap; line-height: 1.55;
  font-family: var(--mono, ui-monospace, monospace); color: var(--muted); }
.armsdk-log .err { color: var(--danger); }
.armsdk-log .ok { color: var(--ok); }
`;

const PAGE_HTML = `
  <div class="armsdk-head page-head">
    <h1 class="page-title">Arm SDK</h1>
    <span class="armsdk-sub">live Manipulation API · URDF mirror</span>
  </div>
  <p class="armsdk-banner" data-el="banner">
    robot connection lost — commands and the live view pause until it returns
  </p>
  <div class="armsdk-toolbar">
    <span class="armsdk-pill" data-el="torque" title="servo torque state — /mars/arm/status">torque ?</span>
    <span class="armsdk-pill" data-el="moving" title="whether an arm command is in flight">idle</span>
    <button class="armsdk-go" data-cmd="torque_on" title="Stiffen the servos so the arm holds and accepts moves">Torque on</button>
    <button class="armsdk-warn" data-cmd="torque_off" title="Go limp — also the abort path; fires mid-motion">Torque off</button>
    <button data-cmd="rest" title="Move to the SDK rest pose">Rest</button>
    <button class="armsdk-warn" data-el="rebootBtn" title="recover() — reboot the servos, settle, torque back on">⟳ Reboot arm</button>
    <span class="spacer"></span>
    <label title="max_ee_speed — end-effector speed cap applied to every move">speed cap <input type="number" data-el="speedcap" value="0.20" step="0.05" min="0.05"> m/s</label>
    <label title="After each cartesian move the SDK FK-checks the settled pose against the target (5 cm xy / 10 cm z). A miss triggers recover() — servo reboot + torque on — then one retry before raising ArmUnhealthy. Off = moves are unverified; you just see the error here.">
      <input type="checkbox" data-el="verify"> verified moves</label>
  </div>

  <div class="armsdk-grid">
    <div class="armsdk-card armsdk-viz">
      <div class="armsdk-viz-canvas" data-el="viz"></div>
      <span class="armsdk-viz-title">mars.urdf · live joint state</span>
      <span class="armsdk-viz-legend">⊕ ring = base_link origin (0,0,0) · axes <b style="color:#e06c60">x</b> <b style="color:#56c28c">y</b> <b style="color:#6b93d6">z</b> · <b style="color:#e8a33d">⬥ target</b> <b style="color:#56c28c">● settled</b></span>
      <span class="armsdk-viz-hint">grab the amber handle to move the arm · drag to orbit · scroll to zoom</span>
      <div class="armsdk-viz-loading" data-el="vizLoading">loading model…</div>
      <div class="armsdk-pose">
        <span title="end-effector position in base_link (m) — /fk_pose"><b>xyz</b><span class="num" data-el="pxyz">—</span></span>
        <span title="end-effector roll/pitch/yaw (degrees)"><b>rpy</b><span class="num" data-el="prpy">—</span></span>
        <span title="measured gripper joint / commanded grip target"><b>grip</b><span class="num" data-el="pg">—</span> / <span class="num" data-el="gt">—</span> tgt</span>
        <span hidden data-el="dragChip" title="where the 3D drag handle will send the arm on release"><b>target</b><span class="num" data-el="dragXyz">—</span></span>
        <button class="armsdk-copy" data-el="copyPose" title="Copy pose (x y z rpy)">${ICON_COPY}</button>
      </div>
    </div>

    <div>
      <div class="armsdk-card" style="margin-bottom: 14px">
        <h2>End-effector jog · move_by</h2>
        <div class="armsdk-row">
          <label>step
            <select data-el="step">
              <option value="0.01">1 cm</option><option value="0.02" selected>2 cm</option><option value="0.05">5 cm</option>
            </select></label>
          <label>rot <select data-el="rstep"><option value="5">5°</option><option value="15" selected>15°</option></select></label>
          <label title="duration passed to move_by">dur <input type="number" data-el="jogdur" value="0.8" step="0.2" min="0.2"> s</label>
        </div>
        <div class="armsdk-jog">
          <button data-jog="dx,1" data-key="w" title="Keyboard: W — nudge +x (forward)">+x fwd <kbd>W</kbd></button>
          <button data-jog="dy,1" data-key="a" title="Keyboard: A — nudge +y (left)">+y left <kbd>A</kbd></button>
          <button data-jog="dz,1" data-key="r" title="Keyboard: R — nudge +z (up)">+z up <kbd>R</kbd></button>
          <button data-jog="dx,-1" data-key="s" title="Keyboard: S — nudge −x (back)">−x back <kbd>S</kbd></button>
          <button data-jog="dy,-1" data-key="d" title="Keyboard: D — nudge −y (right)">−y right <kbd>D</kbd></button>
          <button data-jog="dz,-1" data-key="f" title="Keyboard: F — nudge −z (down)">−z down <kbd>F</kbd></button>
          <button data-rjog="droll,1" title="Rotate +roll about x">+roll</button>
          <button data-rjog="dpitch,1" title="Rotate +pitch about y">+pitch</button>
          <button data-rjog="dyaw,1" title="Rotate +yaw about z">+yaw</button>
          <button data-rjog="droll,-1" title="Rotate −roll about x">−roll</button>
          <button data-rjog="dpitch,-1" title="Rotate −pitch about y">−pitch</button>
          <button data-rjog="dyaw,-1" title="Rotate −yaw about z">−yaw</button>
        </div>
        <div class="armsdk-hint">keyboard works anywhere on the page (not while typing in a field) — settled pose &amp; error land in the log</div>
      </div>

      <div class="armsdk-card" style="margin-bottom: 14px">
        <h2>Absolute · move_to</h2>
        <div class="armsdk-row">
          <label>x <input type="number" data-el="ax" value="0.28" step="0.01"></label>
          <label>y <input type="number" data-el="ay" value="0.00" step="0.01"></label>
          <label>z <input type="number" data-el="az" value="0.20" step="0.01"></label>
        </div>
        <div class="armsdk-row">
          <label>r° <input type="number" data-el="ar" value="0" step="5"></label>
          <label>p° <input type="number" data-el="ap" value="0" step="5"></label>
          <label>y° <input type="number" data-el="aw" value="0" step="5"></label>
          <label title="duration passed to move_to">dur <input type="number" data-el="adur" value="1.5" step="0.5" min="0.5"> s</label>
        </div>
        <div class="armsdk-row">
          <button class="armsdk-go" data-el="goBtn" title="move_to — absolute cartesian move to these fields">Go</button>
          <button data-el="fillBtn" title="Fill the fields from the live pose">← from current</button>
        </div>
      </div>

      <div class="armsdk-card">
        <h2>Gripper</h2>
        <div class="armsdk-row">
          <button class="armsdk-go" data-el="open100" title="gripper_open percent=100">Open</button>
          <button data-el="open50" title="gripper_open percent=50">Open 50%</button>
          <button class="armsdk-warn" data-el="closeBtn" title="gripper_close at the strength beside">Close</button>
          <label title="grip preload in rad — higher grips harder">strength <input type="number" data-el="grip" value="0.2" step="0.1" min="0" max="0.6"> rad</label>
        </div>
      </div>
    </div>
  </div>

  <div class="armsdk-bottom">
    <div class="armsdk-card">
      <h2>Joints · stream_joints (rad)
        <button class="armsdk-copy" data-el="copyJoints" title="Copy measured joint positions">${ICON_COPY}</button>
        <span class="armsdk-dirty" data-el="jdirty" hidden title="sliders are streaming joints; live sync resumes once the arm settles">driving live — sync paused</span></h2>
      <div data-el="sliders"></div>
      <div class="armsdk-row">
        <button data-el="syncBtn" title="Snap the sliders back to the measured arm state">Sync from measured</button>
        <button data-cmd="zero" title="Drive every joint to 0 rad">Zero pose</button>
      </div>
      <div class="armsdk-hint">sliders drive the arm live as you drag; they resume tracking the arm once it settles</div>
    </div>
    <div class="armsdk-card">
      <h2>Log</h2>
      <div class="armsdk-log" data-el="log"></div>
    </div>
  </div>`;

/** @param {HTMLElement} stage */
export function mount(stage) {
  if (!document.getElementById(STYLE_ID)) {
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  const root = document.createElement("div");
  root.className = "armsdk";
  root.innerHTML = PAGE_HTML;
  stage.appendChild(root);

  /** @param {string} name */
  const el = (name) => /** @type {HTMLElement} */ (root.querySelector(`[data-el="${name}"]`));
  /** @param {string} name */
  const input = (name) => /** @type {HTMLInputElement} */ (el(name));

  let busy = false;
  /** @type {number | null} which slider is mid-drag, so live sync doesn't fight the thumb */
  let dragging = null;
  /** @type {Awaited<ReturnType<typeof import("./viz.js").createArmViz>> | null} */
  let viz = null;
  let destroyed = false;
  /** Live arm state, mutated by the topic subscriptions and command results —
   * feeds the pose chips, drag-release orientation and the copy buttons.
   * @type {{ pose: any, joints: number[] | null, torque: boolean | null, grip_target: number | null }} */
  const lastState = { pose: null, joints: null, torque: null, grip_target: null };

  buildViz(el("viz"), {
    onDragMove(p) {
      el("dragChip").hidden = false;
      el("dragXyz").textContent = `${fmt(p.x)} ${fmt(p.y)} ${fmt(p.z)}`;
    },
    onDragEnd(p) {
      el("dragChip").hidden = true;
      const pose = lastState.pose;
      if (!pose) return;
      // Keep the current orientation — the drag only repositions the EE.
      cmd("move_to", {
        x: p.x,
        y: p.y,
        z: p.z,
        roll: pose.roll,
        pitch: pose.pitch,
        yaw: pose.yaw,
        duration: 1.2,
      });
    },
  }).then(
    (v) => {
      if (destroyed) {
        v.destroy();
        return;
      }
      viz = v;
      el("vizLoading").remove();
    },
    (err) => {
      el("vizLoading").textContent = `3D model unavailable (${err?.message || err})`;
    },
  );

  // ---- joint sliders ------------------------------------------------------
  /** @type {HTMLInputElement[]} */
  const sliders = [];
  /** @type {HTMLElement[]} */
  const sliderVals = [];
  JOINTS.forEach((j, i) => {
    const row = document.createElement("div");
    row.className = "armsdk-row";
    row.innerHTML = `<span class="armsdk-jname">${j.name}</span>
      <input type="range" min="${j.lo}" max="${j.hi}" step="0.01" value="0">
      <span class="armsdk-jval">0.00</span>`;
    const sl = /** @type {HTMLInputElement} */ (row.querySelector("input"));
    const val = /** @type {HTMLElement} */ (row.querySelector(".armsdk-jval"));
    sl.addEventListener("input", () => {
      dragging = i;
      if (i === 5) j6Touched = true;
      // Latch the card out of live sync while driving — otherwise the state
      // subscription would snap the thumb back under the pointer.
      setDirty(true);
      val.textContent = (+sl.value).toFixed(2);
      queueLive();
    });
    sl.addEventListener("change", () => {
      dragging = null;
    });
    sliders.push(sl);
    sliderVals.push(val);
    el("sliders").appendChild(row);
  });

  /** True while the sliders are driving the arm; pauses live sync so the
   * state subscription can't overwrite them mid-interaction. Cleared once the
   * arm settles. */
  let slidersDirty = false;

  /** True once the j6 slider itself was touched this interaction. Only then
   * is j6 streamed: streaming the measured j6 (which live sync wrote into the
   * slider) would re-command a gripped claw's stalled position and drop the
   * object — the SDK's 5-joint form keeps the standing grip instead. */
  let j6Touched = false;

  /** @param {boolean} dirty */
  function setDirty(dirty) {
    slidersDirty = dirty;
    el("jdirty").hidden = !dirty;
    if (!dirty) j6Touched = false;
  }

  // --- live joint streaming -------------------------------------------------
  // Sliding streams the arm after the thumb via the server's stream topic —
  // the driver's teleop pass-through with a velocity-clamped slew loop, the
  // same path leader-arm teleop uses. Smooth, unlike chaining the SDK's
  // rest-to-rest goto calls. Not via cmd(): no busy gate, no per-step log
  // spam; a step landing during a discrete motion is dropped server-side.
  /** @type {number[] | null} */
  let livePending = null;
  let liveInFlight = false;
  /** @type {number | undefined} */
  let liveResyncTimer;

  function queueLive() {
    clearTimeout(liveResyncTimer);
    const vals = sliders.map((sl) => +sl.value);
    livePending = j6Touched ? vals : vals.slice(0, 5);
    if (!liveInFlight) void pumpLive();
  }

  async function pumpLive() {
    liveInFlight = true;
    /** @type {number[] | null} */
    let lastSent = null;
    let deadline = performance.now() + 5000;
    while (!destroyed) {
      let joints = livePending;
      livePending = null;
      if (joints) {
        deadline = performance.now() + 5000;
      } else {
        // The SDK's stream idles out 0.4 s after the last message, which
        // would strand a big slider jump ~0.7 rad short of its target — keep
        // feeding the deadman until the arm converges (arm joints only; a
        // gripped j6 never reaches its goal by design). Capped so an
        // obstructed arm isn't pushed forever.
        const measured = lastState.joints;
        if (!lastSent || !measured || performance.now() > deadline) break;
        const err = Math.max(...lastSent.slice(0, 5).map((v, i) => Math.abs(v - (measured[i] ?? v))));
        if (err < 0.03) break;
        joints = lastSent;
      }
      lastSent = joints;
      ros.publish(STREAM_TOPIC, { data: joints });
      await new Promise((r) => setTimeout(r, 100));
    }
    liveInFlight = false;
    // Resume slider live-sync once the stream idles out and the arm settles.
    liveResyncTimer = setTimeout(() => setDirty(false), 1200);
  }

  /** @param {number[]} joints */
  function setSliders(joints) {
    joints.forEach((v, i) => {
      if (sliders[i] && dragging !== i) {
        sliders[i].value = String(v);
        sliderVals[i].textContent = v.toFixed(2);
      }
    });
  }

  /** @param {string} msg @param {string} [cls] */
  function log(msg, cls) {
    const line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = `${new Date().toLocaleTimeString()}  ${msg}`;
    el("log").appendChild(line);
    el("log").scrollTop = el("log").scrollHeight;
  }

  function render() {
    const s = lastState;
    if (s.pose) {
      el("pxyz").textContent = `${fmt(s.pose.x)} ${fmt(s.pose.y)} ${fmt(s.pose.z)}`;
      el("prpy").textContent = `${fmt(deg(s.pose.roll), 1)}° ${fmt(deg(s.pose.pitch), 1)}° ${fmt(deg(s.pose.yaw), 1)}°`;
    }
    el("pg").textContent = fmt(s.joints?.[5], 2);
    el("gt").textContent = fmt(s.grip_target, 2);
    const t = el("torque");
    t.textContent = "torque " + (s.torque == null ? "?" : s.torque ? "on" : "off");
    t.className = "armsdk-pill " + (s.torque ? "on" : "off");
    el("moving").textContent = busy ? "moving" : "idle";
    el("moving").className = "armsdk-pill " + (busy ? "busy" : "on");
    if (Array.isArray(s.joints)) {
      viz?.setJoints(s.joints);
      if (!busy && !slidersDirty) setSliders(s.joints);
    }
  }

  /** @param {string} name @param {Record<string, any>} [body] */
  async function cmd(name, body = {}) {
    // Torque off is the abort path — it must fire even while a motion is in
    // flight (the server exempts it from the motion lock; the motion then
    // fails fast on the disabled arm). It leaves the busy latch alone.
    const bypass = busy && name === "torque_off";
    if (busy && !bypass) {
      log("busy — wait for the current motion (Torque off aborts)", "err");
      return;
    }
    if (!bypass) busy = true;
    body.verify = input("verify").checked;
    const t0 = performance.now();
    try {
      const { promise } = ros.sendActionGoal(ARM_ACTION, ARM_ACTION_TYPE, {
        command: name,
        params: JSON.stringify(body),
      });
      const res = await promise;
      const data = JSON.parse(res.state || "{}");
      const dt = ((performance.now() - t0) / 1000).toFixed(1);
      if (!res.success) {
        log(`${name}: ${res.message}`, "err");
      } else {
        let msg = `${name} ok (${dt}s)`;
        if (data.settled) msg += ` → (${fmt(data.settled.x)}, ${fmt(data.settled.y)}, ${fmt(data.settled.z)})`;
        if (data.err_xy != null) msg += `  err_xy=${(data.err_xy * 1000).toFixed(0)}mm err_z=${(data.err_z * 1000).toFixed(0)}mm`;
        log(msg, "ok");
        // Commanded target vs where the arm actually settled, in the 3D view.
        if (data.target && data.settled) viz?.showResult(data.target, data.settled);
      }
      if (!bypass) busy = false;
      if (data.state) {
        lastState.grip_target = data.state.grip_target ?? lastState.grip_target;
        render();
      }
    } catch (e) {
      if (!bypass) busy = false;
      log(`${name}: ${e instanceof Error ? e.message : e}`, "err");
    }
  }

  /** @param {string} axis @param {number} sign */
  const jog = (axis, sign) => cmd("move_by", { [axis]: sign * +input("step").value, duration: +input("jogdur").value });
  /** @param {string} axis @param {number} sign */
  const rjog = (axis, sign) => cmd("move_by", { [axis]: sign * rad(+input("rstep").value), duration: +input("jogdur").value });

  // ---- wiring -------------------------------------------------------------
  root.querySelectorAll("[data-cmd]").forEach((b) =>
    b.addEventListener("click", () => cmd(/** @type {HTMLElement} */ (b).dataset.cmd || "")),
  );
  root.querySelectorAll("[data-jog]").forEach((b) =>
    b.addEventListener("click", () => {
      const [axis, sign] = (/** @type {HTMLElement} */ (b).dataset.jog || "").split(",");
      jog(axis, +sign);
    }),
  );
  root.querySelectorAll("[data-rjog]").forEach((b) =>
    b.addEventListener("click", () => {
      const [axis, sign] = (/** @type {HTMLElement} */ (b).dataset.rjog || "").split(",");
      rjog(axis, +sign);
    }),
  );
  input("speedcap").addEventListener("change", () => cmd("speed", { max_ee_speed: +input("speedcap").value }));
  el("goBtn").addEventListener("click", () =>
    cmd("move_to", {
      x: +input("ax").value,
      y: +input("ay").value,
      z: +input("az").value,
      roll: rad(+input("ar").value),
      pitch: rad(+input("ap").value),
      yaw: rad(+input("aw").value),
      duration: +input("adur").value,
    }),
  );
  el("fillBtn").addEventListener("click", () => {
    const p = lastState.pose;
    if (!p) return;
    input("ax").value = p.x.toFixed(3);
    input("ay").value = p.y.toFixed(3);
    input("az").value = p.z.toFixed(3);
    input("ar").value = deg(p.roll).toFixed(0);
    input("ap").value = deg(p.pitch).toFixed(0);
    input("aw").value = deg(p.yaw).toFixed(0);
  });
  el("syncBtn").addEventListener("click", () => {
    setDirty(false);
    if (Array.isArray(lastState.joints)) setSliders(lastState.joints);
  });
  el("open100").addEventListener("click", () => cmd("gripper_open", { percent: 100 }));
  el("open50").addEventListener("click", () => cmd("gripper_open", { percent: 50 }));
  el("closeBtn").addEventListener("click", () => cmd("gripper_close", { strength: +input("grip").value }));
  el("rebootBtn").addEventListener("click", () => {
    if (!window.confirm("Reboot the arm servos? Any running motion stops; torque re-enables automatically.")) return;
    cmd("recover"); // SDK recover() = reboot + settle + torque back on
  });
  el("copyJoints").addEventListener("click", () => {
    const joints = lastState.joints;
    if (!Array.isArray(joints)) return;
    const text = `[${joints.map((/** @type {number} */ v) => v.toFixed(4)).join(", ")}]`;
    void copyToButton(text, el("copyJoints"), "copied");
    log(`copied joints ${text}`, "ok");
  });
  el("copyPose").addEventListener("click", () => {
    const p = lastState.pose;
    if (!p) return;
    const text =
      `x=${p.x.toFixed(3)}, y=${p.y.toFixed(3)}, z=${p.z.toFixed(3)}, ` +
      `roll=${p.roll.toFixed(3)}, pitch=${p.pitch.toFixed(3)}, yaw=${p.yaw.toFixed(3)}`;
    void copyToButton(text, el("copyPose"), "copied");
    log(`copied pose ${text}`, "ok");
  });

  /** @param {KeyboardEvent} e */
  const onKey = (e) => {
    const target = /** @type {HTMLElement} */ (e.target);
    if (target.tagName === "INPUT" || target.tagName === "SELECT") return;
    /** @type {Record<string, [string, number]>} */
    const map = { w: ["dx", 1], s: ["dx", -1], a: ["dy", 1], d: ["dy", -1], r: ["dz", 1], f: ["dz", -1] };
    const key = e.key.toLowerCase();
    const m = map[key];
    if (m) {
      e.preventDefault();
      // Light up the matching jog button so the key press is visible on screen.
      const btn = root.querySelector(`[data-key="${key}"]`);
      if (btn) {
        btn.classList.add("key-flash");
        setTimeout(() => btn.classList.remove("key-flash"), 250);
      }
      jog(m[0], m[1]);
    }
  };
  document.addEventListener("keydown", onKey);

  // Driver topics, not the SDK server: they publish regardless, so watching
  // the arm never wakes the Manipulation feeds — and they keep flowing while
  // a motion blocks, so the 3D view tracks the arm through the whole move.
  const unsubs = [
    ros.subscribe(
      ARM_STATE_TOPIC,
      (/** @type {any} */ m) => {
        if (!Array.isArray(m?.position)) return;
        lastState.joints = m.position.slice(0, 6);
        render();
      },
      STATE_THROTTLE_MS,
      "sensor_msgs/msg/JointState",
    ),
    ros.subscribe(
      FK_POSE_TOPIC,
      (/** @type {any} */ m) => {
        const p = m?.pose;
        if (!p?.position || !p?.orientation) return;
        const [roll, pitch, yaw] = quatToRpy(p.orientation);
        lastState.pose = { x: p.position.x, y: p.position.y, z: p.position.z, roll, pitch, yaw };
        render();
      },
      STATE_THROTTLE_MS,
      "geometry_msgs/msg/PoseStamped",
    ),
    ros.subscribe(
      ARM_STATUS_TOPIC,
      (/** @type {any} */ m) => {
        if (typeof m?.is_torque_enabled !== "boolean") return;
        lastState.torque = m.is_torque_enabled;
        render();
      },
      undefined,
      "mars_msgs/msg/ArmStatus",
    ),
  ];
  const showBanner = () => el("banner").classList.toggle("show", ros.state !== "connected");
  const unsubConn = ros.onStateChange(showBanner);
  showBanner();

  log("ready — check torque, then jog gently. Speed cap 0.20 m/s is active.");

  return {
    destroy() {
      destroyed = true;
      clearTimeout(liveResyncTimer);
      livePending = null; // stop the pump loop from sending more
      for (const unsub of unsubs) unsub();
      unsubConn();
      document.removeEventListener("keydown", onKey);
      viz?.destroy();
      root.remove();
    },
  };
}
