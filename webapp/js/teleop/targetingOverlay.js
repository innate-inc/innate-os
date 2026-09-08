// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Targeting overlay — what a manipulation skill is doing, drawn over the robot's
// cameras. On the head camera: the object it detected (corner brackets), the
// pick box the base steers it into, the tracked point gliding toward that box
// and the spot the fingers will close on. On the wrist camera: the servo's goal
// square, Gemini's wrist detection and the colour blob the descent follows,
// with its long axis. Plus a HUD of the run's stages on either. Driven entirely
// by /brain/skill_telemetry: a run declares its stages up front and every
// marker arrives in image pixels, so nothing here is specific to one skill.

import { SKILL_TELEMETRY_TOPIC } from "../constants.js";
import { primaryCameraName } from "./trajectoryOverlay.js";

// The sim renders the head camera with the driver's lens
// (mars_sim_driver/constants.py — keep in sync). The viewer's preview shares
// its vertical field, so the skill's frame fits the stage by height, runs wider
// sideways because that lens has fx < fy, and centres on the lens's principal
// point rather than the frame's middle. The wrist camera is a plain fovy in
// both (square pixels, centred), so its frame just fits by height.
const SIM_HEAD_LENS = { fx: 200.3, fy: 267.3, cx: 319.1, cy: 248.7 };
// Half-length of the blob's drawn long axis, in frame px.
const AXIS_HALF_PX = 45;
// How long the verdict stays up after the run ends.
const RESULT_LINGER_MS = 4000;
// A page opened mid-run missed the start event that carries the frame size and
// the stage list: it draws in the head camera's native frame and learns the
// stages, prompt and pick box from the events that repeat them, rather than
// showing nothing until the next run.
const DEFAULT_FRAME = { w: 640, h: 480 };

/** @typedef {{ w: number, h: number }} Size */
/** @typedef {[number, number]} Px */
/** @typedef {[number, number, number, number]} Corners x0, y0, x1, y1 */
/** @typedef {{ cu: number, cv: number, hu: number, hv: number, au: number, av: number }} PickBox */
/** @typedef {{ u: number, v: number, half: number }} WristBox */
/** @typedef {{ left: number, top: number, width: number, height: number }} Rect */
/** @typedef {"main" | "arm"} View the two robot cameras a run draws on */
/**
 * The stream as laid out on the page: intrinsic size, the video element's
 * content box within the stage, and how object-fit / object-position place the
 * picture in that box (position as the computed "x y" string).
 * @typedef {{ w: number, h: number, box: Rect, fit: string, position: string }} VideoLayout
 */

/** One object-position component -> where the picture's slack goes.
 * @param {string} term "50%" or "12px" @param {number} slack box minus picture */
function positionOffset(term, slack) {
  if (term.endsWith("%")) return (slack * parseFloat(term)) / 100;
  if (term.endsWith("px")) return parseFloat(term);
  return slack / 2;
}
/**
 * One run of a skill, as the overlay understands it.
 * @typedef {{
 *   skill: string,
 *   prompt: string,
 *   stages: string[],
 *   frame: Size,
 *   box: PickBox | null,
 *   wristBox: WristBox | null,
 *   stage: string | null,
 *   looking: boolean,
 *   seen: { px: Px | null, box: Corners | null } | null,
 *   track: { px: Px, inside: boolean } | null,
 *   grasp: Px | null,
 *   wristSeen: { px: Px | null, box: Corners | null } | null,
 *   wristTrack: { px: Px, inside: boolean, axis: number | null } | null,
 *   descent: { top: number, z: number, stop: number } | null,
 *   readout: string,
 *   result: { ok: boolean, text: string } | null,
 * }} Run
 */

/**
 * What to draw for one camera: every geometry in frame px, null = not drawn.
 * @typedef {{
 *   seen: Corners | null,
 *   goal: { rect: Rect, accept: Size | null, inside: boolean } | null,
 *   track: { px: Px, inside: boolean } | null,
 *   vec: [Px, Px] | null,
 *   axis: { px: Px, angle: number } | null,
 *   reticle: Px | null,
 *   tags: { seen: string, goal: string, track: string },
 * }} Markers
 */

