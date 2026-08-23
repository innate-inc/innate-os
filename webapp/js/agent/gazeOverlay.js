// @ts-check
// Gaze debugger — projects the brain's normalized person geometry onto the
// already-streaming main camera. No images cross rosbridge.

import { GAZE_TOPIC, SET_FOLLOWING_SERVICE } from "../constants.js";

const STALE_AFTER_MS = 1_200;
const SVG_NS = "http://www.w3.org/2000/svg";
const POSE_EDGES = [
  ["left_eye", "right_eye"],
  ["left_eye", "nose"],
  ["right_eye", "nose"],
  ["left_eye", "left_ear"],
  ["right_eye", "right_ear"],
  ["left_ear", "left_shoulder"],
  ["right_ear", "right_shoulder"],
  ["left_shoulder", "right_shoulder"],
  ["left_shoulder", "left_elbow"],
  ["left_elbow", "left_wrist"],
  ["right_shoulder", "right_elbow"],
  ["right_elbow", "right_wrist"],
  ["left_shoulder", "left_hip"],
  ["right_shoulder", "right_hip"],
  ["left_hip", "right_hip"],
  ["left_hip", "left_knee"],
  ["left_knee", "left_ankle"],
  ["right_hip", "right_knee"],
  ["right_knee", "right_ankle"],
];

/**
 * @param {HTMLElement} stage
 * @param {import("../rosClient.js").RosClient} ros
 * @param {GazeOverlaySession} session
 * @param {HTMLElement} panelHost
 * @returns {{ setFollowDebugVisible: (visible: boolean) => void, destroy: () => void }}
 */
