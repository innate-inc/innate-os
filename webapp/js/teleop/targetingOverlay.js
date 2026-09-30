// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Targeting overlay — what a running skill draws over the robot's cameras.
// Driven entirely by /brain/skill_overlay (Skill.overlay in the SDK): a run
// declares its stage ladder, keeps one readout line and a progress bar in a
// HUD, and places markers by id — brackets, boxes, points, reticles, lines —
// in image pixels of its frame, on the head ("main") or wrist ("arm") camera.
// Nothing here is specific to one skill.

import { SKILL_OVERLAY_TOPIC } from "../constants.js";
import { ros } from "../rosClient.js";
import { primaryCameraName } from "./trajectoryOverlay.js";

// The sim renders the head camera with the driver's lens
// (mars_sim_driver/constants.py — keep in sync). The viewer's preview shares
// its vertical field, so the skill's frame fits the stage by height, runs wider
// sideways because that lens has fx < fy, and centres on the lens's principal
// point rather than the frame's middle. The wrist camera is a plain fovy in
// both (square pixels, centred), so its frame just fits by height.
const SIM_HEAD_LENS = { fx: 200.3, fy: 267.3, cx: 319.1, cy: 248.7 };
// How long the verdict stays up after the run ends.
const RESULT_LINGER_MS = 4000;
// A run whose end never arrived (rosbridge dropped, the server died) must not
// stay on screen: nothing heard for this long clears it.
const IDLE_CLEAR_MS = 60000;
// An event carrying another run's id replaces the shown run only when that
// run is finished or has gone quiet; while it is live, such events are
// stragglers from the run before and are dropped.
const STALE_MS = 2000;
// A page opened mid-run missed the start event: it draws in the head camera's
// native frame until the next event, every one of which repeats the run's
// header, rather than showing nothing until the next run.
const DEFAULT_FRAME = { w: 640, h: 480 };

/** @typedef {{ w: number, h: number }} Size */
/** @typedef {[number, number]} Px */
/** @typedef {[number, number, number, number]} Corners x0, y0, x1, y1 */
/** @typedef {{ left: number, top: number, width: number, height: number }} Rect */
/** @typedef {"main" | "arm"} View the two robot cameras a run draws on */
/** @typedef {"bracket" | "box" | "point" | "reticle" | "vector" | "line"} Kind */
/**
 * One marker, as the skill placed it: geometry in frame px, `locked` greens it.
 * @typedef {{
 *   id: string, kind: Kind, view: View, label: string, locked: boolean,
 *   corners: Corners | null, inner: number | null, px: Px | null, a: Px | null, b: Px | null,
 * }} Marker
 */
/**
 * The stream as laid out on the page: intrinsic size, the video element's
 * content box within the stage, and how object-fit / object-position place the
 * picture in that box (position as the computed "x y" string).
 * @typedef {{ w: number, h: number, box: Rect, fit: string, position: string }} VideoLayout
 */
/**
 * One run of a skill, as the overlay understands it.
 * @typedef {{
 *   id: string,
 *   skill: string,
 *   prompt: string,
 *   stages: string[],
 *   stage: string | null,
 *   frame: Size,
 *   readout: string,
 *   busy: boolean,
 *   progress: number | null,
 *   result: { ok: boolean, text: string } | null,
 *   markers: Map<string, Marker>,
 *   seen: number,
 * }} Run
 */

/** One object-position component -> where the picture's slack goes.
 * @param {string} term "50%" or "12px" @param {number} slack box minus picture */
function positionOffset(term, slack) {
  if (term.endsWith("%")) return (slack * parseFloat(term)) / 100;
  if (term.endsWith("px")) return parseFloat(term);
  return slack / 2;
}

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

/** @param {any} v @returns {Px | null} */
function px(v) {
  return Array.isArray(v) && v.length >= 2 && Number.isFinite(v[0]) && Number.isFinite(v[1]) ? [v[0], v[1]] : null;
}

/** @param {any} v @returns {Corners | null} */
function corners(v) {
  if (!Array.isArray(v) || v.length < 4 || !v.slice(0, 4).every(Number.isFinite)) return null;
  return [Math.min(v[0], v[2]), Math.min(v[1], v[3]), Math.max(v[0], v[2]), Math.max(v[1], v[3])];
}

