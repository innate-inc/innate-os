// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Camera Calibration page — drives the mars_cam stereo_calibrator's interactive
// ChArUco stereo calibration over the RunStereoCalibration action. Start opens
// the goal and keeps it running while the operator moves the board in view of
// the live feed and clicks Capture (one enter_events publish per click); live
// feedback after each capture shows progress + whether the board was seen, plus
// the two coverage-dot debug images. The goal resolves (RMS errors) once enough
// images are captured, Stop cancels it, or the server's capture watchdog times
// out. Only MODE_MANUAL exists today, so the goal always sends mode: 0.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { acquireVideoSession, releaseVideoSession } from "../sharedVideoSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import {
  RUN_STEREO_CALIBRATION_ACTION,
  RUN_STEREO_CALIBRATION_ACTION_TYPE,
  STEREO_CALIB_CAPTURE_TOPIC,
  STEREO_CALIB_DEFAULT_NUM_IMAGES,
  STEREO_CALIB_DEFAULT_MIN_CORNERS,
  MAIN_CAMERA_DEPTH_TOPIC,
} from "../constants.js";

// How long to wait for a depth frame before concluding "no calibration file
// detected yet" (a late arrival afterward still flips the indicator — see
// the MAIN_CAMERA_DEPTH_TOPIC subscription below).
const CALIB_FILE_CHECK_TIMEOUT_MS = 7000;

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "calib", buildView);
}

/**
 * @typedef {Object} FeedbackState
 * @property {number} imagesCaptured
 * @property {number} target
 * @property {number} captureAttempts
 * @property {boolean | null} cornersFound
 * @property {string} message
 * @property {number | null} deadlineMs Wall-clock deadline (Date.now()-comparable)
 *   for the server's capture-timeout watchdog, re-anchored from
 *   `capture_timeout_sec` on every feedback tick. Display-only — see render().
 *
 * @typedef {Object} ResultState
 * @property {boolean} success
 * @property {string} message
 * @property {boolean} timedOut
 * @property {number} imagesCaptured
 * @property {number} leftRms
 * @property {number} rightRms
 * @property {number} stereoRms
 * @property {string} quality Server's RMS-threshold label (EXCELLENT/GOOD/ACCEPTABLE/
 *   POOR), empty if not computed (e.g. cancelled before finishing).
 */

