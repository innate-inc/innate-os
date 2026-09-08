// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Draws the people the robot recognizes over the live camera: one box per
// tracked person, tagged `P3 · Theo`, teal when the identity is settled, amber
// while it is tentative, red on a contradiction (docs/rfc/people-memory.md §7).
//
// The boxes come off /brain/people in Gemini's per-mille convention
// [ymin, xmin, ymax, xmax] of the published camera frame — the same rectangle
// the agent reasons about — so the only work here is mapping that frame onto
// the letterboxed rectangle `object-fit: contain` leaves the video in
// (trajectoryOverlay.js does the same for the planned route).
//
// The overlay never asserts more than it knows: a snapshot describes one frame,
// so once one stops arriving the boxes go away rather than following whatever
// the camera moved on to.

import { PEOPLE_SNAPSHOT_FRESH_MS, PEOPLE_THROTTLE_MS, PEOPLE_TOPIC } from "../constants.js";

/** Box coordinates are thousandths of the frame, not pixels or fractions. */
const PER_MILLE = 1000;

/** Teal once the identity is settled; amber for everything tentative; red for a
 * contradiction the engine could not resolve. Unknown states read as tentative,
 * which is the safe direction: a box the operator over-trusts is the bad one. */
export const STATE_COLORS = {
  known: "#3fd8c0",
  familiar: "#e8a33d",
  possible: "#e8a33d",
  unknown: "#e8a33d",
  conflict: "#e95656",
};
const TENTATIVE_COLOR = STATE_COLORS.unknown;

/** Chip and outline geometry, in CSS pixels of the stage. */
const BOX_WIDTH = 2;
const CHIP_HEIGHT = 19;
const CHIP_PAD_X = 7;
const CHIP_GAP = 4;
const CHIP_RADIUS = 5;
const LABEL_FONT = '600 12px system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
// The feed behind a box is whatever the robot is looking at, from a dark room to
// a white wall, so both marks carry their own dark base and let the state color
// live in the stroke and the text (the --ctl-glass rule in css/app.css).
const CHIP_FILL = "rgb(8 8 10 / 78%)";
const BOX_SHADOW = "rgb(0 0 0 / 45%)";

/** @typedef {{ tag: string, name: string | null, state: string, bbox: number[] }} DrawablePerson */
/** @typedef {{ stamp: number, people: DrawablePerson[] }} PeopleSnapshot */
/** @typedef {{ x: number, y: number, w: number, h: number }} Rect */

/**
 * Parse a /brain/people message into the little the overlay draws. Null when
 * the payload is unusable — the overlay then keeps its last good snapshot until
 * that one goes stale, exactly as if nothing had arrived.
 * @param {any} payload std_msgs/String carrying a PeopleSnapshotDict
 * @returns {PeopleSnapshot | null}
 */
export function parseSnapshot(payload) {
  /** @type {any} */
  let data;
  try {
    data = JSON.parse(payload?.data ?? "");
  } catch {
    return null;
  }
  if (!Array.isArray(data?.people)) return null;
  /** @type {DrawablePerson[]} */
  const people = [];
  for (const person of data.people) {
    const bbox = validBox(person?.bbox);
    // A lost track's box is the tracker's guess at where someone WOULD be, kept
    // so the person is re-associated on return. Drawing it would put a name on
    // an empty patch of floor.
    if (!bbox || person?.lost === true) continue;
    people.push({
      tag: typeof person.tag === "string" ? person.tag : "",
      name: typeof person.name === "string" && person.name ? person.name : null,
      state: typeof person.state === "string" ? person.state : "unknown",
      bbox,
    });
  }
  return { stamp: typeof data.stamp === "number" ? data.stamp : 0, people };
}

/**
 * A drawable per-mille box: four numbers, clamped to the frame, with both
 * extents the right way round and non-empty. Null otherwise.
 * @param {any} bbox
 * @returns {number[] | null}
 */
function validBox(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return null;
  const clamped = [];
  for (const v of bbox) {
    if (typeof v !== "number" || !Number.isFinite(v)) return null;
    clamped.push(Math.min(PER_MILLE, Math.max(0, v)));
  }
  if (clamped[2] <= clamped[0] || clamped[3] <= clamped[1]) return null;
  return clamped;
}

/**
 * The stroke and text color for an identity state.
 * @param {string} state
 * @returns {string}
 */
export function stateColor(state) {
  return /** @type {Record<string, string>} */ (STATE_COLORS)[state] ?? TENTATIVE_COLOR;
}

/**
 * What the frame occupies inside the stage under `object-fit: contain` — the
 * rectangle the per-mille boxes are relative to. A frame size of 0 (the sim's
 * canvas, which has no stream of its own and simply fills the stage) means the
 * whole stage is the frame.
 * @param {number} vw @param {number} vh source frame size in pixels
 * @param {number} cw @param {number} ch stage size in CSS pixels
 * @returns {Rect}
 */
export function containRect(vw, vh, cw, ch) {
  if (!vw || !vh) return { x: 0, y: 0, w: cw, h: ch };
  const fit = Math.min(cw / vw, ch / vh);
  const w = vw * fit;
  const h = vh * fit;
  return { x: (cw - w) / 2, y: (ch - h) / 2, w, h };
}

/**
 * A per-mille [ymin, xmin, ymax, xmax] box as a pixel rect inside `rect`.
 * @param {number[]} bbox @param {Rect} rect
 * @returns {Rect}
 */