/** @type {Record<Kind, (ev: any) => boolean>} what each kind needs to be drawable */
const KIND_GEOMETRY = {
  bracket: (ev) => corners(ev.corners) !== null,
  box: (ev) => corners(ev.corners) !== null,
  point: (ev) => px(ev.px) !== null,
  reticle: (ev) => px(ev.px) !== null,
  vector: (ev) => px(ev.a) !== null && px(ev.b) !== null,
  line: (ev) => px(ev.a) !== null && px(ev.b) !== null,
};

/** @param {any} ev a mark event @returns {Marker | null} */
function marker(ev) {
  const kind = /** @type {Kind} */ (ev.kind);
  if (typeof ev.id !== "string" || !ev.id || !(kind in KIND_GEOMETRY) || !KIND_GEOMETRY[kind](ev)) return null;
  const inner = Number.isFinite(ev.inner) && ev.inner > 0 && ev.inner <= 1 ? ev.inner : null;
  return {
    id: ev.id,
    kind,
    view: ev.view === "arm" ? "arm" : "main",
    label: typeof ev.label === "string" ? ev.label : "",
    locked: ev.locked === true,
    corners: corners(ev.corners),
    inner,
    px: px(ev.px),
    a: px(ev.a),
    b: px(ev.b),
  };
}

/** @param {any} v @returns {Size | null} */
function size(v) {
  if (!Array.isArray(v) || !(Number(v[0]) > 0) || !(Number(v[1]) > 0)) return null;
  return { w: Number(v[0]), h: Number(v[1]) };
}

/** @param {any} v @returns {string[]} */
function stageList(v) {
  return Array.isArray(v) ? v.filter((/** @type {unknown} */ s) => typeof s === "string") : [];
}

/** @param {any} ev @returns {string} the run id the event carries, "" when none */
function runId(ev) {
  return typeof ev.run === "string" ? ev.run : "";
}

/** @param {any} ev a run-start event, or any first event of a run missed at its start
 * @param {number} now @returns {Run} */
function startRun(ev, now) {
  return {
    id: runId(ev),
    skill: ev.skill,
    prompt: typeof ev.prompt === "string" ? ev.prompt : "",
    stages: stageList(ev.stages),
    stage: null,
    frame: size(ev.frame) ?? DEFAULT_FRAME,
    readout: "starting",
    busy: false,
    progress: null,
    result: null,
    markers: new Map(),
    seen: now,
  };
}

/**
 * Every event repeats the run's header (prompt, stage list, frame, current
 * stage); a run opened from whichever message came first catches up here.
 * @param {Run} run @param {any} ev
 */
function adoptHeader(run, ev) {
  const stages = stageList(ev.stages);
  if (stages.length > run.stages.length) run.stages = stages;
  if (!run.prompt && typeof ev.prompt === "string") run.prompt = ev.prompt;
  run.frame = size(ev.frame) ?? run.frame;
  const stage = ev.ev === "stage" ? ev.name : ev.stage;
  if (typeof stage !== "string" || !stage || stage === run.stage) return;
  if (!run.stages.includes(stage)) run.stages.push(stage);
  run.stage = stage;
}

/** @param {Run} run @param {any} ev a run-end event */
function endRun(run, ev) {
  run.markers.clear();
  run.busy = false;
  run.progress = null;
  const text = typeof ev.text === "string" && ev.text ? ev.text : "";
  run.result = ev.cancelled
    ? { ok: false, text: "stopped" }
    : ev.ok === true
      ? { ok: true, text: text || "done" }
      : { ok: false, text: `failed · ${text || "no reason given"}` };
  run.readout = run.result.text;
}

/**
 * Fold one overlay event into the run. Returns the run to keep showing, or
 * null when there is none. A run ends with its result set; the caller decides
 * how long that lingers. Events are matched to the run by the id the SDK
 * stamps on them: a finished run's own stragglers never revive it, another
 * run's end never closes it, and another run's first event opens a fresh run
 * over a verdict or a run gone quiet.
 * @param {Run | null} run
 * @param {any} ev
 * @param {number} [now]
 * @returns {Run | null}
 */
