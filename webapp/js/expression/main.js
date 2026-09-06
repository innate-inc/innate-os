// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Expression Studio — design MARS's body language from first principles.
//
// A pose is authored on semantic axes (approach, turn, rise, expand, reach,
// attend — see model.js), previewed live on the full-robot 3D view, scored by
// blind VLM judges that are never told the target emotion (judge.js), and
// collected into an exportable library — the raw material for the next steps
// (PCA over collected poses, motion dynamics, the runtime controller).
//
// The robot side rides the Arm SDK server's existing paths: the joint stream
// topic for the arm, /mars/head/set_position for the head. Base offset is
// preview-only for now — driving belongs to the runtime controller.

import {
  ACTUATORS,
  AXES,
  NEUTRAL,
  PRESETS,
  actuatorNumbers,
  defaultBasis,
  exportPayload,
  parseImport,
  sanitizeBasis,
  sanitizeOffsets,
  sanitizeWeights,
  synthesize,
  zeroWeights,
} from "./model.js";
import { createExpressionViz } from "./viz.js";
import { JUDGE_LABELS, judgePair, judgePanel, loadJudgeConfig, saveJudgeConfig } from "./judge.js";
import { copyToButton, ICON_COPY } from "../clipboard.js";
import { ARM_STATUS_TOPIC, HEAD_MAX_DEG, HEAD_MIN_DEG, HEAD_SET_POSITION_TOPIC } from "../constants.js";
import { ros } from "../rosClient.js";

const ARM_ACTION = "/armsdk/command";
const ARM_ACTION_TYPE = "brain_messages/action/ExecuteArmCommand";
const STREAM_TOPIC = "/armsdk/stream_joints";
const ARM_STATE_TOPIC = "/mars/arm/state";
const LIBRARY_KEY = "innate.expression.library";
const BASIS_KEY = "innate.expression.basis";
const MAX_POSES = 60;

/** @typedef {import("./model.js").Pose} Pose */
/** @typedef {import("./model.js").SavedPose} SavedPose */

const deg = (/** @type {number} */ r) => (r * 180) / Math.PI;

