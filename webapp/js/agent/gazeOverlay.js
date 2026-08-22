// @ts-check
// Gaze debugger — projects the brain's normalized face geometry onto the
// already-streaming main camera. No images cross rosbridge.

import { GAZE_TOPIC } from "../constants.js";

const STALE_AFTER_MS = 1_200;

/**
 * @param {HTMLElement} stage
 * @param {import("../rosClient.js").RosClient} ros
 * @param {GazeOverlaySession} session
 * @returns {{ destroy: () => void }}
 */
export function createGazeOverlay(stage, ros, session) {
  const root = document.createElement("div");
  root.className = "gaze-debug";
  root.setAttribute("aria-hidden", "true");

  const content = document.createElement("div");
  content.className = "gaze-debug-content";
  const zone = document.createElement("div");
  zone.className = "gaze-zone";
  const boxes = document.createElement("div");
  boxes.className = "gaze-boxes";
  content.append(zone, boxes);

  const status = document.createElement("div");
  status.className = "gaze-status microlabel";
  root.append(content, status);
  stage.append(root);

  /** @type {GazeDebug | null} */
  let debug = null;
  let stale = false;
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
    root.hidden = !visible();
    if (root.hidden || !debug) return;

    root.dataset.status = stale ? "stale" : debug.status;
    status.textContent = stale ? "GAZE STALE" : statusText(debug);
    content.hidden = stale || debug.status === "off" || debug.status === "paused" || debug.status === "starting";
    boxes.replaceChildren();

    if (!content.hidden) {
      place(zone, {
        center_x: (debug.zone.left + debug.zone.right) / 2,
        center_y: (debug.zone.top + debug.zone.bottom) / 2,
        width: debug.zone.right - debug.zone.left,
        height: debug.zone.bottom - debug.zone.top,
      });
      zone.style.setProperty("--gaze-progress", `${Math.max(0, Math.min(1, debug.progress)) * 100}%`);
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

  /** @param {GazeBox} face @param {string} className */
  const addBox = (face, className) => {
    const box = document.createElement("div");
    box.className = className;
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
  const unsubscribeSession = session.onChange(() => render());
  const resizeObserver = new ResizeObserver(() => positionContent());
  resizeObserver.observe(stage);
  const video = stage.querySelector("video");
  video?.addEventListener("loadedmetadata", positionContent);

  return {
    destroy() {
      unsubscribeGaze();
      unsubscribeSession();
      resizeObserver.disconnect();
      video?.removeEventListener("loadedmetadata", positionContent);
      if (staleTimer !== null) clearTimeout(staleTimer);
      root.remove();
    },
  };
}

/** @param {GazeDebug} debug */
function statusText(debug) {
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