export function applyEvent(run, ev, now = Date.now()) {
  if (!ev || typeof ev !== "object" || typeof ev.ev !== "string" || typeof ev.skill !== "string") return run;
  const isEnd = ev.ev === "run" && ev.state === "end";
  if (run && runId(ev) !== run.id) {
    if (isEnd || (!run.result && now - run.seen < STALE_MS)) return run;
    run = null;
  }
  if (ev.ev === "run") {
    if (ev.state === "start") return startRun(ev, now);
    if (isEnd && run && !run.result) endRun(run, ev);
    return run;
  }
  if (run?.result) return run;
  run ??= startRun(ev, now);
  run.seen = now;
  adoptHeader(run, ev);
  switch (ev.ev) {
    case "stage":
      if (typeof ev.name !== "string") break;
      run.readout = ev.name;
      run.busy = false;
      break;
    case "readout":
      if (typeof ev.text !== "string") break;
      run.readout = ev.text;
      run.busy = ev.busy === true;
      run.progress = Number.isFinite(ev.progress) ? Math.min(1, Math.max(0, ev.progress)) : null;
      break;
    case "mark": {
      const m = marker(ev);
      if (m) run.markers.set(m.id, m);
      break;
    }
    case "clear": {
      const ids = Array.isArray(ev.ids) ? ev.ids : [];
      if (ids.length) ids.forEach((/** @type {unknown} */ id) => run?.markers.delete(String(id)));
      else if (ev.view === "main" || ev.view === "arm") {
        for (const [id, m] of run.markers) if (m.view === ev.view) run.markers.delete(id);
      } else run.markers.clear();
      break;
    }
    default:
      break;
  }
  return run;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_OPEN =
  '<svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="currentColor" ' +
  'stroke-width="1.6" aria-hidden="true">';
// Crosshair: where the fingers close.
const RETICLE_SVG =
  '<circle cx="24" cy="24" r="13"/>' +
  '<line x1="24" y1="3" x2="24" y2="14"/><line x1="24" y1="34" x2="24" y2="45"/>' +
  '<line x1="3" y1="24" x2="14" y2="24"/><line x1="34" y1="24" x2="45" y2="24"/>' +
  '<circle cx="24" cy="24" r="2.2" fill="currentColor" stroke="none"/>';
// Small diamond: a point the skill is holding on to.
const POINT_SVG = '<path d="M24 11 37 24 24 37 11 24Z"/><circle cx="24" cy="24" r="1.8" fill="currentColor" stroke="none"/>';
const TAG = '<span class="tgt-tag mono"></span>';

/** @type {Record<"bracket" | "box" | "point" | "reticle", string>} */
const TEMPLATES = {
  bracket: `<i></i><i></i><i></i><i></i>${TAG}`,
  box: `${TAG}<div class="tgt-inner"></div>`,
  point: `${SVG_OPEN}${POINT_SVG}</svg>${TAG}`,
  reticle: `${SVG_OPEN}${RETICLE_SVG}</svg>${TAG}`,
};

/**
 * The run as last heard on the topic, kept for the whole session so a page
 * switch mid-run (Agent to Teleop) draws the run in full at once instead of
 * waiting for events to trickle in. Built on first use; pages drop their own
 * listeners on unmount and nothing destroys the store itself.
 * @returns {{ run: () => Run | null, subscribe: (cb: () => void) => () => void }}
 */
export function sharedTargetingRun() {
  return (_store ??= createRunStore());
}

/** @type {ReturnType<typeof createRunStore> | undefined} */
let _store;

function createRunStore() {
  /** @type {Run | null} */
  let run = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let lingerTimer;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let idleTimer;
  /** @type {Set<() => void>} */
  const listeners = new Set();

  function notify() {
    for (const cb of listeners) cb();
  }

  function clear() {
    clearTimeout(lingerTimer);
    clearTimeout(idleTimer);
    run = null;
    notify();
  }

  /** @param {any} msg std_msgs/String */
  function onOverlay(msg) {
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
    clearTimeout(idleTimer);
    if (run && !run.result) idleTimer = setTimeout(clear, IDLE_CLEAR_MS);
    // Only the transition into a verdict arms the linger: later events of a
    // finished run (or a stray one) must not keep pushing it out.
    if (run?.result && !ended) {
      lingerTimer = setTimeout(() => {
        run = null;
        notify();
      }, RESULT_LINGER_MS);
    }
    notify();
  }

  ros.subscribe(SKILL_OVERLAY_TOPIC, onOverlay, undefined, "std_msgs/msg/String");
  // The end of a run published while the socket was down is gone for good.
  ros.onStateChange((state) => {
    if (state !== "connected" && run) clear();
  });
  return {
    run: () => run,
    /** @param {() => void} cb */
    subscribe(cb) {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
  };
}

/**
 * @param {HTMLElement} stage the .video-stage the markers pin to
 * @param {HTMLVideoElement | null} video the stage's video on hardware; null in
 *   the sim, where a canvas renders the head camera at the stage's size
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ destroy: () => void }}
 */
export function createTargetingOverlay(stage, video, session) {
  const store = sharedTargetingRun();
  const clip = document.createElement("div");
  clip.className = "tgt-clip";
  clip.hidden = true;
  // The skill's frame, placed over the picture; every marker is a percentage
  // of it, so a resize only moves this one element.
  const layer = document.createElement("div");
  layer.className = "tgt-layer";
  layer.innerHTML = '<div class="tgt-scan"></div><svg class="tgt-lines" preserveAspectRatio="none" aria-hidden="true"></svg>';
  clip.appendChild(layer);
  const lines = /** @type {SVGSVGElement} */ (/** @type {unknown} */ (layer.querySelector(".tgt-lines")));

  const hud = document.createElement("div");
  hud.className = "overlay tgt-hud";
  hud.hidden = true;
  hud.innerHTML =
    '<div class="tgt-hud-head"><span class="microlabel tgt-skill"></span><span class="tgt-prompt mono"></span></div>' +
    '<ol class="tgt-stages"></ol>' +
    '<div class="tgt-readout mono"><span class="tgt-readout-text"></span><span class="tgt-bar" hidden><span class="tgt-bar-fill"></span></span></div>';
  stage.append(clip, hud);

  const skillEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-skill"));
  const promptEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-prompt"));
  const stagesEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-stages"));
  const readoutEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-readout-text"));
  const barEl = /** @type {HTMLElement} */ (hud.querySelector(".tgt-bar"));
  const barFill = /** @type {HTMLElement} */ (hud.querySelector(".tgt-bar-fill"));

  /** The DOM for each marker on screen, by id; a marker keeps its element
   * between updates so a point glides instead of stepping.
   * @type {Map<string, HTMLElement | SVGLineElement>} */
  const els = new Map();

  /** The robot camera on the stage, or null when another view (map, orbit) is up.
   * @returns {View | null} */
  function view() {
    const name = primaryCameraName(session);
    return name === "main" || name === "arm" ? name : null;
  }

  /** @param {Marker} m @returns {HTMLElement | SVGLineElement} */
  function elementFor(m) {
    let el = els.get(m.id);
    if (el && el.dataset.kind !== m.kind) {
      el.remove();
      el = undefined;
    }
    if (el) return el;
    if (m.kind === "vector" || m.kind === "line") {
      el = document.createElementNS(SVG_NS, "line");
      lines.appendChild(el);
    } else {
      el = document.createElement("div");
      el.innerHTML = TEMPLATES[m.kind];
      layer.appendChild(el);
    }
    el.setAttribute("class", `tgt-${m.kind}`);
    el.dataset.kind = m.kind;
    els.set(m.id, el);
    return el;
  }

  /** @param {Marker} m @param {Size} frame */
  function draw(m, frame) {
    const el = elementFor(m);
    el.classList.toggle("locked", m.locked);
    if (el instanceof SVGLineElement) {
      const [a, b] = [/** @type {Px} */ (m.a), /** @type {Px} */ (m.b)];
      el.setAttribute("x1", String(a[0]));
      el.setAttribute("y1", String(a[1]));
      el.setAttribute("x2", String(b[0]));
      el.setAttribute("y2", String(b[1]));
      return;
    }
    const tag = /** @type {HTMLElement} */ (el.querySelector(".tgt-tag"));
    tag.textContent = m.label;
    tag.hidden = !m.label;
    if (m.corners) {
      const [x0, y0, x1, y1] = m.corners;
      el.style.left = `${(x0 / frame.w) * 100}%`;
      el.style.top = `${(y0 / frame.h) * 100}%`;
      el.style.width = `${((x1 - x0) / frame.w) * 100}%`;
      el.style.height = `${((y1 - y0) / frame.h) * 100}%`;
    } else if (m.px) {
      el.style.left = `${(m.px[0] / frame.w) * 100}%`;
      el.style.top = `${(m.px[1] / frame.h) * 100}%`;
    }
    const inner = /** @type {HTMLElement | null} */ (el.querySelector(".tgt-inner"));
    if (inner) {
      inner.hidden = m.inner === null;
      if (m.inner !== null) inner.style.width = inner.style.height = `${m.inner * 100}%`;
    }
  }

  /** @param {Set<string>} keep marker ids drawn this pass */
  function prune(keep) {
    for (const [id, el] of els) {
      if (keep.has(id)) continue;
      el.remove();
      els.delete(id);
    }
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
    const run = store.run();
    if (!run) return;
    const rect = frameRect(run.frame, videoLayout(), stage.clientWidth, stage.clientHeight, view() ?? "main");
    layer.hidden = !rect;
    if (!rect) return;
    layer.style.left = `${rect.left}px`;
    layer.style.top = `${rect.top}px`;
    layer.style.width = `${rect.width}px`;
    layer.style.height = `${rect.height}px`;
  }

  /** @param {Run} run */
  function renderStages(run) {
    const doneUpTo = run.stage ? run.stages.indexOf(run.stage) : -1;
    stagesEl.replaceChildren(
      ...run.stages.map((name, i) => {
        const li = document.createElement("li");
        li.className = "tgt-stage";
        li.textContent = name;
        const done = run.result?.ok ? true : i < doneUpTo;
        li.classList.toggle("done", done);
        li.classList.toggle("active", !run.result && i === doneUpTo);
        return li;
      }),
    );
  }

  function render() {
    const run = store.run();
    const cam = view();
    const show = !!run && cam !== null;
    clip.hidden = !show || !!run?.result;
    hud.hidden = !show;
    if (!run || !cam) return;
    placeLayer();

    skillEl.textContent = run.skill.replace(/_/g, " ");
    promptEl.textContent = run.prompt ? `“${run.prompt}”` : "";
    renderStages(run);
    readoutEl.textContent = run.readout;
    hud.classList.toggle("ok", !!run.result?.ok);
    hud.classList.toggle("failed", !!run.result && !run.result.ok);
    hud.classList.toggle("busy", run.busy);
    barEl.hidden = run.progress === null;
    if (run.progress !== null) barFill.style.width = `${run.progress * 100}%`;
    if (run.result) {
      prune(new Set());
      return;
    }

    layer.classList.toggle("busy", run.busy);
    lines.setAttribute("viewBox", `0 0 ${run.frame.w} ${run.frame.h}`);
    const drawn = new Set();
    for (const m of run.markers.values()) {
      if (m.view !== cam) continue;
      draw(m, run.frame);
      drawn.add(m.id);
    }
    prune(drawn);
  }

  const unsubStore = store.subscribe(render);
  const resize = new ResizeObserver(placeLayer);
  resize.observe(stage);
  if (video) resize.observe(video); // its box moves with media queries the stage's size does not
  video?.addEventListener("resize", placeLayer);
  const unsubSession = session.onChange(render);
  render(); // a run already under way draws at once

  return {
    destroy() {
      unsubStore();
      unsubSession();
      resize.disconnect();
      video?.removeEventListener("resize", placeLayer);
      clip.remove();
      hud.remove();
    },
  };
}