/**
 * Where the skill's image frame lands on the stage. On hardware the skill sees
 * the same picture the stream shows, so the frame is wherever object-fit put
 * that picture inside the video element — the cockpits letterbox it (contain),
 * the Agent page crops it (cover) at some widths. The sim has no stream: its
 * canvas renders the camera at the stage's own size (see SIM_HEAD_LENS).
 * @param {Size} frame the skill's image size
 * @param {VideoLayout | null} video the stream on the page, null in the sim
 * @param {number} cw @param {number} ch stage size
 * @param {View} [view] which camera fills the stage (the sim's lens differs)
 * @returns {Rect | null}
 */
export function frameRect(frame, video, cw, ch, view = "main") {
  if (!cw || !ch || !frame.w || !frame.h) return null;
  if (video) {
    const { w, h, box, fit } = video;
    if (!w || !h || !box.width || !box.height) return null;
    let sx = box.width / w;
    let sy = box.height / h;
    if (fit === "cover") sx = sy = Math.max(sx, sy);
    else if (fit === "none") sx = sy = 1;
    else if (fit !== "fill") sx = sy = Math.min(sx, sy);
    const width = w * sx;
    const height = h * sy;
    const [px = "50%", py = "50%"] = (video.position || "").split(/\s+/);
    return {
      left: box.left + positionOffset(px, box.width - width),
      top: box.top + positionOffset(py, box.height - height),
      width,
      height,
    };
  }
  const lens = view === "main" ? SIM_HEAD_LENS : { fx: 1, fy: 1, cx: frame.w / 2, cy: frame.h / 2 };
  const sy = ch / frame.h;
  const sx = sy * (lens.fy / lens.fx);
  return { left: cw / 2 - lens.cx * sx, top: ch / 2 - lens.cy * sy, width: frame.w * sx, height: frame.h * sy };
}

/** @param {any} v @returns {v is Px} */
function isPx(v) {
  return Array.isArray(v) && v.length >= 2 && Number.isFinite(v[0]) && Number.isFinite(v[1]);
}

/** @param {any} v @returns {v is Corners} */
function isBox(v) {
  return Array.isArray(v) && v.length >= 4 && v.slice(0, 4).every(Number.isFinite);
}

/** @param {any} v @returns {PickBox | null} */
function pickBox(v) {
  if (!v || typeof v !== "object") return null;
  const b = { cu: v.cu, cv: v.cv, hu: v.hu, hv: v.hv, au: v.au, av: v.av };
  return Object.values(b).every((n) => Number.isFinite(n) && n >= 0) && b.hu > 0 && b.hv > 0 ? b : null;
}

/** @param {any} v @returns {WristBox | null} */
function wristBox(v) {
  if (!v || typeof v !== "object") return null;
  const b = { u: v.u, v: v.v, half: v.half };
  return Object.values(b).every(Number.isFinite) && b.half > 0 ? b : null;
}

/** @param {any} v @returns {{ px: Px | null, box: Corners | null }} */
function sighting(v) {
  return { px: isPx(v.px) ? [v.px[0], v.px[1]] : null, box: isBox(v.box) ? [v.box[0], v.box[1], v.box[2], v.box[3]] : null };
}

/** @param {number} n */
function metres(n) {
  return n >= 1 ? `${n.toFixed(2)} m` : `${Math.round(n * 100)} cm`;
}

/** @type {Record<string, string>} what each stage reads as while nothing finer is known */
const STAGE_READOUT = {
  search: "scanning the floor",
  approach: "steering onto it",
  align: "aligning the wrist camera",
  grasp: "reaching down",
  verify: "backing up to check",
};

/** @param {any} ev a run-start event, or any first event of a run missed at its start
 * @returns {Run} */