export function createGazeOverlay(stage, ros, session, panelHost) {
  const root = document.createElement("div");
  root.className = "gaze-debug";

  const content = document.createElement("div");
  content.className = "gaze-debug-content";
  content.setAttribute("aria-hidden", "true");
  const zone = document.createElement("div");
  zone.className = "gaze-zone";
  const pose = document.createElementNS(SVG_NS, "svg");
  pose.classList.add("gaze-pose");
  pose.setAttribute("viewBox", "0 0 1 1");
  pose.setAttribute("preserveAspectRatio", "none");
  const boxes = document.createElement("div");
  boxes.className = "gaze-boxes";
  content.append(zone, pose, boxes);

  const status = document.createElement("div");
  status.className = "gaze-status microlabel";
  status.setAttribute("aria-live", "polite");
  const followPanel = document.createElement("div");
  followPanel.className = "gaze-follow-panel";
  const followToggle = document.createElement("button");
  followToggle.type = "button";
  followToggle.className = "gaze-follow-toggle";
  const followTelemetry = document.createElement("div");
  followTelemetry.className = "gaze-follow-telemetry";
  followTelemetry.innerHTML = `
    <div class="gaze-follow-head">
      <span class="gaze-follow-title">FOLLOW DEBUG</span>
      <span class="gaze-follow-state">IDLE · IDLE</span>
    </div>
    <div class="gaze-follow-gauge">
      <div class="gaze-follow-gauge-label"><span>BODY SIZE</span><span data-follow-size>—</span></div>
      <div class="gaze-follow-track gaze-follow-size-track">
        <span class="gaze-follow-target-band"></span><span class="gaze-follow-size-dot"></span>
      </div>
      <div class="gaze-follow-axis"><span>TOO FAR</span><span>TARGET</span><span>TOO CLOSE</span></div>
    </div>
    <div class="gaze-follow-gauge">
      <div class="gaze-follow-gauge-label"><span>PERSON CENTER</span><span data-follow-center>—</span></div>
      <div class="gaze-follow-track gaze-follow-center-track">
        <span class="gaze-follow-target-band"></span><span class="gaze-follow-center-dot"></span>
      </div>
      <div class="gaze-follow-axis"><span>LEFT</span><span>CENTER</span><span>RIGHT</span></div>
    </div>
    <div class="gaze-follow-metrics">
      <div><span>BEARING</span><strong data-follow-bearing>—</strong></div>
      <div><span>NEXT STEP</span><strong data-follow-step>—</strong></div>
      <div><span>VISION AGE</span><strong data-follow-age>—</strong></div>
      <div><span>NAV GOALS</span><strong data-follow-goals>—</strong></div>
    </div>
    <div class="gaze-follow-details"></div>
  `;
  const followState = /** @type {HTMLElement} */ (followTelemetry.querySelector(".gaze-follow-state"));
  const followSize = /** @type {HTMLElement} */ (followTelemetry.querySelector("[data-follow-size]"));
  const followSizeDot = /** @type {HTMLElement} */ (followTelemetry.querySelector(".gaze-follow-size-dot"));
  const followCenter = /** @type {HTMLElement} */ (followTelemetry.querySelector("[data-follow-center]"));
  const followCenterDot = /** @type {HTMLElement} */ (followTelemetry.querySelector(".gaze-follow-center-dot"));
  const followBearing = /** @type {HTMLElement} */ (followTelemetry.querySelector("[data-follow-bearing]"));
  const followStep = /** @type {HTMLElement} */ (followTelemetry.querySelector("[data-follow-step]"));
  const followAge = /** @type {HTMLElement} */ (followTelemetry.querySelector("[data-follow-age]"));
  const followGoals = /** @type {HTMLElement} */ (followTelemetry.querySelector("[data-follow-goals]"));
  const followDetails = /** @type {HTMLElement} */ (followTelemetry.querySelector(".gaze-follow-details"));
  const followCommand = document.createElement("div");
  followCommand.className = "gaze-follow-command";
  followPanel.append(followToggle, followTelemetry, followCommand);
  root.append(content, status);
  stage.append(root);
  panelHost.append(followPanel);

  /** @type {GazeDebug | null} */
  let debug = null;
  let stale = false;
  let followBusy = false;
  let followDebugVisible = true;
  let followCommandText = "";
  let followCommandFailed = false;
  /** @type {number | null} */
  let staleTimer = null;

  const surface = () =>
    /** @type {HTMLVideoElement | HTMLCanvasElement | null} */ (
      stage.querySelector(":scope > video, :scope > canvas")
    );

  const primaryCamera = () => {
    const primary = session.primaryCamera;
    return typeof primary === "string" ? primary : primary?.name;
  };

  const visible = () => {
    const cockpit = stage.closest(".agent-cockpit");
    const view = surface();
    if (!debug || primaryCamera() !== "main") return false;
    if (cockpit?.classList.contains("brain-open") || cockpit?.classList.contains("cam-map-primary")) return false;
    return !(view instanceof HTMLVideoElement) || stage.classList.contains("video-ready");
  };

  const render = () => {
    const isVisible = visible();
    root.hidden = !isVisible;
    followPanel.hidden = !isVisible || !followDebugVisible;
    if (root.hidden || !debug) return;

    root.dataset.status = stale ? "stale" : debug.detector?.error ? "error" : debug.status;
    root.dataset.following = debug.follow?.enabled ? "true" : "false";
    status.textContent = stale ? "GAZE STALE" : statusText(debug);
    content.hidden =
      stale ||
      debug.status === "off" ||
      debug.status === "paused" ||
      debug.status === "starting" ||
      debug.status === "error";
    pose.replaceChildren();
    boxes.replaceChildren();
    const follow = debug.follow;
    const canStartFollowing = debug.status === "locked";
    followToggle.textContent = followBusy
      ? "WORKING…"
      : follow?.enabled
        ? "STOP FOLLOWING"
        : canStartFollowing
          ? "START FOLLOWING"
          : "WAIT FOR PERSON LOCK";
    followToggle.classList.toggle("active", follow?.enabled === true);
    followToggle.setAttribute("aria-pressed", String(follow?.enabled === true));
    followToggle.disabled =
      followBusy ||
      stale ||
      ros.state !== "connected" ||
      debug.status === "off" ||
      debug.status === "paused" ||
      debug.status === "starting" ||
      debug.status === "error" ||
      (!follow?.enabled && !canStartFollowing);
    followToggle.title = followToggle.disabled
      ? "Activate a gaze-enabled agent and wait for PERSON LOCKED"
      : follow?.enabled
        ? "Cancel person following"
        : "Follow the currently locked person";
    renderFollowTelemetry(debug);
    followCommand.textContent = followCommandText;
    followCommand.dataset.kind = followCommandFailed ? "error" : "ok";

    if (!content.hidden) {
      place(zone, {
        center_x: (debug.zone.left + debug.zone.right) / 2,
        center_y: (debug.zone.top + debug.zone.bottom) / 2,
        width: debug.zone.right - debug.zone.left,
        height: debug.zone.bottom - debug.zone.top,
      });
      zone.style.setProperty("--gaze-progress", `${Math.max(0, Math.min(1, debug.progress)) * 100}%`);
      for (const person of debug.people ?? []) addPerson(person);
      let targetDrawn = false;
      for (const face of debug.faces) {
        const target = debug.target !== null && sameBox(face, debug.target);
        addBox(face, target ? "gaze-face gaze-target" : "gaze-face");
        targetDrawn ||= target;
      }
      if (debug.target && !targetDrawn) addBox(debug.target, "gaze-face gaze-target");
      positionContent();
    }
  };

  /** @param {GazeDebug} current */
  function renderFollowTelemetry(current) {
    const follow = current.follow;
    if (!follow) {
      followState.textContent = "WAITING FOR TELEMETRY";
      return;
    }
    const reference = follow.reference_height ?? 0;
    const observed = follow.observed_height ?? 0;
    const ratio = reference > 0 ? observed / reference : 1;
    const sizeError = (1 - ratio) * 100;
    const center = typeof follow.body_center_x === "number" ? follow.body_center_x : 0.5;
    const age = follow.perception_age_ms ?? 0;
    const goal = follow.goal;
    const reason = follow.reason || "—";
    const followMode = follow.state || "idle";
    const navigationMode = follow.nav_state || "idle";

    followPanel.dataset.state =
      navigationMode === "failed" || navigationMode === "unavailable"
        ? "error"
        : follow.enabled
          ? "active"
          : follow.reason
            ? "stopped"
            : "idle";
    followState.textContent = `${followMode.toUpperCase()} · ${navigationMode.toUpperCase()}`;
    followSize.textContent =
      reference > 0
        ? `${(observed * 100).toFixed(1)}% / ${(reference * 100).toFixed(1)}% · ${Math.abs(sizeError).toFixed(1)}% ${sizeError >= 0 ? "FAR" : "CLOSE"}`
        : "WAITING FOR REFERENCE";
    followSizeDot.style.left = `${clamp((ratio - 0.5) * 100, 0, 100)}%`;
    followCenter.textContent = `X ${center.toFixed(3)} · ${Math.abs((center - 0.5) * 200).toFixed(1)}% OFF`;
    followCenterDot.style.left = `${clamp(center * 100, 0, 100)}%`;
    followBearing.textContent = `${(follow.bearing_degrees ?? 0).toFixed(1)}°`;
    followStep.textContent = `${(follow.forward_m ?? 0).toFixed(2)} m`;
    followAge.textContent = `${age.toFixed(0)} ms`;
    followAge.dataset.fresh = age <= 300 ? "true" : "false";
    followGoals.textContent = `${follow.nav_active ?? 0} active · ${follow.nav_pending ?? 0} pending`;
    followDetails.textContent = [
      `ODOM GOAL  ${goal ? `${goal.x.toFixed(2)}, ${goal.y.toFixed(2)}, ${goal.yaw_degrees.toFixed(0)}°` : "—"}`,
      `STOP REASON  ${reason}`,
      `FRAME ${current.frame}  ·  INFERENCE ${(current.detector?.inference_ms ?? 0).toFixed(1)} ms`,
    ].join("\n");
  }

  followToggle.addEventListener("click", async () => {
    if (!debug || followBusy) return;
    const enable = !debug.follow?.enabled;
    followBusy = true;
    followCommandText = enable ? "Requesting follow…" : "Stopping follow…";
    followCommandFailed = false;
    render();
    try {
      const response = await ros.callService(SET_FOLLOWING_SERVICE, { data: enable });
      followCommandText = String(response?.message || (enable ? "Follow request sent." : "Stop request sent."));
      followCommandFailed = response?.success === false;
    } catch (error) {
      followCommandText = error instanceof Error ? error.message : String(error);
      followCommandFailed = true;
    } finally {
      followBusy = false;
      render();
    }
  });

  /** @param {GazePerson} person */
  const addPerson = (person) => {
    addBox(
      person.body,
      person.target ? "gaze-person gaze-person-target" : "gaze-person",
      person.confidence.toFixed(2),
    );
    const points = new Map(person.keypoints.map((point) => [point.name, point]));
    for (const [fromName, toName] of POSE_EDGES) {
      const from = points.get(fromName);
      const to = points.get(toName);
      if (from && to) addPoseLine(from, to, person.target ? "gaze-bone gaze-bone-target" : "gaze-bone");
    }
    for (const point of points.values()) addPosePoint(point, person.target ? "gaze-kp gaze-kp-target" : "gaze-kp");
    if (person.target) addAim(person.head);
  };

  /** @param {GazeKeypoint} from @param {GazeKeypoint} to @param {string} className */
  const addPoseLine = (from, to, className) => {
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", `${from.x}`);
    line.setAttribute("y1", `${from.y}`);
    line.setAttribute("x2", `${to.x}`);
    line.setAttribute("y2", `${to.y}`);
    line.setAttribute("class", className);
    pose.append(line);
  };

  /** @param {GazeKeypoint} point @param {string} className */
  const addPosePoint = (point, className) => {
    const marker = document.createElementNS(SVG_NS, "line");
    marker.setAttribute("x1", `${point.x}`);
    marker.setAttribute("x2", `${point.x}`);
    marker.setAttribute("y1", `${point.y}`);
    marker.setAttribute("y2", `${point.y}`);
    marker.setAttribute("class", className);
    pose.append(marker);
  };

  /** @param {GazeBox} head */
  const addAim = (head) => {
    const horizontal = document.createElementNS(SVG_NS, "line");
    horizontal.setAttribute("x1", `${head.center_x - 0.015}`);
    horizontal.setAttribute("x2", `${head.center_x + 0.015}`);
    horizontal.setAttribute("y1", `${head.center_y}`);
    horizontal.setAttribute("y2", `${head.center_y}`);
    horizontal.setAttribute("class", "gaze-aim");
    const vertical = document.createElementNS(SVG_NS, "line");
    vertical.setAttribute("x1", `${head.center_x}`);
    vertical.setAttribute("x2", `${head.center_x}`);
    vertical.setAttribute("y1", `${head.center_y - 0.015}`);
    vertical.setAttribute("y2", `${head.center_y + 0.015}`);
    vertical.setAttribute("class", "gaze-aim");
    pose.append(horizontal, vertical);
  };

  /** @param {GazeBox} face @param {string} className @param {string} [score] */
  const addBox = (face, className, score) => {
    const box = document.createElement("div");
    box.className = className;
    if (score) box.dataset.score = score;
    place(box, face);
    boxes.append(box);
  };

  const positionContent = () => {
    const view = surface();
    if (!view || !debug) return;
    const rect = paintedContentRect(stage, view, debug.image);
    content.style.left = `${rect.left}px`;
    content.style.top = `${rect.top}px`;
    content.style.width = `${rect.width}px`;
    content.style.height = `${rect.height}px`;
  };

  const onMessage = (/** @type {any} */ msg) => {
    const next = parseDebug(msg);
    if (!next) return;
    debug = next;
    stale = false;
    if (staleTimer !== null) clearTimeout(staleTimer);
    staleTimer = null;
    if (next.status !== "off" && next.status !== "paused") {
      staleTimer = window.setTimeout(() => {
        stale = true;
        render();
      }, STALE_AFTER_MS);
    }
    render();
  };

  const unsubscribeGaze = ros.subscribe(GAZE_TOPIC, onMessage, 0, "std_msgs/msg/String");
  const unsubscribeConnection = ros.onStateChange(() => render());
  const unsubscribeSession = session.onChange(() => render());
  const resizeObserver = new ResizeObserver(() => positionContent());
  resizeObserver.observe(stage);
  const video = stage.querySelector("video");
  video?.addEventListener("loadedmetadata", positionContent);

  return {
    setFollowDebugVisible(visible) {
      followDebugVisible = visible;
      render();
    },
    destroy() {
      unsubscribeGaze();
      unsubscribeConnection();
      unsubscribeSession();
      resizeObserver.disconnect();
      video?.removeEventListener("loadedmetadata", positionContent);
      if (staleTimer !== null) clearTimeout(staleTimer);
      root.remove();
      followPanel.remove();
    },
  };
}