const STYLE_ID = "expression-style";
const CSS = `
.exs { padding: 18px 22px 26px; overflow-y: auto; height: 100%; font-size: 14px; }
.exs-head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 4px; }
.exs-sub { color: var(--muted); font-size: 12px; letter-spacing: .04em; }
.exs-banner { display: none; margin: 10px 0; padding: 10px 14px; border: 1px solid var(--hairline-strong);
  border-radius: 9px; color: var(--muted); background: var(--panel); font-size: 13px; }
.exs-banner.show { display: block; }
.exs-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0 16px; }
.exs-toolbar .spacer { flex: 1; }
.exs-grid { display: grid; grid-template-columns: minmax(400px, 1.35fr) minmax(320px, .85fr); gap: 14px; }
.exs-bottom { display: grid; grid-template-columns: minmax(340px, 1.15fr) minmax(300px, .85fr); gap: 14px; margin-top: 14px; }
@media (max-width: 1020px) { .exs-grid, .exs-bottom { grid-template-columns: 1fr; } }
.exs-card { background: linear-gradient(180deg, rgb(255 255 255 / 2.5%), transparent 40%), var(--panel);
  border: 1px solid var(--hairline); border-radius: 12px; padding: 14px 16px; }
.exs-card h2 { font-size: 11px; margin: 0 0 10px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .09em; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.exs-card h2 .spacer { flex: 1; }
.exs button { background: var(--panel); color: var(--text); border: 1px solid var(--hairline-strong);
  border-radius: 8px; padding: 7px 12px; font: inherit; font-size: 13px; cursor: pointer;
  transition: border-color 120ms ease, color 120ms ease, background 120ms ease; }
.exs button:hover:not(:disabled) { border-color: var(--accent-dim); color: var(--accent); }
.exs button:disabled { opacity: .38; cursor: wait; }
.exs button.exs-go { border-color: rgb(86 194 140 / 45%); color: var(--ok); }
.exs button.exs-go:hover:not(:disabled) { border-color: var(--ok); background: rgb(86 194 140 / 8%); }
.exs button.exs-warn { border-color: rgb(217 106 90 / 45%); color: var(--danger); }
.exs button.exs-warn:hover:not(:disabled) { border-color: var(--danger); background: rgb(217 106 90 / 8%); }
.exs button.exs-mini { padding: 3px 9px; font-size: 12px; }
.exs-pill { padding: 3px 12px; border-radius: 999px; font-size: 12px; border: 1px solid var(--hairline-strong);
  color: var(--muted); font-family: var(--mono, ui-monospace, monospace); }
.exs-pill.on { color: var(--ok); border-color: rgb(86 194 140 / 55%); }
.exs-pill.off { color: var(--danger); border-color: rgb(217 106 90 / 55%); }
.exs input[type="text"], .exs input[type="password"], .exs input[type="number"], .exs select {
  background: var(--bg); color: var(--text); border: 1px solid var(--hairline-strong); border-radius: 7px;
  padding: 5px 8px; font: inherit; font-size: 13px; }
.exs input[type="number"] { width: 58px; }
.exs input:focus, .exs select:focus { outline: none; border-color: var(--accent-dim); }
.exs input[type="range"] { flex: 1; accent-color: var(--accent); min-width: 90px; }
.exs label { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.exs-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 7px 0; }
.exs-hint { color: var(--muted); font-size: 11.5px; margin-top: 6px; }

/* --- viewport ----------------------------------------------------------- */
.exs-viz { position: relative; padding: 0; overflow: hidden; min-height: 500px; }
.exs-viz-canvas { position: absolute; inset: 0; }
.exs-viz-canvas canvas { width: 100%; height: 100%; }
.exs-viz-title { position: absolute; top: 12px; left: 14px; font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .09em; pointer-events: none; }
.exs-viz-legend { position: absolute; top: 32px; left: 14px; font-size: 11px; color: var(--muted);
  pointer-events: none; }
.exs-viz-hint { position: absolute; top: 12px; right: 14px; font-size: 11px; color: var(--muted);
  background: var(--glass); border: 1px solid var(--hairline); border-radius: 999px; padding: 3px 12px;
  pointer-events: none; backdrop-filter: blur(6px); }
.exs-viz-loading { position: absolute; inset: 0; display: grid; place-items: center;
  color: var(--muted); font-size: 12px; letter-spacing: .05em; }
.exs-chips { position: absolute; left: 12px; right: 12px; bottom: 12px; display: flex; flex-wrap: wrap;
  gap: 6px; pointer-events: none; }
.exs-chips span { background: var(--glass); border: 1px solid var(--hairline); border-radius: 8px;
  padding: 4px 10px; font-family: var(--mono, ui-monospace, monospace); font-size: 12px; color: var(--text);
  backdrop-filter: blur(6px); }
.exs-chips b { color: var(--muted); font-weight: 500; margin-right: 6px; }
.exs-chips .num { color: var(--accent); }
.exs-copy { pointer-events: auto; background: var(--glass); color: var(--muted);
  border: 1px solid var(--hairline-strong); border-radius: 6px; padding: 3px 7px; line-height: 0;
  cursor: pointer; transition: color 120ms ease, border-color 120ms ease; }
.exs-copy:hover { color: var(--accent); border-color: var(--accent-dim); }
.exs-copy.copied { color: var(--ok); border-color: var(--ok); }

/* --- axes --------------------------------------------------------------- */
.exs-axgroup { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .09em;
  font-weight: 600; margin: 12px 0 2px; padding-top: 8px; border-top: 1px solid var(--hairline); }
.exs-axgroup:first-child { margin-top: 2px; padding-top: 0; border-top: none; }
.exs-axis { margin: 9px 0; }
.exs-axis-top { display: flex; align-items: baseline; gap: 8px; }
.exs-axis-name { font-size: 12.5px; color: var(--text); width: 74px; }
.exs-axis-val { margin-left: auto; font-family: var(--mono, ui-monospace, monospace); font-size: 12px;
  color: var(--accent); width: 46px; text-align: right; }
.exs-axis-track { display: flex; align-items: center; gap: 8px; }
.exs-axis-end { font-size: 10.5px; color: var(--muted); width: 74px; letter-spacing: .02em; }
.exs-axis-end.hi { text-align: right; }
.exs-presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
.exs-presets button { padding: 4px 11px; font-size: 12.5px; border-radius: 999px; }
.exs-presets button.active { border-color: var(--accent); color: var(--accent); background: var(--accent-faint); }
.exs details { margin-top: 12px; border-top: 1px solid var(--hairline); padding-top: 10px; }
.exs summary { cursor: pointer; font-size: 11px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .09em; font-weight: 600; }
.exs-off-row { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
.exs-off-name { width: 104px; font-size: 12px; color: var(--muted); }
.exs-off-val { width: 56px; text-align: right; font-family: var(--mono, ui-monospace, monospace);
  font-size: 12px; color: var(--accent); }

/* --- basis editor -------------------------------------------------------- */
.exs-bx { border-top: 1px solid var(--hairline); padding: 7px 0 3px; }
.exs-bx:first-child { border-top: none; }
.exs-bx-head { display: flex; gap: 6px; align-items: center; }
.exs-bx-head .spacer { flex: 1; }
.exs-bx-head input { font-size: 12px; padding: 3px 6px; }
.exs-bx-end { display: flex; gap: 6px; align-items: center; margin: 5px 0; }
.exs-bx-end > b { width: 18px; font-size: 11px; color: var(--muted); font-weight: 500;
  font-family: var(--mono, ui-monospace, monospace); }
.exs-bx-end input { flex: 1; min-width: 0; font-family: var(--mono, ui-monospace, monospace);
  font-size: 11.5px; padding: 3px 7px; }
.exs-bx-end input.bad { border-color: var(--danger); }
.exs-bx-end button { white-space: nowrap; }

/* --- judge -------------------------------------------------------------- */
.exs-views { display: flex; gap: 8px; margin: 8px 0; }
.exs-views figure { margin: 0; flex: 1; min-width: 0; }
.exs-views img { width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 8px;
  border: 1px solid var(--hairline); display: block; background: var(--bg); }
.exs-views figcaption { font-size: 10.5px; color: var(--muted); margin-top: 3px; text-align: center; }
.exs-score { font-size: 15px; margin: 8px 0 4px; }
.exs-score b { color: var(--accent); font-family: var(--mono, ui-monospace, monospace); }
.exs-bars { margin: 6px 0; }
.exs-bar { display: grid; grid-template-columns: 86px 1fr 44px; gap: 8px; align-items: center;
  margin: 3px 0; font-size: 12px; }
.exs-bar .lbl { color: var(--muted); text-align: right; }
.exs-bar .track { display: block; height: 9px; border-radius: 5px; background: var(--bg); overflow: hidden;
  border: 1px solid var(--hairline); }
.exs-bar .fill { display: block; height: 100%; background: var(--hairline-strong); border-radius: 5px 0 0 5px;
  transition: width 300ms ease; }
.exs-bar.target .lbl { color: var(--accent); }
.exs-bar.target .fill { background: var(--accent); }
.exs-bar .num { font-family: var(--mono, ui-monospace, monospace); color: var(--text); }
.exs-quotes { margin: 8px 0 0; font-size: 12px; color: var(--muted); line-height: 1.5; }
.exs-quotes li { margin: 2px 0 2px 16px; }
.exs-log { max-height: 150px; overflow-y: auto; background: var(--bg); border: 1px solid var(--hairline);
  border-radius: 9px; padding: 8px 11px; font-size: 12px; line-height: 1.55; margin-top: 10px;
  font-family: var(--mono, ui-monospace, monospace); color: var(--muted); white-space: pre-wrap; }
.exs-log .ok { color: var(--ok); }
.exs-log .err { color: var(--danger); }
.exs-pin { display: inline-flex; align-items: center; gap: 7px; }
.exs-pin img { width: 30px; height: 30px; border-radius: 6px; border: 1px solid var(--hairline); }

/* --- library ------------------------------------------------------------ */
.exs-lib { max-height: 420px; overflow-y: auto; margin-top: 8px; }
.exs-lib-item { display: flex; align-items: center; gap: 10px; padding: 6px 8px; border-radius: 9px;
  cursor: pointer; border: 1px solid transparent; }
.exs-lib-item:hover { background: var(--accent-faint); border-color: var(--accent-dim); }
.exs-lib-item img { width: 44px; height: 44px; border-radius: 8px; border: 1px solid var(--hairline);
  background: var(--bg); object-fit: cover; }
.exs-lib-item .noimg { width: 44px; height: 44px; border-radius: 8px; border: 1px dashed var(--hairline-strong); }
.exs-lib-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.exs-lib-judge { font-size: 11px; color: var(--muted); font-family: var(--mono, ui-monospace, monospace); }
.exs-lib-item button { opacity: 0; }
.exs-lib-item:hover button { opacity: 1; }
`;