function startRun(ev) {
  const frame = Array.isArray(ev.frame) ? { w: Number(ev.frame[0]), h: Number(ev.frame[1]) } : DEFAULT_FRAME;
  return {
    skill: typeof ev.skill === "string" ? ev.skill : "skill",
    prompt: typeof ev.prompt === "string" ? ev.prompt : "",
    stages: Array.isArray(ev.stages) ? ev.stages.filter((/** @type {unknown} */ s) => typeof s === "string") : [],
    frame: frame.w > 0 && frame.h > 0 ? frame : DEFAULT_FRAME,
    box: pickBox(ev.box),
    wristBox: wristBox(ev.wrist_box),
    stage: null,
    looking: false,
    seen: null,
    track: null,
    grasp: null,
    wristSeen: null,
    wristTrack: null,
    descent: null,
    readout: "starting",
    result: null,
  };
}

/**
 * Fold one telemetry event into the run. Returns the run to keep showing, or
 * null when there is none. A run ends with its result set; the caller decides
 * how long that lingers.
 * @param {Run | null} run
 * @param {any} ev
 * @returns {Run | null}
 */
export function applyEvent(run, ev) {
  if (!ev || typeof ev !== "object" || typeof ev.ev !== "string" || typeof ev.skill !== "string") return run;
  if (ev.ev === "run" && ev.state === "start") return startRun(ev);
  if (ev.ev === "run" && ev.state !== "end") return run;
  if (run?.result || (run && ev.skill !== run.skill)) return run;
  if (!run) {
    if (ev.ev === "run") return null; // the end of a run never seen
    run = startRun(ev);
  }
  switch (ev.ev) {
    case "run":
      if (ev.state !== "end") break;
      run.seen = run.track = run.grasp = run.wristSeen = run.wristTrack = null;
      run.looking = false;
      run.result = ev.ok
        ? { ok: true, text: "picked up" }
        : { ok: false, text: ev.cancelled ? "stopped" : `failed · ${ev.reason || "no reason given"}` };
      run.readout = run.result.text;
      break;
    case "stage":
      if (typeof ev.stage !== "string") break;
      if (!run.stages.includes(ev.stage)) run.stages.push(ev.stage);
      if (!run.prompt && typeof ev.prompt === "string") run.prompt = ev.prompt;
      run.stage = ev.stage;
      run.readout = STAGE_READOUT[ev.stage] ?? ev.stage;
      if (ev.stage !== "approach") run.track = null;
      if (ev.stage !== "align") run.wristSeen = run.wristTrack = null; // the arm moves on, the view with it
      if (ev.stage === "verify") run.grasp = null;
      break;
    case "look":
      run.looking = ev.state === "ask";
      if (ev.state === "ask") {
        run.readout = run.stage === "search" ? "looking for it" : "taking another look";
      } else if (ev.state === "seen") {
        run.seen = sighting(ev);
        run.track = null;
        const dist = Number.isFinite(ev.dist) ? ` · ${metres(ev.dist)} ahead` : "";
        const more = Number.isFinite(ev.n) && ev.n > 1 ? ` · ${ev.n} matches` : "";
        run.readout = `spotted${dist}${more}`;
      } else {
        run.seen = run.track = null;
        run.readout = "not in view";
      }
      break;
    case "move": {
      // The picture is about to change under every pixel marker.
      run.seen = run.track = run.grasp = null;
      const amount = Number(ev.amount) || 0;
      if (ev.kind === "rotate") {
        const deg = Math.round(Math.abs(amount) * (180 / Math.PI));
        run.readout = `turning ${deg}° ${amount >= 0 ? "left" : "right"}`;
      } else {
        run.readout = `${amount >= 0 ? "driving" : "backing up"} ${metres(Math.abs(amount))}`;
      }
      break;
    }
    case "track": {
      if (!isPx(ev.px)) break;
      run.box ??= pickBox(ev.box);
      run.seen = null;
      run.track = { px: [ev.px[0], ev.px[1]], inside: ev.inside === true };
      const off = run.box ? Math.round(Math.hypot(ev.px[0] - run.box.cu, ev.px[1] - run.box.cv)) : null;
      run.readout = run.track.inside ? "in the pick box" : off === null ? "tracking" : `steering · ${off} px off`;
      break;
    }
    case "parked":
      if (run.track) run.track.inside = true;
      run.readout = "parked over it";
      break;
    case "grasp":
      if (ev.step === "target") {
        run.grasp = isPx(ev.px) ? [ev.px[0], ev.px[1]] : null;
        run.readout = "grasp point set";
      } else if (ev.step === "descend") run.readout = "reaching to the floor";
      else if (ev.step === "close") run.readout = "closing the gripper";
      else if (ev.step === "lift") run.readout = "lifting";
      break;
    case "wrist":
      if (ev.state === "seed") {
        run.wristSeen = isPx(ev.px) ? sighting(ev) : null;
        run.wristTrack = null;
        run.readout = run.wristSeen ? "wrist camera has it" : "wrist camera can't see it";
      } else if (ev.state === "track" && Number.isFinite(ev.z)) {
        const stop = Number.isFinite(ev.stop) ? ev.stop : 0;
        run.descent = { top: run.descent?.top ?? ev.z, z: ev.z, stop };
        run.wristBox ??= wristBox(ev.box);
        if (isPx(ev.px)) {
          run.wristSeen = null;
          run.wristTrack = { px: [ev.px[0], ev.px[1]], inside: ev.inside === true, axis: Number.isFinite(ev.axis) ? ev.axis : null };
        }
        run.readout = `${ev.inside ? "descending" : "centring"} · ${Math.round(ev.z * 100)} cm up`;
      } else if (ev.state === "done") {
        run.readout = typeof ev.reason === "string" ? `wrist align: ${ev.reason}` : "wrist align done";
      }
      break;
    case "verify":
      run.readout = ev.held ? "holding it" : "missed it";
      break;
    default:
      break;
  }
  return run;
}