/** @param {GazeDebug} debug */
function statusText(debug) {
  const detector = debug.detector;
  if (!detector) return legacyStatusText(debug);
  const prefix = detector.model === "yolov8n-pose" ? "YOLO POSE" : detector.model.toUpperCase();
  if (detector.error) return `${prefix} · ERROR: ${detector.error.slice(0, 80).toUpperCase()}`;
  if (debug.follow?.enabled) {
    const size =
      debug.follow.reference_height > 0
        ? Math.round((debug.follow.observed_height / debug.follow.reference_height) * 100)
        : 100;
    return `${prefix} · PERSON FOLLOW · ${debug.follow.nav_state.toUpperCase()} · SIZE ${size}%`;
  }
  if (debug.follow?.reason) {
    return `${prefix} · FOLLOW STOPPED: ${debug.follow.reason.slice(0, 64).toUpperCase()}`;
  }
  if (debug.status === "centering") return `${prefix} · CENTERING ${Math.round(debug.progress * 100)}%`;
  if (debug.status === "locked") return `${prefix} · PERSON LOCKED`;
  if (debug.status === "too_far") {
    const target = debug.people?.find((person) => person.target);
    return target?.head_visible === false ? `${prefix} · LOOKING UP FOR A HEAD` : `${prefix} · PERSON TOO SMALL`;
  }
  if (debug.status === "following") {
    const count = debug.people?.length ?? debug.faces.length;
    const latency = detector.inference_ms ? ` · ${Math.round(detector.inference_ms)} MS` : "";
    return `${prefix} · FOLLOWING · ${count} ${count === 1 ? "PERSON" : "PEOPLE"}${latency}`;
  }
  if (debug.status === "searching") return `${prefix} · SEARCHING FOR A PERSON`;
  if (debug.status === "starting") return `STARTING ${prefix}`;
  if (debug.status === "paused") return "GAZE PAUSED";
  return "GAZE OFF";
}