const PAGE_HTML = `
  <div class="exs-head page-head">
    <h1 class="page-title">Expression Studio</h1>
    <span class="exs-sub">semantic pose axes · blind VLM judges · the body-language vocabulary bench</span>
  </div>
  <p class="exs-banner" data-el="banner">
    robot connection lost — the studio keeps working (preview + judging are local); apply-to-robot resumes when it returns
  </p>
  <div class="exs-toolbar">
    <span class="exs-pill" data-el="torquePill" title="servo torque state — /mars/arm/status">torque ?</span>
    <button class="exs-go" data-el="torqueOn" title="Stiffen the servos so the arm holds and accepts poses">Torque on</button>
    <button class="exs-warn" data-el="torqueOff" title="Go limp — also the abort path">Torque off</button>
    <button data-el="sendPose" title="Stream the current pose to the robot once (arm joints + head; base offset is preview-only)">Send pose to robot</button>
    <label title="Re-send the pose to the robot on every slider change (arm + head; base offset is preview-only)">
      <input type="checkbox" data-el="live"> drive live</label>
    <span class="spacer"></span>
    <span data-el="robotMsg" class="exs-sub"></span>
  </div>

  <div class="exs-grid">
    <div class="exs-card exs-viz">
      <div class="exs-viz-canvas" data-el="viz"></div>
      <span class="exs-viz-title">mars.urdf · expression preview</span>
      <span class="exs-viz-legend">amber ring = home spot · <b style="color:#6b93d6">blue figure</b> = the person it expresses to</span>
      <span class="exs-viz-hint">drag to orbit · scroll to zoom</span>
      <div class="exs-viz-loading" data-el="vizLoading">loading model…</div>
      <div class="exs-chips">
        <span title="synthesized arm joints j1..j6 (rad)"><b>arm</b><span class="num" data-el="cJoints">—</span></span>
        <span title="head pitch command (degrees, + = up)"><b>head</b><span class="num" data-el="cHead">—</span></span>
        <span title="base offset from home: advance, sidestep (m), turn (deg)"><b>base</b><span class="num" data-el="cBase">—</span></span>
        <button class="exs-copy" data-el="copyPose" title="Copy the full actuator pose as JSON">${ICON_COPY}</button>
      </div>
    </div>

    <div class="exs-card">
      <h2>Expression axes <span class="spacer"></span>
        <button class="exs-mini" data-el="resetAxes" title="All axes to 0, offsets cleared — back to neutral">reset</button></h2>
      <div data-el="axes"></div>
      <div class="exs-presets" data-el="presets"></div>
      <details>
        <summary>Fine-tune · per-actuator offsets</summary>
        <div data-el="offsets"></div>
        <div class="exs-row"><button class="exs-mini" data-el="clearOffsets">clear offsets</button></div>
        <div class="exs-hint">offsets add on top of the axes and export with the pose — use them to fix what the basis can't say, then fold recurring fixes back into the basis</div>
      </details>
      <details>
        <summary>Basis editor · redefine the axes</summary>
        <div data-el="basisRows"></div>
        <div class="exs-row">
          <input type="text" data-el="newAxisName" placeholder="new axis name" size="12">
          <button class="exs-mini" data-el="addAxis" title="Add an empty axis — then sculpt each extreme and capture it">+ add axis</button>
          <span class="spacer"></span>
          <button class="exs-mini exs-warn" data-el="resetBasis" title="Throw away every edit and restore the built-in basis">reset all</button>
        </div>
        <div class="exs-hint">the puppet workflow: sculpt an extreme with the axes + fine-tune, then <b>capture</b> it as that end of an axis (a delta from neutral — the slider then holds your pose at ±1). Edits persist in this browser and export with your poses; saved poses keep their exact look across basis changes.</div>
      </details>
    </div>
  </div>

  <div class="exs-bottom">
    <div class="exs-card">
      <h2>Blind VLM judges <span class="spacer"></span><span class="exs-sub" data-el="judgeStatus"></span></h2>
      <div class="exs-row">
        <label>key <input type="password" data-el="jKey" placeholder="Gemini API key" size="18"
          title="Gemini API key (dev) — stored only in this browser's localStorage, used straight from the browser"></label>
        <label>model <input type="text" data-el="jModel" size="14"></label>
        <label>judges <input type="number" data-el="jCount" min="1" max="5" step="1"
          title="Independent readings per evaluation — scores are averaged; VLM scores are noisy alone"></label>
      </div>
      <div class="exs-views" data-el="views"></div>
      <div class="exs-row">
        <button data-el="refreshViews" title="Re-render the three judge viewpoints from the current pose">Refresh views</button>
        <label>target <select data-el="target"
          title="What YOU are aiming for. Never sent to the judges — they distribute probability over all labels blind."></select></label>
        <button class="exs-go" data-el="evalBtn" title="Render the views and ask the judge panel — blind — what the pose reads as">Evaluate</button>
      </div>
      <div data-el="result" hidden>
        <div class="exs-score" data-el="score"></div>
        <div class="exs-bars" data-el="bars"></div>
        <ul class="exs-quotes" data-el="quotes"></ul>
      </div>
      <div class="exs-row" style="margin-top:10px">
        <button data-el="pinBtn" title="Hold the current pose's renders as comparison side A">Pin as A</button>
        <span class="exs-pin" data-el="pinInfo" hidden><img data-el="pinImg" alt=""><span class="exs-sub" data-el="pinName"></span></span>
        <button data-el="abBtn" title="Ask the panel which of pinned (A) vs current (B) reads more as the target — order randomized per judge">A/B vs pinned</button>
      </div>
      <div class="exs-log" data-el="log"></div>
    </div>

    <div class="exs-card">
      <h2>Pose library <span class="spacer"></span><span class="exs-sub" data-el="libCount"></span></h2>
      <div class="exs-row">
        <input type="text" data-el="poseName" placeholder="pose name" size="14">
        <button class="exs-go" data-el="saveBtn" title="Save the current axes + offsets (with thumbnail and last judge score)">Save</button>
        <button data-el="exportBtn" title="Download the library as JSON — weights AND synthesized actuator vectors, for the next pipeline steps">Export</button>
        <button data-el="importBtn" title="Import a previously exported library JSON">Import</button>
        <input type="file" data-el="importFile" accept="application/json" hidden>
      </div>
      <div class="exs-lib" data-el="lib"></div>
      <div class="exs-hint">click a pose to load it · judged poses carry their blind score</div>
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
  root.className = "exs";
  root.innerHTML = PAGE_HTML;
  stage.appendChild(root);

  /** @param {string} name */
  const el = (name) => /** @type {HTMLElement} */ (root.querySelector(`[data-el="${name}"]`));
  /** @param {string} name */
  const input = (name) => /** @type {HTMLInputElement} */ (el(name));

  let destroyed = false;
  /** @type {Awaited<ReturnType<typeof createExpressionViz>> | null} */
  let viz = null;

  // ---- pose state ---------------------------------------------------------
  // The live basis: the built-ins, overridden by whatever this browser has
  // sculpted in the basis editor.
  /** @type {import("./model.js").Axis[]} */
  let basis;
  try {
    basis = sanitizeBasis(JSON.parse(localStorage.getItem(BASIS_KEY) ?? "null"));
  } catch {
    basis = defaultBasis();
  }
  const persistBasis = () => localStorage.setItem(BASIS_KEY, JSON.stringify(basis));

  let weights = zeroWeights(basis);
  /** @type {import("./model.js").PoseDelta} */
  let offsets = {};
  const currentPose = () => synthesize(weights, offsets, basis);

  /** @type {HTMLInputElement[]} */
  const axisSliders = [];
  /** @type {HTMLElement[]} */
  const axisVals = [];
  /** @type {HTMLInputElement[]} */
  const offsetSliders = [];
  /** @type {HTMLElement[]} */
  const offsetVals = [];

  function applyPose() {
    const pose = currentPose();
    viz?.setPose(pose);
    el("cJoints").textContent = [pose.j1, pose.j2, pose.j3, pose.j4, pose.j5, pose.j6]
      .map((v) => v.toFixed(2))
      .join(" ");
    el("cHead").textContent = `${pose.head.toFixed(0)}°`;
    el("cBase").textContent = `${pose.baseX.toFixed(2)} ${pose.baseY.toFixed(2)} ${deg(pose.baseYaw).toFixed(0)}°`;
    markActivePreset();
    if (input("live").checked) queueRobot(pose);
  }

  function refreshSliders() {
    basis.forEach((axis, i) => {
      axisSliders[i].value = String(weights[axis.key] ?? 0);
      axisVals[i].textContent = (weights[axis.key] ?? 0).toFixed(2);
    });
    ACTUATORS.forEach((a, i) => {
      offsetSliders[i].value = String(offsets[a.key] ?? 0);
      offsetVals[i].textContent = (offsets[a.key] ?? 0).toFixed(a.unit === "°" ? 0 : 2);
    });
  }

  function markActivePreset() {
    const chips = el("presets").querySelectorAll("button");
    chips.forEach((chip, i) => {
      const preset = PRESETS[i];
      const active =
        Object.keys(offsets).length === 0 &&
        basis.every((axis) => Math.abs((preset.weights[axis.key] ?? 0) - (weights[axis.key] ?? 0)) < 0.005);
      chip.classList.toggle("active", active);
    });
  }

  // ---- axes UI ------------------------------------------------------------
  const GROUP_LABELS = {
    shape: "shape · the body's configuration",
    stance: "stance · where it stands vs the person",
  };

  /** Append a group eyebrow to `parent` when `axis` starts a new group —
   * suppressed while the whole basis lives in one group.
   * @param {HTMLElement} parent @param {import("./model.js").Axis} axis @param {{ g: string }} state */
  function groupEyebrow(parent, axis, state) {
    if (axis.group === state.g || new Set(basis.map((a) => a.group)).size < 2) return;
    state.g = axis.group;
    const eyebrow = document.createElement("div");
    eyebrow.className = "exs-axgroup";
    eyebrow.textContent = GROUP_LABELS[axis.group] ?? axis.group;
    parent.appendChild(eyebrow);
  }

  // Rebuilt whenever the basis editor changes the axis roster or its labels.
  function renderAxes() {
    axisSliders.length = 0;
    axisVals.length = 0;
    el("axes").innerHTML = "";
    const state = { g: "" };
    for (const axis of basis) {
      groupEyebrow(el("axes"), axis, state);
      const row = document.createElement("div");
      row.className = "exs-axis";
      row.title = axis.doc;
      row.innerHTML = `
        <div class="exs-axis-top"><span class="exs-axis-name">${escapeHtml(axis.label)}</span>
          <span class="exs-axis-val">0.00</span></div>
        <div class="exs-axis-track"><span class="exs-axis-end">${escapeHtml(axis.negLabel)}</span>
          <input type="range" min="-1" max="1" step="0.01" value="0">
          <span class="exs-axis-end hi">${escapeHtml(axis.posLabel)}</span></div>`;
      const slider = /** @type {HTMLInputElement} */ (row.querySelector("input"));
      const val = /** @type {HTMLElement} */ (row.querySelector(".exs-axis-val"));
      slider.addEventListener("input", () => {
        weights[axis.key] = +slider.value;
        val.textContent = (+slider.value).toFixed(2);
        applyPose();
      });
      slider.addEventListener("dblclick", () => {
        weights[axis.key] = 0;
        slider.value = "0";
        val.textContent = "0.00";
        applyPose();
      });
      axisSliders.push(slider);
      axisVals.push(val);
      el("axes").appendChild(row);
    }
    refreshSliders();
  }

  PRESETS.forEach((preset) => {
    const chip = document.createElement("button");
    chip.textContent = preset.label;
    chip.title = preset.doc;
    chip.addEventListener("click", () => {
      weights = sanitizeWeights(preset.weights, basis);
      offsets = {};
      refreshSliders();
      applyPose();
      input("poseName").value = preset.label;
    });
    el("presets").appendChild(chip);
  });

  // ---- basis editor -------------------------------------------------------
  /** The current pose as a delta from neutral — what a captured extreme *is*.
   * Values rounded to what the actuator meaningfully resolves. */
  function deltaFromNeutral() {
    const pose = currentPose();
    /** @type {import("./model.js").PoseDelta} */
    const delta = {};
    for (const a of ACTUATORS) {
      const d = pose[a.key] - NEUTRAL[a.key];
      if (Math.abs(d) > (a.unit === "°" ? 0.5 : 0.005)) {
        delta[a.key] = a.unit === "°" ? Math.round(d) : +d.toFixed(3);
      }
    }
    return delta;
  }

  /** Re-render everything the basis feeds and persist it. */
  function basisChanged() {
    weights = sanitizeWeights(weights, basis);
    persistBasis();
    renderAxes();
    renderBasisEditor();
    applyPose();
  }

  function renderBasisEditor() {
    el("basisRows").innerHTML = "";
    const state = { g: "" };
    basis.forEach((axis, index) => {
      groupEyebrow(el("basisRows"), axis, state);
      const row = document.createElement("div");
      row.className = "exs-bx";
      row.innerHTML = `
        <div class="exs-bx-head">
          <input type="text" value="${escapeHtml(axis.label)}" size="9" data-f="label" title="axis name">
          <input type="text" value="${escapeHtml(axis.negLabel)}" size="8" data-f="negLabel" title="label of the −1 end">
          <span class="exs-sub">⟷</span>
          <input type="text" value="${escapeHtml(axis.posLabel)}" size="8" data-f="posLabel" title="label of the +1 end">
          <span class="spacer"></span>
          ${AXES.some((d) => d.key === axis.key) ? '<button class="exs-mini" data-f="restore" title="Restore this axis\'s built-in definition">↺</button>' : ""}
          <button class="exs-mini exs-warn" data-f="del" title="Remove this axis from the basis">✕</button>
        </div>
        <div class="exs-bx-end"><b>−1</b>
          <input type="text" spellcheck="false" data-end="neg" title='the −1 extreme as a delta from neutral, e.g. {"j2":-0.5,"head":-30}'>
          <button class="exs-mini" data-cap="neg" title="Capture the CURRENT pose as this axis's −1 extreme; the slider then holds it at −1">⤓ capture</button>
          <button class="exs-mini" data-view="neg" title="Preview this extreme (sets the slider to −1)">view</button>
        </div>
        <div class="exs-bx-end"><b>+1</b>
          <input type="text" spellcheck="false" data-end="pos" title='the +1 extreme as a delta from neutral, e.g. {"j1":-0.7,"head":14}'>
          <button class="exs-mini" data-cap="pos" title="Capture the CURRENT pose as this axis's +1 extreme; the slider then holds it at +1">⤓ capture</button>
          <button class="exs-mini" data-view="pos" title="Preview this extreme (sets the slider to +1)">view</button>
        </div>`;

      for (const end of /** @type {const} */ (["neg", "pos"])) {
        const field = /** @type {HTMLInputElement} */ (row.querySelector(`input[data-end="${end}"]`));
        field.value = JSON.stringify(axis[end]);
        field.addEventListener("change", () => {
          try {
            const delta = sanitizeOffsets(JSON.parse(field.value || "{}"));
            field.classList.remove("bad");
            axis[end] = delta;
            field.value = JSON.stringify(delta);
            persistBasis();
            applyPose();
          } catch {
            field.classList.add("bad"); // left visible until a valid parse; not applied
          }
        });
        /** @type {HTMLButtonElement} */ (row.querySelector(`[data-cap="${end}"]`)).addEventListener("click", () => {
          axis[end] = deltaFromNeutral();
          weights = zeroWeights(basis);
          weights[axis.key] = end === "pos" ? 1 : -1;
          offsets = {};
          basisChanged();
          log(`captured the pose as "${axis.label}" ${end === "pos" ? "+1" : "−1"} — the slider now owns it`, "ok");
        });
        /** @type {HTMLButtonElement} */ (row.querySelector(`[data-view="${end}"]`)).addEventListener("click", () => {
          weights[axis.key] = end === "pos" ? 1 : -1;
          refreshSliders();
          applyPose();
        });
      }
      for (const f of ["label", "negLabel", "posLabel"]) {
        const field = /** @type {HTMLInputElement} */ (row.querySelector(`input[data-f="${f}"]`));
        field.addEventListener("change", () => {
          axis[/** @type {"label"|"negLabel"|"posLabel"} */ (f)] = field.value.trim() || axis.key;
          persistBasis();
          renderAxes();
        });
      }
      row.querySelector('[data-f="restore"]')?.addEventListener("click", () => {
        const builtin = defaultBasis().find((d) => d.key === axis.key);
        if (!builtin) return;
        basis[index] = builtin;
        basisChanged();
        log(`restored built-in "${builtin.label}"`);
      });
      /** @type {HTMLButtonElement} */ (row.querySelector('[data-f="del"]')).addEventListener("click", () => {
        if (!window.confirm(`Remove the "${axis.label}" axis? Saved poses keep their look (they carry actuator vectors), but its slider disappears.`)) return;
        basis.splice(index, 1);
        basisChanged();
        log(`removed axis "${axis.label}" — reset all restores the built-ins`);
      });
      el("basisRows").appendChild(row);
    });
  }

  el("addAxis").addEventListener("click", () => {
    const name = input("newAxisName").value.trim();
    if (!name) {
      input("newAxisName").focus();
      return;
    }
    let key = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "axis";
    while (basis.some((a) => a.key === key)) key += "2";
    // After the last shape axis, so the shape/stance grouping stays contiguous.
    basis.splice(basis.map((a) => a.group).lastIndexOf("shape") + 1, 0, {
      key,
      label: name,
      negLabel: "−",
      posLabel: "+",
      group: "shape",
      doc: "custom axis — sculpt each extreme with the sliders, then capture it",
      neg: {},
      pos: {},
    });
    input("newAxisName").value = "";
    basisChanged();
    log(`added axis "${name}" — capture its extremes to give it meaning`);
  });
  el("resetBasis").addEventListener("click", () => {
    if (!window.confirm("Reset ALL axes to the built-in basis? Every edited or added axis is discarded.")) return;
    basis = defaultBasis();
    basisChanged();
    log("basis reset to built-ins");
  });

  ACTUATORS.forEach((a, i) => {
    const half = (a.max - a.min) / 2;
    const row = document.createElement("div");
    row.className = "exs-off-row";
    row.innerHTML = `<span class="exs-off-name">${a.label}</span>
      <input type="range" min="${-half}" max="${half}" step="${a.step}" value="0">
      <span class="exs-off-val">0</span>`;
    const slider = /** @type {HTMLInputElement} */ (row.querySelector("input"));
    const val = /** @type {HTMLElement} */ (row.querySelector(".exs-off-val"));
    slider.addEventListener("input", () => {
      const v = +slider.value;
      if (v === 0) delete offsets[a.key];
      else offsets[a.key] = v;
      val.textContent = v.toFixed(a.unit === "°" ? 0 : 2);
      applyPose();
    });
    offsetSliders.push(slider);
    offsetVals.push(val);
    el("offsets").appendChild(row);
  });

  el("resetAxes").addEventListener("click", () => {
    weights = zeroWeights(basis);
    offsets = {};
    refreshSliders();
    applyPose();
  });
  el("clearOffsets").addEventListener("click", () => {
    offsets = {};
    refreshSliders();
    applyPose();
  });
  el("copyPose").addEventListener("click", () => {
    void copyToButton(JSON.stringify(currentPose(), null, 2), el("copyPose"), "copied");
  });

  // ---- viz ----------------------------------------------------------------
  createExpressionViz(el("viz")).then(
    (v) => {
      if (destroyed) {
        v.destroy();
        return;
      }
      viz = v;
      el("vizLoading").remove();
      applyPose();
      renderLibrary();
    },
    (err) => {
      el("vizLoading").textContent = `3D model unavailable (${err?.message || err})`;
    },
  );

  // ---- robot --------------------------------------------------------------
  /** @type {number[] | null} */
  let measuredJoints = null;
  /** @type {Pose | null} */
  let robotPending = null;
  let robotBusy = false;
  let lastHeadSent = NaN;

  /** @param {Pose} pose */
  function queueRobot(pose) {
    robotPending = pose;
    if (!robotBusy) void pumpRobot();
  }

  // Same pump as the Arm SDK sliders: keep feeding the stream's deadman until
  // the arm converges (arm joints only — an obstructed gripper never will),
  // capped so an obstructed arm isn't pushed forever.
  async function pumpRobot() {
    robotBusy = true;
    /** @type {Pose | null} */
    let lastSent = null;
    let deadline = performance.now() + 5000;
    while (!destroyed) {
      let pose = robotPending;
      robotPending = null;
      if (pose) {
        deadline = performance.now() + 5000;
      } else {
        if (!lastSent || !measuredJoints || performance.now() > deadline) break;
        const sent = [lastSent.j1, lastSent.j2, lastSent.j3, lastSent.j4, lastSent.j5];
        const err = Math.max(...sent.map((v, i) => Math.abs(v - (measuredJoints?.[i] ?? v))));
        if (err < 0.03) break;
        pose = lastSent;
      }
      lastSent = pose;
      ros.publish(STREAM_TOPIC, { data: [pose.j1, pose.j2, pose.j3, pose.j4, pose.j5, pose.j6] });
      const headDeg = Math.round(Math.min(HEAD_MAX_DEG, Math.max(HEAD_MIN_DEG, pose.head)));
      if (headDeg !== lastHeadSent) {
        ros.publish(HEAD_SET_POSITION_TOPIC, { data: headDeg });
        lastHeadSent = headDeg;
      }
      await new Promise((r) => setTimeout(r, 100));
    }
    robotBusy = false;
  }

  /** @param {string} name */
  async function armCmd(name) {
    el("robotMsg").textContent = `${name}…`;
    try {
      const { promise } = ros.sendActionGoal(ARM_ACTION, ARM_ACTION_TYPE, { command: name, params: "{}" });
      const res = await promise;
      el("robotMsg").textContent = res.success ? "" : res.message;
    } catch (e) {
      el("robotMsg").textContent = e instanceof Error ? e.message : String(e);
    }
  }
  el("torqueOn").addEventListener("click", () => void armCmd("torque_on"));
  el("torqueOff").addEventListener("click", () => {
    input("live").checked = false;
    void armCmd("torque_off");
  });
  el("sendPose").addEventListener("click", () => queueRobot(currentPose()));

  const unsubs = [
    ros.subscribe(
      ARM_STATE_TOPIC,
      (/** @type {any} */ m) => {
        if (Array.isArray(m?.position)) measuredJoints = m.position.slice(0, 6);
      },
      100,
      "sensor_msgs/msg/JointState",
    ),
    ros.subscribe(
      ARM_STATUS_TOPIC,
      (/** @type {any} */ m) => {
        if (typeof m?.is_torque_enabled !== "boolean") return;
        const pill = el("torquePill");
        pill.textContent = "torque " + (m.is_torque_enabled ? "on" : "off");
        pill.className = "exs-pill " + (m.is_torque_enabled ? "on" : "off");
      },
      undefined,
      "mars_msgs/msg/ArmStatus",
    ),
  ];
  const showBanner = () => el("banner").classList.toggle("show", ros.state !== "connected");
  const unsubConn = ros.onStateChange(showBanner);
  showBanner();

  // ---- judge --------------------------------------------------------------
  const judgeCfg = loadJudgeConfig();
  input("jKey").value = judgeCfg.apiKey;
  input("jModel").value = judgeCfg.model;
  input("jCount").value = String(judgeCfg.judges);
  for (const name of ["jKey", "jModel", "jCount"]) {
    input(name).addEventListener("change", () => {
      judgeCfg.apiKey = input("jKey").value.trim();
      judgeCfg.model = input("jModel").value.trim() || judgeCfg.model;
      judgeCfg.judges = Math.min(5, Math.max(1, +input("jCount").value || 3));
      saveJudgeConfig(judgeCfg);
    });
  }
  JUDGE_LABELS.forEach((label) => {
    const opt = document.createElement("option");
    opt.value = label;
    opt.textContent = label;
    el("target").appendChild(opt);
  });
  /** @type {HTMLSelectElement} */ (el("target")).value = "curious";

  /** @type {{ key: string, label: string, dataUrl: string }[]} */
  let lastShots = [];
  /** @type {{ target: string, p: number, stamp: number } | null} */
  let lastVerdict = null;
  /** @type {{ shots: typeof lastShots, name: string } | null} */
  let pinned = null;

  function captureViews() {
    if (!viz) return [];
    lastShots = viz.snapshot(640);
    el("views").innerHTML = lastShots
      .map((s) => `<figure><img src="${s.dataUrl}" alt="${s.label}"><figcaption>${s.label}</figcaption></figure>`)
      .join("");
    return lastShots;
  }
  el("refreshViews").addEventListener("click", captureViews);

  /** @param {string} msg @param {string} [cls] */
  function log(msg, cls) {
    const line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = `${new Date().toLocaleTimeString()}  ${msg}`;
    el("log").appendChild(line);
    el("log").scrollTop = el("log").scrollHeight;
  }

  function requireJudge() {
    if (!viz) {
      log("3D view not ready yet", "err");
      return false;
    }
    if (!judgeCfg.apiKey) {
      log("set a Gemini API key first (stored only in this browser)", "err");
      input("jKey").focus();
      return false;
    }
    return true;
  }

  el("evalBtn").addEventListener("click", async () => {
    if (!requireJudge()) return;
    const shots = captureViews();
    const target = /** @type {HTMLSelectElement} */ (el("target")).value;
    const btn = /** @type {HTMLButtonElement} */ (el("evalBtn"));
    btn.disabled = true;
    el("judgeStatus").textContent = `judging 0/${judgeCfg.judges}…`;
    try {
      const panel = await judgePanel(shots, judgeCfg, (done) => {
        el("judgeStatus").textContent = `judging ${done}/${judgeCfg.judges}…`;
      });
      if (destroyed) return;
      el("judgeStatus").textContent = "";
      if (!panel.runs.length) {
        log(`all judges failed: ${panel.errors[0] || "unknown"}`, "err");
        return;
      }
      renderPanel(panel, target);
      const p = panel.mean[target] ?? 0;
      lastVerdict = { target, p, stamp: Date.now() };
      const top = JUDGE_LABELS.reduce((a, b) => (panel.mean[a] >= panel.mean[b] ? a : b));
      log(
        `blind panel ×${panel.runs.length}: P(${target})=${p.toFixed(2)} · top read: ${top} ${panel.mean[top].toFixed(2)}` +
          (panel.errors.length ? ` · ${panel.errors.length} judge(s) failed` : ""),
        top === target ? "ok" : undefined,
      );
    } catch (e) {
      el("judgeStatus").textContent = "";
      log(`judge error: ${e instanceof Error ? e.message : e}`, "err");
    } finally {
      btn.disabled = false;
    }
  });

  /** @param {import("./judge.js").PanelResult} panel @param {string} target */
  function renderPanel(panel, target) {
    el("result").hidden = false;
    const sorted = [...JUDGE_LABELS].sort((a, b) => panel.mean[b] - panel.mean[a]);
    const p = panel.mean[target] ?? 0;
    const runnerUp = sorted.find((l) => l !== target) ?? target;
    el("score").innerHTML =
      `P(<b>${target}</b>) = <b>${p.toFixed(2)}</b> ` +
      `<span class="exs-sub">vs ${runnerUp} ${panel.mean[runnerUp].toFixed(2)} — target never shown to the judges</span>`;
    el("bars").innerHTML = sorted
      .filter((label) => panel.mean[label] >= 0.02 || label === target)
      .map((label) => {
        const spread =
          panel.runs.length > 1
            ? ` title="judges ranged ${Math.min(...panel.runs.map((r) => r.probabilities[label])).toFixed(2)}–${Math.max(...panel.runs.map((r) => r.probabilities[label])).toFixed(2)}"`
            : "";
        return `<div class="exs-bar${label === target ? " target" : ""}"${spread}>
          <span class="lbl">${label}</span>
          <span class="track"><span class="fill" style="width:${(panel.mean[label] * 100).toFixed(1)}%"></span></span>
          <span class="num">${panel.mean[label].toFixed(2)}</span></div>`;
      })
      .join("");
    const cues = [...new Set(panel.runs.flatMap((r) => r.cues))].slice(0, 5);
    const impressions = panel.runs.map((r) => r.impression).filter(Boolean).slice(0, 3);
    el("quotes").innerHTML = [
      ...impressions.map((t) => `<li>“${t}”</li>`),
      ...cues.map((t) => `<li class="exs-sub">cue: ${t}</li>`),
    ].join("");
  }

  el("pinBtn").addEventListener("click", () => {
    if (!viz) return;
    const shots = captureViews();
    pinned = { shots, name: input("poseName").value.trim() || "pinned" };
    el("pinInfo").hidden = false;
    /** @type {HTMLImageElement} */ (el("pinImg")).src = shots[1]?.dataUrl ?? shots[0]?.dataUrl ?? "";
    el("pinName").textContent = pinned.name;
    log(`pinned "${pinned.name}" as A`);
  });

  el("abBtn").addEventListener("click", async () => {
    if (!requireJudge()) return;
    if (!pinned) {
      log("pin a pose as A first", "err");
      return;
    }
    const current = captureViews();
    const target = /** @type {HTMLSelectElement} */ (el("target")).value;
    const btn = /** @type {HTMLButtonElement} */ (el("abBtn"));
    btn.disabled = true;
    el("judgeStatus").textContent = "comparing…";
    try {
      const pins = pinned;
      const verdicts = await Promise.all(
        Array.from({ length: judgeCfg.judges }, () => judgePair(pins.shots, current, target, judgeCfg)),
      );
      if (destroyed) return;
      const bWins = verdicts.filter((v) => v.winner === "B").length;
      const winner = bWins * 2 >= verdicts.length ? "current" : `pinned "${pins.name}"`;
      log(
        `A/B for "${target}": ${winner} wins ${Math.max(bWins, verdicts.length - bWins)}/${verdicts.length} — ${verdicts[0].reason}`,
        winner === "current" ? "ok" : undefined,
      );
    } catch (e) {
      log(`compare error: ${e instanceof Error ? e.message : e}`, "err");
    } finally {
      el("judgeStatus").textContent = "";
      btn.disabled = false;
    }
  });

  // ---- library ------------------------------------------------------------
  /** @returns {SavedPose[]} */
  function loadLibrary() {
    try {
      const raw = JSON.parse(localStorage.getItem(LIBRARY_KEY) || "[]");
      return Array.isArray(raw) ? raw : [];
    } catch {
      return [];
    }
  }
  /** @param {SavedPose[]} poses */
  function saveLibrary(poses) {
    try {
      localStorage.setItem(LIBRARY_KEY, JSON.stringify(poses));
    } catch {
      // Quota — drop the heaviest payload (thumbnails) and retry once.
      try {
        localStorage.setItem(LIBRARY_KEY, JSON.stringify(poses.map((p) => ({ ...p, thumb: null }))));
      } catch {
        log("library save failed (storage full)", "err");
      }
    }
  }
  let library = loadLibrary();

  /**
   * Resolve a stored/imported pose against the LIVE basis. Weights are pruned
   * to known axes; when the entry carries its exact actuator vector, the
   * residual (from basis edits since it was authored) folds into offsets so
   * the pose looks identical — the semantic weights survive where they still
   * apply. Two passes: the second absorbs clamping interactions.
   * @param {any} entryWeights @param {any} entryOffsets @param {import("./model.js").PoseDelta | null} actuators
   */
  function resolvePose(entryWeights, entryOffsets, actuators) {
    const w = sanitizeWeights(entryWeights, basis);
    let off = sanitizeOffsets(entryOffsets);
    if (actuators) {
      for (let pass = 0; pass < 2; pass++) {
        const synth = synthesize(w, off, basis);
        for (const a of ACTUATORS) {
          const v = actuators[a.key];
          if (typeof v === "number" && Math.abs(v - synth[a.key]) > 1e-4) {
            off[a.key] = (off[a.key] ?? 0) + v - synth[a.key];
          }
        }
      }
      off = sanitizeOffsets(off);
    }
    return { weights: w, offsets: off };
  }

  /** Thumbnail for an arbitrary pose: pose in, snapshot, current pose back
   * (the view eases home afterwards). @param {Pose} pose */
  function thumbFor(pose) {
    if (!viz) return null;
    viz.setPose(pose);
    const [shot] = viz.snapshot(160, ["quarter"]);
    viz.setPose(currentPose());
    return shot?.dataUrl ?? null;
  }

  function renderLibrary() {
    el("libCount").textContent = library.length ? `${library.length} pose${library.length > 1 ? "s" : ""}` : "";
    el("lib").innerHTML = "";
    for (const pose of library) {
      const item = document.createElement("div");
      item.className = "exs-lib-item";
      item.innerHTML = `${pose.thumb ? `<img src="${pose.thumb}" alt="">` : '<span class="noimg"></span>'}
        <span class="exs-lib-name">${escapeHtml(pose.name)}
          ${pose.judge ? `<div class="exs-lib-judge">${escapeHtml(pose.judge.target)} ${pose.judge.p.toFixed(2)}</div>` : ""}</span>
        <button class="exs-mini exs-warn" title="Delete this pose">✕</button>`;
      item.addEventListener("click", () => {
        ({ weights, offsets } = resolvePose(pose.weights, pose.offsets, actuatorNumbers(pose.actuators)));
        refreshSliders();
        applyPose();
        input("poseName").value = pose.name;
        log(`loaded "${pose.name}"`);
      });
      /** @type {HTMLButtonElement} */ (item.querySelector("button")).addEventListener("click", (e) => {
        e.stopPropagation();
        library = library.filter((p) => p.id !== pose.id);
        saveLibrary(library);
        renderLibrary();
      });
      el("lib").appendChild(item);
    }
  }

  el("saveBtn").addEventListener("click", () => {
    const name = input("poseName").value.trim() || `pose ${library.length + 1}`;
    library.unshift({
      id: crypto.randomUUID(),
      name,
      weights: { ...weights },
      offsets: { ...offsets },
      notes: "",
      thumb: thumbFor(currentPose()),
      actuators: currentPose(),
      judge: lastVerdict,
    });
    library = library.slice(0, MAX_POSES);
    saveLibrary(library);
    renderLibrary();
    log(`saved "${name}"${lastVerdict ? ` with blind P(${lastVerdict.target})=${lastVerdict.p.toFixed(2)}` : ""}`, "ok");
  });

  el("exportBtn").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(exportPayload(library, basis), null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "mars-expression-poses.json";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  el("importBtn").addEventListener("click", () => input("importFile").click());
  input("importFile").addEventListener("change", async () => {
    const file = input("importFile").files?.[0];
    input("importFile").value = "";
    if (!file) return;
    try {
      const { basis: fileBasis, poses: entries } = parseImport(await file.text());
      if (fileBasis && JSON.stringify(fileBasis) !== JSON.stringify(basis)) {
        if (window.confirm("This file carries its own axis basis. Adopt it (replaces your current axes)? The poses import correctly either way.")) {
          basis = fileBasis;
          basisChanged();
          log("adopted the file's basis", "ok");
        }
      }
      for (const entry of entries) {
        const resolved = resolvePose(entry.weights, entry.offsets, entry.actuators);
        library.unshift({
          id: crypto.randomUUID(),
          name: entry.name,
          weights: resolved.weights,
          offsets: resolved.offsets,
          notes: entry.notes,
          thumb: thumbFor(synthesize(resolved.weights, resolved.offsets, basis)),
          actuators: entry.actuators,
          judge: null,
        });
      }
      library = library.slice(0, MAX_POSES);
      saveLibrary(library);
      renderLibrary();
      log(`imported ${entries.length} pose(s)`, "ok");
    } catch (e) {
      log(`import failed: ${e instanceof Error ? e.message : e}`, "err");
    }
  });

  /** @param {string} s */
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  }

  renderAxes();
  renderBasisEditor();
  applyPose();
  renderLibrary();
  log("ready — shape a pose on the axes, then Evaluate to hear what blind judges read");

  return {
    destroy() {
      destroyed = true;
      robotPending = null;
      for (const unsub of unsubs) unsub();
      unsubConn();
      viz?.destroy();
      root.remove();
    },
  };
}
