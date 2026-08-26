// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Head tilt — thin vertical slider on the right edge, hairline tick at 0°.
// Drag publishes std_msgs/Int32 to /mars/head/set_position at ~10 Hz.
// Feedback from /mars/head/current_position (JSON-in-String) drives the
// thumb whenever the operator isn't dragging (after a short post-release
// hold, so lagging reports don't yank the gesture back) — a skill moving
// the head keeps the slider honest — and a marker shows where the head
// really is mid-drag. The default ±range is only a fallback: the feedback payload
// carries the robot's real min/max angles (e.g. ±25° on MARS) and the
// slider adopts them on first contact.

import {
  HEAD_SET_POSITION_TOPIC,
  HEAD_CURRENT_POSITION_TOPIC,
  HEAD_MIN_DEG,
  HEAD_MAX_DEG,
} from "../constants.js";

const PUBLISH_GAP_MS = 100;
const FEEDBACK_GRACE_MS = 500;

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ el: HTMLElement, destroy: () => void }}
 */
export function createHeadTilt(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "head-tilt";

  let minDeg = HEAD_MIN_DEG;
  let maxDeg = HEAD_MAX_DEG;

  const topLabel = document.createElement("span");
  topLabel.className = "ht-label mono";
  topLabel.textContent = `+${maxDeg}`;

  const track = document.createElement("div");
  track.className = "ht-track";
  track.title = `drag to tilt the head — ${HEAD_SET_POSITION_TOPIC}`;

  const rail = document.createElement("div");
  rail.className = "ht-rail";

  const zeroTick = document.createElement("div");
  zeroTick.className = "ht-zero";
  zeroTick.style.top = `${fractionFor(0) * 100}%`;

  const actual = document.createElement("div");
  actual.className = "ht-actual";
  actual.title = `where the head actually is — ${HEAD_CURRENT_POSITION_TOPIC}`;
  actual.hidden = true;

  const thumb = document.createElement("div");
  thumb.className = "ht-thumb";

  track.append(rail, zeroTick, actual, thumb);

  const bottomLabel = document.createElement("span");
  bottomLabel.className = "ht-label mono";
  bottomLabel.textContent = String(minDeg);

  const readout = document.createElement("span");
  readout.className = "ht-readout mono";
  readout.textContent = "–";

  wrap.append(topLabel, track, bottomLabel, readout);
  parent.appendChild(wrap);

  /** Top-of-track fraction for a given angle (max at top). @param {number} deg */
  function fractionFor(deg) {
    return 1 - (deg - minDeg) / (maxDeg - minDeg);
  }

  /**
   * Adopt the robot's reported range and re-place everything on the track.
   * @param {number} min @param {number} max
   */
  function setRange(min, max) {
    if (min === minDeg && max === maxDeg) return;
    minDeg = min;
    maxDeg = max;
    topLabel.textContent = `+${Math.round(maxDeg)}`;
    bottomLabel.textContent = String(Math.round(minDeg));
    zeroTick.style.top = `${fractionFor(0) * 100}%`;
    if (shownDeg !== null) place(thumb, shownDeg);
  }

  /** @param {HTMLElement} el @param {number} deg */
  function place(el, deg) {
    el.style.top = `${fractionFor(deg) * 100}%`;
  }

  let dragging = false;
  let lastPublishAt = 0;
  // Feedback lags a release (transport + head travel), so the first post-drag
  // report would yank the thumb back before it chases; hold the gesture briefly.
  let feedbackHoldUntil = 0;
  /** @type {number | null} */
  let shownDeg = null;

  /** @param {number} deg */
  function show(deg) {
    shownDeg = deg;
    place(thumb, deg);
    readout.textContent = `${Math.round(deg)}°`;
  }

  /** @param {PointerEvent} e @returns {number} */
  function degreesFrom(e) {
    const rect = track.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    return Math.round(minDeg + (1 - frac) * (maxDeg - minDeg));
  }

  /** @param {number} deg @param {boolean} force */
  function command(deg, force) {
    show(deg);
    const now = performance.now();
    if (force || now - lastPublishAt >= PUBLISH_GAP_MS) {
      lastPublishAt = now;
      rosClient.publish(HEAD_SET_POSITION_TOPIC, { data: deg });
    }
  }

  /** @param {PointerEvent} e */
  function onPointerDown(e) {
    dragging = true;
    try {
      track.setPointerCapture(e.pointerId);
    } catch {
      // Capture can fail for synthetic/stale pointers; degrade gracefully.
    }
    wrap.classList.add("engaged");
    command(degreesFrom(e), false);
  }

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (dragging) command(degreesFrom(e), false);
  }

  /** @param {PointerEvent} e */
  function onPointerUp(e) {
    if (!dragging) return;
    dragging = false;
    wrap.classList.remove("engaged");
    feedbackHoldUntil = performance.now() + FEEDBACK_GRACE_MS;
    command(degreesFrom(e), true); // final value always goes out
  }

  track.addEventListener("pointerdown", onPointerDown);
  track.addEventListener("pointermove", onPointerMove);
  track.addEventListener("pointerup", onPointerUp);
  track.addEventListener("pointercancel", onPointerUp);

  const unsub = rosClient.subscribe(HEAD_CURRENT_POSITION_TOPIC, (payload) => {
    if (typeof payload?.data !== "string") return;
    /** @type {HeadPosition} */
    let parsed;
    try {
      parsed = JSON.parse(payload.data);
    } catch {
      return;
    }
    if (typeof parsed.current_position !== "number") return;
    if (typeof parsed.min_angle === "number" && typeof parsed.max_angle === "number" && parsed.max_angle > parsed.min_angle) {
      setRange(parsed.min_angle, parsed.max_angle);
    }
    const deg = Math.max(minDeg, Math.min(maxDeg, parsed.current_position));
    actual.hidden = false;
    place(actual, deg);
    if (!dragging && performance.now() >= feedbackHoldUntil) show(deg);
  }, undefined, "std_msgs/msg/String");

  return {
    // Exposed so the Collect page can hide head control during learned-policy
    // recording (camera viewpoint must stay fixed); Teleop ignores it.
    el: wrap,
    destroy() {
      unsub();
      wrap.remove();
    },
  };
}