/** @param {number} value @param {number} minimum @param {number} maximum */
function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

/** @param {GazeDebug} debug */
function legacyStatusText(debug) {
  if (debug.status === "centering") return `CENTERING ${Math.round(debug.progress * 100)}%`;
  if (debug.status === "locked") return "PERSON LOCKED";
  if (debug.status === "too_far") return "FACE TOO SMALL";
  if (debug.status === "following") return `FOLLOWING · ${debug.faces.length} FACE${debug.faces.length === 1 ? "" : "S"}`;
  if (debug.status === "searching") return "SEARCHING FOR A FACE";
  if (debug.status === "starting") return "STARTING FACE DETECTOR";
  if (debug.status === "paused") return "GAZE PAUSED";
  return "GAZE OFF";
}

/** @param {HTMLElement} element @param {GazeBox} box */
function place(element, box) {
  element.style.left = `${(box.center_x - box.width / 2) * 100}%`;
  element.style.top = `${(box.center_y - box.height / 2) * 100}%`;
  element.style.width = `${box.width * 100}%`;
  element.style.height = `${box.height * 100}%`;
}

/** @param {GazeBox} left @param {GazeBox} right */
function sameBox(left, right) {
  return (
    left.center_x === right.center_x &&
    left.center_y === right.center_y &&
    left.width === right.width &&
    left.height === right.height
  );
}