export function boxRect(bbox, rect) {
  const [ymin, xmin, ymax, xmax] = bbox;
  return {
    x: rect.x + (xmin / PER_MILLE) * rect.w,
    y: rect.y + (ymin / PER_MILLE) * rect.h,
    w: ((xmax - xmin) / PER_MILLE) * rect.w,
    h: ((ymax - ymin) / PER_MILLE) * rect.h,
  };
}

/**
 * The chip over a box: the tag the agent uses, plus the name once there is one.
 * @param {{ tag: string, name: string | null }} person
 * @returns {string}
 */
export function tagLabel(person) {
  if (!person.tag) return person.name ?? "";
  return person.name ? `${person.tag} · ${person.name}` : person.tag;
}

/**
 * Whether a snapshot that arrived at `receivedAt` still describes what the
 * camera is showing. Measured on the browser's own clock: the stamps are the
 * robot's, and a robot without NTP can sit hours off a browser (see
 * map/memories.js headerSkew).
 * @param {number} receivedAt performance.now() when the snapshot arrived
 * @param {number} now performance.now()
 * @returns {boolean}
 */
export function isFresh(receivedAt, now) {
  return now - receivedAt < PEOPLE_SNAPSHOT_FRESH_MS;
}

/**
 * @param {HTMLElement} stage the .video-stage the video (or sim canvas) fills
 * @param {HTMLVideoElement | null} video the stage's video element on hardware;
 *   null in sim, where a Three.js canvas renders the head camera at stage size
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ destroy: () => void }}
 */
export function createPeopleOverlay(stage, video, ros) {
  const canvas = document.createElement("canvas");
  canvas.className = "people-canvas";
  canvas.hidden = true;
  stage.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  /** @type {PeopleSnapshot | null} */
  let snapshot = null;
  let receivedAt = 0;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let staleTimer;
  let raf = 0;

  function schedule() {
    if (!raf) raf = requestAnimationFrame(draw);
  }

  /** @param {any} payload */
  function onSnapshot(payload) {
    const next = parseSnapshot(payload);
    if (!next) return;
    // rws replays a latched topic on every (re)subscribe, so an older snapshot
    // can arrive after a newer one. The newest frame wins.
    if (snapshot && next.stamp && next.stamp < snapshot.stamp) return;
    snapshot = next;
    receivedAt = performance.now();
    clearTimeout(staleTimer);
    // Nothing else would wake the overlay to take the boxes down once the
    // publisher stops.
    staleTimer = setTimeout(schedule, PEOPLE_SNAPSHOT_FRESH_MS);
    schedule();
  }

  function draw() {
    raf = 0;
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // Unhidden below only if there is something to paint.
    canvas.hidden = true;

    const people = snapshot && isFresh(receivedAt, performance.now()) ? snapshot.people : [];
    if (!people.length) return;

    const cw = stage.clientWidth;
    const ch = stage.clientHeight;
    if (!cw || !ch) return;
    const vw = video ? video.videoWidth : 0;
    const vh = video ? video.videoHeight : 0;
    // A video element with no frame size yet has nothing on screen to annotate.
    if (video && (!vw || !vh)) return;
    const rect = containRect(vw, vh, cw, ch);
    canvas.hidden = false;

    // Derive DPR from the backing store so a monitor move cannot desync it.
    const dpr = canvas.width / cw;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = LABEL_FONT;
    ctx.textBaseline = "middle";

    for (const person of people) {
      const color = stateColor(person.state);
      const box = boxRect(person.bbox, rect);

      ctx.lineJoin = "round";
      ctx.strokeStyle = BOX_SHADOW;
      ctx.lineWidth = BOX_WIDTH + 2;
      ctx.strokeRect(box.x, box.y, box.w, box.h);
      ctx.strokeStyle = color;
      ctx.lineWidth = BOX_WIDTH;
      ctx.strokeRect(box.x, box.y, box.w, box.h);

      const label = tagLabel(person);
      if (!label) continue;
      const chipW = ctx.measureText(label).width + CHIP_PAD_X * 2;
      // Above the box, unless that would leave the frame — then just inside it.
      const above = box.y - CHIP_GAP - CHIP_HEIGHT;
      const chipY = above >= rect.y ? above : Math.min(box.y + CHIP_GAP, rect.y + rect.h - CHIP_HEIGHT);
      const chipX = Math.max(rect.x, Math.min(box.x, rect.x + rect.w - chipW));
      ctx.fillStyle = CHIP_FILL;
      ctx.beginPath();
      // roundRect landed well after the rest of what this app assumes (Safari
      // 16.4), and square corners are a fine chip on a browser without it.
      if (typeof ctx.roundRect === "function") ctx.roundRect(chipX, chipY, chipW, CHIP_HEIGHT, CHIP_RADIUS);
      else ctx.rect(chipX, chipY, chipW, CHIP_HEIGHT);
      ctx.fill();
      ctx.fillStyle = color;
      ctx.fillText(label, chipX + CHIP_PAD_X, chipY + CHIP_HEIGHT / 2);
    }
  }

  const resize = new ResizeObserver(() => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(stage.clientWidth * dpr);
    canvas.height = Math.round(stage.clientHeight * dpr);
    schedule();
  });
  resize.observe(stage);
  video?.addEventListener("resize", schedule);

  const unsubscribe = ros.subscribe(PEOPLE_TOPIC, onSnapshot, PEOPLE_THROTTLE_MS, "std_msgs/msg/String");

  return {
    destroy() {
      unsubscribe();
      resize.disconnect();
      video?.removeEventListener("resize", schedule);
      clearTimeout(staleTimer);
      cancelAnimationFrame(raf);
      canvas.remove();
    },
  };
}