/** @param {Corners} c @returns {Rect} */
function cornersRect([x0, y0, x1, y1]) {
  return { left: x0, top: y0, width: x1 - x0, height: y1 - y0 };
}

/** @param {Px} px @param {number} half @returns {Rect} */
function squareAround(px, half) {
  return { left: px[0] - half, top: px[1] - half, width: 2 * half, height: 2 * half };
}

/** A sighting's frame: its box, or a small square around its point.
 * @param {{ px: Px | null, box: Corners | null } | null} s @returns {Corners | null} */
function seenCorners(s) {
  if (s?.box) return s.box;
  if (s?.px) return [s.px[0] - 20, s.px[1] - 20, s.px[0] + 20, s.px[1] + 20];
  return null;
}

/** The head camera's markers: detection, pick box, flow track, grasp point.
 * @param {Run} run @returns {Markers} */
export function headMarkers(run) {
  const { box, track } = run;
  const goal =
    box && run.stage === "approach"
      ? {
          rect: { left: box.cu - box.hu, top: box.cv - box.hv, width: 2 * box.hu, height: 2 * box.hv },
          accept: { w: box.au / box.hu, h: box.av / box.hv },
          inside: !!track?.inside,
        }
      : null;
  return {
    seen: seenCorners(run.seen),
    goal,
    track,
    vec: track && box && !track.inside ? [track.px, [box.cu, box.cv]] : null,
    axis: null,
    reticle: run.grasp,
    tags: { seen: "target", goal: "pick box", track: "tracking" },
  };
}

/** The wrist camera's markers: Gemini's seed, the servo box, the tracked blob.
 * @param {Run} run @returns {Markers} */