/**
 * @param {HTMLElement} stage
 * @param {HTMLVideoElement | HTMLCanvasElement} surface
 * @param {{ width: number, height: number }} image
 */
function paintedContentRect(stage, surface, image) {
  const stageRect = stage.getBoundingClientRect();
  const elementRect = surface.getBoundingClientRect();
  const sourceWidth = surface instanceof HTMLVideoElement ? surface.videoWidth : image.width || surface.width;
  const sourceHeight = surface instanceof HTMLVideoElement ? surface.videoHeight : image.height || surface.height;
  const style = getComputedStyle(surface);
  const fit = style.objectFit;

  if (!sourceWidth || !sourceHeight || (fit !== "contain" && fit !== "cover")) {
    return relativeRect(stageRect, elementRect);
  }

  const scale =
    fit === "cover"
      ? Math.max(elementRect.width / sourceWidth, elementRect.height / sourceHeight)
      : Math.min(elementRect.width / sourceWidth, elementRect.height / sourceHeight);
  const width = sourceWidth * scale;
  const height = sourceHeight * scale;
  const [xPosition = "50%", yPosition = "50%"] = style.objectPosition.split(/\s+/);
  const left = elementRect.left - stageRect.left + positionOffset(elementRect.width - width, xPosition);
  const top = elementRect.top - stageRect.top + positionOffset(elementRect.height - height, yPosition);
  return { left, top, width, height };
}

/** @param {DOMRect} parent @param {DOMRect} child */
function relativeRect(parent, child) {
  return {
    left: child.left - parent.left,
    top: child.top - parent.top,
    width: child.width,
    height: child.height,
  };
}

/** @param {number} freeSpace @param {string} position */
function positionOffset(freeSpace, position) {
  if (position === "left" || position === "top") return 0;
  if (position === "right" || position === "bottom") return freeSpace;
  if (position === "center") return freeSpace / 2;
  const percent = Number.parseFloat(position);
  return Number.isFinite(percent) ? (freeSpace * percent) / 100 : freeSpace / 2;
}

/** @param {any} msg @returns {GazeDebug | null} */
function parseDebug(msg) {
  try {
    const value = JSON.parse(msg.data);
    if (!value || typeof value.status !== "string" || !Array.isArray(value.faces)) return null;
    return value;
  } catch {
    return null;
  }
}