const CALIBRATION_BOARD_PDF_URL =
  "https://raw.githubusercontent.com/innate-inc/web-docs/main/.gitbook/assets/mars-calibration-board.pdf";

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  const session = acquireVideoSession();
  // No camera switcher here; the shared session may arrive on another page's view.
  session.showMainCamera();

  // ---- header ---------------------------------------------------------
  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.className = "page-title";
  title.textContent = "Camera Calibration";
  const fileStatus = document.createElement("span");
  fileStatus.className = "calib-file-status microlabel";
  fileStatus.textContent = "Calibration file: checking…";
  fileStatus.title = `Inferred from ${MAIN_CAMERA_DEPTH_TOPIC} — depth frames only flow once a valid calibration is loaded`;
  head.append(title, fileStatus);

  // ---- grid: live feed | controls & feedback ---------------------------
  const grid = document.createElement("div");
  grid.className = "calib-grid";
  const videoWrap = document.createElement("div");
  videoWrap.className = "calib-video";
  const side = document.createElement("aside");
  side.className = "calib-side";
  grid.append(videoWrap, side);
  root.append(head, grid);

  const videoStage = createVideoStage(videoWrap, session);
  session.start();

  // ---- controls -----------------------------------------------------------
  const controls = document.createElement("div");
  controls.className = "calib-panel";

  const numField = fieldRow("Images to capture", String(STEREO_CALIB_DEFAULT_NUM_IMAGES));
  const minField = fieldRow("Min corners per capture", String(STEREO_CALIB_DEFAULT_MIN_CORNERS));

  const saveRow = document.createElement("label");
  saveRow.className = "calib-checkbox-row";
  const saveCheckbox = document.createElement("input");
  saveCheckbox.type = "checkbox";
  saveCheckbox.checked = true;
  const saveText = document.createElement("span");
  saveText.textContent =
    "Save calibration when done (backs up the existing calibration file, then writes the new one)";
  saveRow.append(saveCheckbox, saveText);

  const boardLink = document.createElement("a");
  boardLink.className = "calib-board-link";
  boardLink.href = CALIBRATION_BOARD_PDF_URL;
  boardLink.target = "_blank";
  boardLink.rel = "noopener";
  const boardIcon = document.createElement("span");
  boardIcon.className = "calib-board-icon";
  boardIcon.innerHTML =
    '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M12 4v16M4 12h16"/>' +
    '<path d="M5 5h7v7H5zM12 12h7v7h-7z" fill="currentColor" opacity=".3" stroke="none"/></svg>';
  const boardText = document.createElement("span");
  boardText.className = "calib-board-text";
  const boardTitle = document.createElement("span");
  boardTitle.className = "calib-board-title";
  boardTitle.textContent = "Calibration board (PDF)";
  const boardSub = document.createElement("span");
  boardSub.className = "calib-board-sub";
  boardSub.textContent = "ChArUco board — print at 100% scale and keep it flat";
  boardText.append(boardTitle, boardSub);
  const boardDl = document.createElement("span");
  boardDl.className = "calib-board-dl";
  boardDl.innerHTML =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 4v10m-4-3.5 4 4 4-4M5 18.5h14"/></svg>';
  boardLink.append(boardIcon, boardText, boardDl);

  // One row, morphing with the run state: Start while idle, Capture + Stop while active.
  const actionRow = document.createElement("div");
  actionRow.className = "calib-action-row";
  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "calib-btn calib-btn-primary";
  startBtn.textContent = "Start Calibration";
  startBtn.title = RUN_STEREO_CALIBRATION_ACTION;
  const captureBtn = document.createElement("button");
  captureBtn.type = "button";
  captureBtn.className = "calib-btn";
  captureBtn.textContent = "Capture";
  captureBtn.title = STEREO_CALIB_CAPTURE_TOPIC;
  const stopBtn = document.createElement("button");
  stopBtn.type = "button";
  stopBtn.className = "calib-btn calib-btn-stop";
  stopBtn.textContent = "Stop";
  stopBtn.title = "Cancel the calibration run";
  actionRow.append(startBtn, captureBtn, stopBtn);

  const statusLine = document.createElement("p");
  statusLine.className = "calib-status microlabel";

  controls.append(boardLink, numField.row, minField.row, saveRow, actionRow, statusLine);

  // ---- live feedback --------------------------------------------------------
  const feedback = document.createElement("div");
  feedback.className = "calib-panel calib-feedback";

  const progressRow = statRow("Captured");
  const attemptsRow = statRow("Capture attempts");
  const countdownRow = statRow("Capture window");

  const boardBadge = document.createElement("span");
  boardBadge.className = "calib-badge";

  const feedbackMessage = document.createElement("p");
  feedbackMessage.className = "calib-feedback-message";

  const coverageRow = document.createElement("div");
  coverageRow.className = "calib-coverage-row";
  const leftCoverage = coverageTile("Left");
  const rightCoverage = coverageTile("Right");
  coverageRow.append(leftCoverage.tile, rightCoverage.tile);

  feedback.append(progressRow.row, attemptsRow.row, countdownRow.row, boardBadge, feedbackMessage, coverageRow);

  // ---- result ---------------------------------------------------------------
  const result = document.createElement("div");
  result.className = "calib-panel calib-result";

  side.append(controls, feedback, result);

  // ---- state ------------------------------------------------------------
  /** @type {{ cancel: () => void, canceling: boolean } | null} */
  let activeRun = null;
  /** @type {FeedbackState | null} */
  let fb = null;
  /** @type {(ResultState & { rejected?: boolean }) | null} */
  let lastResult = null;
  // "Does the robot have a calibration file loaded?" — no dedicated service for
  // this exists, so it's inferred from whether depth frames are flowing: the
  // depth estimator only publishes once a valid calibration is loaded (it logs
  // "Camera is uncalibrated" and stops otherwise). null = still checking.
  /** @type {boolean | null} */
  let calibFileDetected = null;

  /** @param {string} label @param {string} defaultValue */
  function fieldRow(label, defaultValue) {
    const row = document.createElement("label");
    row.className = "calib-field-row";
    const l = document.createElement("span");
    l.className = "calib-field-label";
    l.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.className = "calib-input mono";
    input.value = defaultValue;
    row.append(l, input);
    return { row, input };
  }

  /** @param {string} label */
  function statRow(label) {
    const row = document.createElement("div");
    row.className = "calib-stat-row";
    const l = document.createElement("span");
    l.className = "microlabel";
    l.textContent = label;
    const value = document.createElement("span");
    value.className = "calib-stat-value mono";
    row.append(l, value);
    return { row, value };
  }

  /** @param {string} label */
  function coverageTile(label) {
    const tile = document.createElement("div");
    tile.className = "calib-coverage-tile";
    const cap = document.createElement("span");
    cap.className = "microlabel calib-coverage-label";
    cap.textContent = label;
    const img = document.createElement("img");
    img.alt = `${label} coverage`;
    img.hidden = true;
    const empty = document.createElement("p");
    empty.className = "calib-coverage-empty microlabel";
    empty.textContent = "no capture yet";
    tile.append(cap, img, empty);
    return { tile, img, empty };
  }

  /**
   * Decode a sensor_msgs/CompressedImage-like feedback entry into an <img> src.
   * The rosbridge-compatible server's wire format for a uint8[] field wasn't
   * confirmed up front — standard rosbridge convention (and this codebase's
   * ttsAudio.js) base64-encodes byte arrays as a string, but handle a raw
   * array of byte values too in case this path differs.
   * @param {any} img
   * @returns {string | null}
   */
  function imageDataUrl(img) {
    if (!img) return null;
    const format = typeof img.format === "string" && img.format ? img.format.split(";")[0].trim() : "jpeg";
    const mime = `image/${format || "jpeg"}`;
    const data = img.data;
    if (typeof data === "string" && data) return `data:${mime};base64,${data}`;
    if (Array.isArray(data) && data.length) {
      const bytes = Uint8Array.from(data);
      return URL.createObjectURL(new Blob([/** @type {BlobPart} */ (bytes)], { type: mime }));
    }
    return null;
  }

  /** @param {{ tile: HTMLElement, img: HTMLImageElement, empty: HTMLElement }} coverage @param {string} url */
  function setCoverageImage(coverage, url) {
    const prevBlob = coverage.img.dataset.blobUrl;
    if (prevBlob) URL.revokeObjectURL(prevBlob);
    if (url.startsWith("blob:")) coverage.img.dataset.blobUrl = url;
    else delete coverage.img.dataset.blobUrl;
    coverage.img.src = url;
    coverage.img.hidden = false;
    coverage.empty.hidden = true;
  }

  /** @param {any} values action_feedback payload */
  function applyCoverageImages(values) {
    /** @type {string[]} */
    const names = Array.isArray(values?.image_names) ? values.image_names : [];
    /** @type {any[]} */
    const images = Array.isArray(values?.images) ? values.images : [];
    names.forEach((name, i) => {
      const url = imageDataUrl(images[i]);
      if (!url) return;
      if (name === "left_coverage") setCoverageImage(leftCoverage, url);
      else if (name === "right_coverage") setCoverageImage(rightCoverage, url);
    });
  }

  /** @param {any} values */
  function onFeedback(values) {
    if (!fb) return;
    if (typeof values?.images_captured === "number") fb.imagesCaptured = values.images_captured;
    if (typeof values?.capture_attempts === "number") fb.captureAttempts = values.capture_attempts;
    if (typeof values?.corners_found === "boolean") fb.cornersFound = values.corners_found;
    if (typeof values?.message === "string") fb.message = values.message;
    // Re-anchor to the server's live watchdog value on every tick — never let
    // this drift into an independent client-side clock. If a tick omits the
    // field (e.g. an older/stale server build), leave the prior deadline alone
    // rather than guess; render() already hides the row when it's still null.
    if (typeof values?.capture_timeout_sec === "number" && values.capture_timeout_sec > 0) {
      fb.deadlineMs = Date.now() + values.capture_timeout_sec * 1000;
    }
    applyCoverageImages(values);
    render();
  }

  /** @param {string} value @param {number} fallback */
  function parsePositiveInt(value, fallback) {
    const n = parseInt(value, 10);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  }

  function startCalibration() {
    if (activeRun || ros.state !== "connected") return;
    const numImages = parsePositiveInt(numField.input.value, STEREO_CALIB_DEFAULT_NUM_IMAGES);
    const minCorners = parsePositiveInt(minField.input.value, STEREO_CALIB_DEFAULT_MIN_CORNERS);
    const saveCalibration = saveCheckbox.checked;

    lastResult = null;
    fb = { imagesCaptured: 0, target: numImages, captureAttempts: 0, cornersFound: null, message: "", deadlineMs: null };

    const { promise, cancel } = ros.sendActionGoal(
      RUN_STEREO_CALIBRATION_ACTION,
      RUN_STEREO_CALIBRATION_ACTION_TYPE,
      { mode: 0, num_images: numImages, min_corners: minCorners, save_calibration: saveCalibration },
      { onFeedback },
    );
    activeRun = { cancel, canceling: false };
    render();

    promise.then(
      (values) => {
        activeRun = null;
        lastResult = {
          success: values?.success !== false,
          message: typeof values?.message === "string" ? values.message : "",
          timedOut: values?.timed_out === true,
          imagesCaptured: typeof values?.images_captured === "number" ? values.images_captured : fb?.imagesCaptured ?? 0,
          leftRms: typeof values?.left_rms === "number" ? values.left_rms : 0,
          rightRms: typeof values?.right_rms === "number" ? values.right_rms : 0,
          stereoRms: typeof values?.stereo_rms === "number" ? values.stereo_rms : 0,
          quality: typeof values?.quality === "string" ? values.quality : "",
        };
        render();
      },
      (err) => {
        activeRun = null;
        lastResult = {
          success: false,
          rejected: true,
          timedOut: false,
          message: err?.message || "Calibration goal was rejected",
          imagesCaptured: fb?.imagesCaptured ?? 0,
          leftRms: 0,
          rightRms: 0,
          stereoRms: 0,
          quality: "",
        };
        render();
      },
    );
  }

  captureBtn.addEventListener("click", () => {
    if (!activeRun || activeRun.canceling) return;
    ros.publish(STEREO_CALIB_CAPTURE_TOPIC, { data: true });
  });

  stopBtn.addEventListener("click", () => {
    if (!activeRun || activeRun.canceling) return;
    activeRun.canceling = true;
    activeRun.cancel();
    render();
  });

  startBtn.addEventListener("click", startCalibration);

  /** @param {ResultState & { rejected?: boolean }} r */
  function renderResult(r) {
    result.replaceChildren();
    const banner = document.createElement("p");
    banner.className = "calib-result-banner" + (r.success ? " ok" : " bad");
    banner.textContent = r.success
      ? "Calibration succeeded"
      : r.timedOut
        ? "Run expired — no capture received in time"
        : "Calibration failed";
    const msg = document.createElement("p");
    msg.className = "calib-feedback-message";
    msg.textContent = r.message;
    result.append(banner, msg);
    if (r.success) {
      const stats = document.createElement("div");
      stats.className = "calib-result-stats";
      /** @param {string} label @param {string} value */
      const stat = (label, value) => {
        const el = document.createElement("div");
        el.className = "calib-stat-row";
        const l = document.createElement("span");
        l.className = "microlabel";
        l.textContent = label;
        const v = document.createElement("span");
        v.className = "calib-stat-value mono";
        v.textContent = value;
        el.append(l, v);
        return el;
      };
      stats.append(
        stat("Images captured", String(r.imagesCaptured)),
        Object.assign(stat("Left RMS", r.leftRms.toFixed(4)), { title: "Reprojection RMS error in pixels — lower is better" }),
        Object.assign(stat("Right RMS", r.rightRms.toFixed(4)), { title: "Reprojection RMS error in pixels — lower is better" }),
        Object.assign(stat("Stereo RMS", r.stereoRms.toFixed(4)), { title: "Stereo-pair reprojection RMS in pixels — lower is better" }),
      );

      if (r.quality) {
        const qualityRow = stat("Quality", r.quality);
        // Same RMS thresholds the server used to pick this label (see
        // run_calibration in stereo_calibrator.py) — just coloring what it
        // already decided, not re-deriving it client-side.
        qualityRow.querySelector(".calib-stat-value")?.classList.add(
          r.quality.startsWith("EXCELLENT") || r.quality.startsWith("GOOD")
            ? "calib-quality-ok"
            : r.quality.startsWith("ACCEPTABLE")
              ? "calib-quality-warn"
              : "calib-quality-bad",
        );
        stats.append(qualityRow);
      }

      result.append(stats);
    }
  }

  function render() {
    const running = !!activeRun;
    startBtn.hidden = running;
    captureBtn.hidden = !running;
    stopBtn.hidden = !running;
    startBtn.disabled = running || ros.state !== "connected";
    captureBtn.disabled = !activeRun || activeRun.canceling;
    stopBtn.disabled = !activeRun || activeRun.canceling;
    stopBtn.textContent = activeRun?.canceling ? "Stopping…" : "Stop";

    statusLine.textContent = activeRun
      ? "Calibration running — move the board and click Capture"
      : ros.state === "connected"
        ? "Idle"
        : "Not connected";

    feedback.hidden = fb === null;
    if (fb) {
      progressRow.value.textContent = `${fb.imagesCaptured} / ${fb.target}`;
      attemptsRow.value.textContent = String(fb.captureAttempts);

      // Display-only countdown to the server's capture-timeout watchdog — never
      // the source of truth. Hidden once the run isn't active, or if the server
      // hasn't told us a deadline yet; never shows "expired" itself (only the
      // real Result, via r.timedOut, gets to say the run actually ended).
      const remainingMs = fb.deadlineMs !== null ? fb.deadlineMs - Date.now() : null;
      countdownRow.row.hidden = !activeRun || remainingMs === null;
      if (activeRun && remainingMs !== null) {
        countdownRow.value.textContent = remainingMs > 0 ? `${Math.ceil(remainingMs / 1000)}s` : "wrapping up…";
        countdownRow.value.classList.toggle("calib-countdown-warn", remainingMs <= 15000);
      }

      boardBadge.classList.toggle("ok", fb.cornersFound === true);
      boardBadge.classList.toggle("bad", fb.cornersFound === false);
      boardBadge.textContent =
        fb.cornersFound === true
          ? "Board detected"
          : fb.cornersFound === false
            ? "Board not detected"
            : "Waiting for first capture";
      feedbackMessage.textContent = fb.message;
    }

    result.hidden = lastResult === null;
    if (lastResult) renderResult(lastResult);

    fileStatus.textContent =
      calibFileDetected === null
        ? "Calibration file: checking…"
        : calibFileDetected
          ? "Calibration file: detected"
          : "Calibration file: not detected";
    fileStatus.classList.toggle("ok", calibFileDetected === true);
    fileStatus.classList.toggle("bad", calibFileDetected === false);
  }

  const unsubState = ros.onStateChange(() => render());

  // A depth frame arriving at any point (even after the initial check window)
  // flips the indicator — a calibration saved mid-session shouldn't need a
  // page reload to be noticed.
  /** @type {number | null} */
  let calibCheckTimer = null;
  const unsubDepthCheck = ros.subscribe(
    MAIN_CAMERA_DEPTH_TOPIC,
    () => {
      if (calibFileDetected === true) return;
      calibFileDetected = true;
      if (calibCheckTimer !== null) {
        clearTimeout(calibCheckTimer);
        calibCheckTimer = null;
      }
      render();
    },
    5000,
    "sensor_msgs/msg/Image",
  );
  calibCheckTimer = setTimeout(() => {
    calibCheckTimer = null;
    if (calibFileDetected === null) {
      calibFileDetected = false;
      render();
    }
  }, CALIB_FILE_CHECK_TIMEOUT_MS);

  render();

  // Re-render on a plain 1s tick so the countdown display stays live between
  // feedback messages — it never recomputes the deadline itself, just repaints
  // against whatever fb.deadlineMs the last feedback tick set.
  const countdownTicker = setInterval(render, 1000);

  return {
    destroy() {
      unsubState();
      unsubDepthCheck();
      clearInterval(countdownTicker);
      if (calibCheckTimer !== null) clearTimeout(calibCheckTimer);
      // A run left going while the operator navigates away must not keep
      // capturing with no owner — cancel it, mirroring the skills menu.
      if (activeRun) activeRun.cancel();
      if (leftCoverage.img.dataset.blobUrl) URL.revokeObjectURL(leftCoverage.img.dataset.blobUrl);
      if (rightCoverage.img.dataset.blobUrl) URL.revokeObjectURL(rightCoverage.img.dataset.blobUrl);
      videoStage.destroy();
      releaseVideoSession();
      root.innerHTML = "";
    },
  };
}