export function wristMarkers(run) {
  const { wristBox: box, wristTrack: track } = run;
  const goal =
    box && run.stage === "align"
      ? { rect: squareAround([box.u, box.v], box.half), accept: null, inside: !!track?.inside }
      : null;
  return {
    seen: seenCorners(run.wristSeen),
    goal,
    track: track && { px: track.px, inside: track.inside },
    vec: track && box && !track.inside ? [track.px, [box.u, box.v]] : null,
    axis: track && track.axis !== null ? { px: track.px, angle: track.axis } : null,
    reticle: null,
    tags: { seen: "wrist target", goal: "wrist box", track: "blob" },
  };
}

const SVG_OPEN =
  '<svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="currentColor" ' +
  'stroke-width="1.6" aria-hidden="true">';
// Crosshair: where the fingers close.
const RETICLE_SVG =
  '<circle cx="24" cy="24" r="13"/>' +
  '<line x1="24" y1="3" x2="24" y2="14"/><line x1="24" y1="34" x2="24" y2="45"/>' +
  '<line x1="3" y1="24" x2="14" y2="24"/><line x1="34" y1="24" x2="45" y2="24"/>' +
  '<circle cx="24" cy="24" r="2.2" fill="currentColor" stroke="none"/>';
// Small diamond: the point optical flow is holding on to.
const TRACK_SVG = '<path d="M24 11 37 24 24 37 11 24Z"/><circle cx="24" cy="24" r="1.8" fill="currentColor" stroke="none"/>';

/**
 * @param {HTMLElement} stage the .video-stage the markers pin to
 * @param {HTMLVideoElement | null} video the stage's video on hardware; null in
 *   the sim, where a canvas renders the head camera at the stage's size
 * @param {import("../rosClient.js").RosClient} ros
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ destroy: () => void }}
 */
export function createTargetingOverlay(stage, video, ros, session) {
  const clip = document.createElement("div");
  clip.className = "tgt-clip";
  clip.hidden = true;
  // The skill's frame, placed over the picture; every marker is a percentage
  // of it, so a resize only moves this one element.
  const layer = document.createElement("div");
  layer.className = "tgt-layer";
  layer.innerHTML =
    '<div class="tgt-scan"></div>' +
    '<svg class="tgt-vec" preserveAspectRatio="none" aria-hidden="true">' +
    '<line class="tgt-vec-line"/><line class="tgt-axis-line"/></svg>' +
    '<div class="tgt-seen" hidden><i></i><i></i><i></i><i></i><span class="tgt-tag mono">target</span></div>' +
    '<div class="tgt-goal" hidden><span class="tgt-tag mono">pick box</span><div class="tgt-accept"></div></div>' +
    `<div class="tgt-track" hidden>${SVG_OPEN}${TRACK_SVG}</svg><span class="tgt-tag mono">tracking</span></div>` +
    `<div class="tgt-reticle" hidden>${SVG_OPEN}${RETICLE_SVG}</svg><span class="tgt-tag mono">grasp</span></div>`;
  clip.appendChild(layer);

  const hud = document.createElement("div");
  hud.className = "overlay tgt-hud";
  hud.hidden = true;
  hud.innerHTML =
    '<div class="tgt-hud-head"><span class="microlabel tgt-skill"></span><span class="tgt-prompt mono"></span></div>' +
    '<ol class="tgt-stages"></ol>' +
    '<div class="tgt-readout mono"><span class="tgt-readout-text"></span><span class="tgt-bar" hidden><span class="tgt-bar-fill"></span></span></div>';
  stage.append(clip, hud);

  const q = (/** @type {string} */ sel) => /** @type {HTMLElement} */ (layer.querySelector(sel));
  const seenEl = q(".tgt-seen");
  const goalEl = q(".tgt-goal");
  const acceptEl = q(".tgt-accept");
  const trackEl = q(".tgt-track");
  const reticleEl = q(".tgt-reticle");
  const vecEl = /** @type {SVGSVGElement} */ (/** @type {unknown} */ (layer.querySelector(".tgt-vec")));
  const vecLine = /** @type {SVGLineElement} */ (/** @type {unknown} */ (layer.querySelector(".tgt-vec-line")));
  const axisLine = /** @type {SVGLineElement} */ (/** @type {unknown} */ (layer.querySelector(".tgt-axis-line")));
  const seenTag = /** @type {HTMLElement} */ (seenEl.querySelector(".tgt-tag"));
  const goalTag = /** @type {HTMLElement} */ (goalEl.querySelector(".tgt-tag"));
  const trackTag = /** @type {HTMLElement} */ (trackEl.querySelector(".tgt-tag"));
  const skillEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-skill"));
  const promptEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-prompt"));
  const stagesEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-stages"));
  const readoutEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-readout-text"));
  const barEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-bar"));
  const barFill = /** @type {HTMLElement} */ (hud.querySelector(".tgt-bar-fill"));

  /** @type {Run | null} */
  let run = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let lingerTimer;

  /** The robot camera on the stage, or null when another view (map, orbit) is up.
   * @returns {View | null} */
  function view() {
    const name = primaryCameraName(session);
    return name === "main" || name === "arm" ? name : null;
  }

  /** @param {HTMLElement} el @param {Px} px @param {Size} frame */
  function pin(el, px, frame) {
    el.style.left = `${(px[0] / frame.w) * 100}%`;
    el.style.top = `${(px[1] / frame.h) * 100}%`;
    el.hidden = false;
  }

  /** @param {HTMLElement} el @param {Rect} r @param {Size} frame */
  function place(el, r, frame) {
    el.style.left = `${(r.left / frame.w) * 100}%`;
    el.style.top = `${(r.top / frame.h) * 100}%`;
    el.style.width = `${(r.width / frame.w) * 100}%`;
    el.style.height = `${(r.height / frame.h) * 100}%`;
    el.hidden = false;
  }

  /** @param {SVGLineElement} line @param {Px} a @param {Px} b */
  function segment(line, a, b) {
    line.setAttribute("x1", String(a[0]));
    line.setAttribute("y1", String(a[1]));
    line.setAttribute("x2", String(b[0]));
    line.setAttribute("y2", String(b[1]));
    line.classList.add("on");
  }

  /** @returns {VideoLayout | null} */
  function videoLayout() {
    if (!video) return null;
    const s = stage.getBoundingClientRect();
    const v = video.getBoundingClientRect();
    const style = getComputedStyle(video);
    // object-fit lays the picture out in the content box, inside any border.
    return {
      w: video.videoWidth,
      h: video.videoHeight,
      box: {
        left: v.left - s.left + video.clientLeft,
        top: v.top - s.top + video.clientTop,
        width: video.clientWidth,
        height: video.clientHeight,
      },
      fit: style.objectFit || "contain",
      position: style.objectPosition || "50% 50%",
    };
  }

  function placeLayer() {
    if (!run) return;
    const rect = frameRect(run.frame, videoLayout(), stage.clientWidth, stage.clientHeight, view() ?? "main");
    layer.hidden = !rect;
    if (!rect) return;
    layer.style.left = `${rect.left}px`;
    layer.style.top = `${rect.top}px`;
    layer.style.width = `${rect.width}px`;
    layer.style.height = `${rect.height}px`;
  }

  function renderStages() {
    if (!run) return;
    const doneUpTo = run.stage ? run.stages.indexOf(run.stage) : -1;
    stagesEl.replaceChildren(
      ...run.stages.map((name, i) => {
        const li = document.createElement("li");
        li.className = "tgt-stage";
        li.textContent = name;
        const done = run?.result?.ok ? true : i < doneUpTo;
        li.classList.toggle("done", done);
        li.classList.toggle("active", !run?.result && i === doneUpTo);
        return li;
      }),
    );
  }

  function render() {
    const cam = view();
    const show = !!run && cam !== null;
    clip.hidden = !show || !!run?.result;
    hud.hidden = !show;
    if (!run || !cam) return;
    placeLayer();

    skillEl.textContent = run.skill.replace(/_/g, " ");
    promptEl.textContent = run.prompt ? `“${run.prompt}”` : "";
    renderStages();
    readoutEl.textContent = run.readout;
    hud.classList.toggle("ok", !!run.result?.ok);
    hud.classList.toggle("failed", !!run.result && !run.result.ok);
    hud.classList.toggle("looking", run.looking);
    if (run.descent) {
      const span = run.descent.top - run.descent.stop;
      const done = span > 0 ? Math.min(1, Math.max(0, (run.descent.top - run.descent.z) / span)) : 1;
      barFill.style.width = `${done * 100}%`;
      barEl.hidden = false;
    } else {
      barEl.hidden = true;
    }
    if (run.result) return;

    const { frame } = run;
    // The sweep is a head-camera look; the wrist stage tracks continuously.
    layer.classList.toggle("looking", run.looking && cam === "main");
    const m = cam === "main" ? headMarkers(run) : wristMarkers(run);
    seenTag.textContent = m.tags.seen;
    goalTag.textContent = m.tags.goal;
    trackTag.textContent = m.tags.track;

    if (m.seen) place(seenEl, cornersRect(m.seen), frame);
    else seenEl.hidden = true;

    if (m.goal) {
      place(goalEl, m.goal.rect, frame);
      acceptEl.hidden = !m.goal.accept;
      if (m.goal.accept) {
        acceptEl.style.width = `${m.goal.accept.w * 100}%`;
        acceptEl.style.height = `${m.goal.accept.h * 100}%`;
      }
      goalEl.classList.toggle("inside", m.goal.inside);
    } else {
      goalEl.hidden = true;
    }

    if (m.track) {
      pin(trackEl, m.track.px, frame);
      trackEl.classList.toggle("inside", m.track.inside);
    } else {
      trackEl.hidden = true;
    }

    vecEl.setAttribute("viewBox", `0 0 ${frame.w} ${frame.h}`);
    // The steering vector: tracked point -> the box it is being driven into.
    if (m.vec) segment(vecLine, m.vec[0], m.vec[1]);
    else vecLine.classList.remove("on");
    // The blob's long axis: the side the fingers will roll onto.
    if (m.axis) {
      const [u, v] = m.axis.px;
      const du = Math.cos(m.axis.angle) * AXIS_HALF_PX;
      const dv = Math.sin(m.axis.angle) * AXIS_HALF_PX;
      segment(axisLine, [u - du, v - dv], [u + du, v + dv]);
    } else {
      axisLine.classList.remove("on");
    }

    if (m.reticle) pin(reticleEl, m.reticle, frame);
    else reticleEl.hidden = true;
  }

  /** @param {any} msg std_msgs/String */
  function onTelemetry(msg) {
    if (typeof msg?.data !== "string") return;
    /** @type {any} */
    let ev;
    try {
      ev = JSON.parse(msg.data);
    } catch {
      return;
    }
    const ended = !!run?.result;
    const next = applyEvent(run, ev);
    if (next !== run) clearTimeout(lingerTimer); // a new run replaces a lingering verdict
    run = next;
    // Only the transition into a verdict arms the linger: later events of a
    // finished run (or a stray one) must not keep pushing it out.
    if (run?.result && !ended) {
      lingerTimer = setTimeout(() => {
        run = null;
        render();
      }, RESULT_LINGER_MS);
    }
    render();
  }

  const unsub = ros.subscribe(SKILL_TELEMETRY_TOPIC, onTelemetry, undefined, "std_msgs/msg/String");
  const resize = new ResizeObserver(placeLayer);
  resize.observe(stage);
  if (video) resize.observe(video); // its box moves with media queries the stage's size does not
  video?.addEventListener("resize", placeLayer);
  const unsubSession = session.onChange(render);

  return {
    destroy() {
      unsub();
      unsubSession();
      resize.disconnect();
      video?.removeEventListener("resize", placeLayer);
      clearTimeout(lingerTimer);
      clip.remove();
      hud.remove();
    },
  };
}
