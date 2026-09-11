// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import { FAST_FOUNDATION_DEPTH_TOPIC, MAIN_CAMERA_DEPTH_TOPIC } from "../constants.js";

const DEPTH_NEAR_M = 0.2;
const DEPTH_FAR_M = 6.0;
const OVERLAY_ALPHA = 170;
const DEPTH_MODEL_KEY = "innate.teleop.depthModel";

const DEPTH_ICON =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="m12 3 7 4v10l-7 4-7-4V7z"/><path d="m19 7-7 4-7-4"/><path d="M12 11v10"/>' +
  "</svg>";

const DEPTH_SOURCES = [
  { id: "classical", label: "Classical", topic: MAIN_CAMERA_DEPTH_TOPIC },
  { id: "fast_foundation", label: "Fast Foundation", topic: FAST_FOUNDATION_DEPTH_TOPIC },
];

/**
 * @typedef {{ width: number, height: number, depthM: Float32Array }} DepthFrame
 */

/**
 * @param {HTMLElement} controlsParent
 * @param {HTMLElement} stageEl
 * @param {HTMLVideoElement} videoEl
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createDepthProbe(controlsParent, stageEl, videoEl, session, rosClient) {
  const controls = document.createElement("div");
  controls.className = "depth-controls";
  controlsParent.appendChild(controls);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-toggle depth-toggle";
  button.innerHTML = DEPTH_ICON;
  button.setAttribute("aria-label", "Depth overlay");
  button.setAttribute("aria-pressed", "false");
  controls.appendChild(button);

  const modelSelect = document.createElement("select");
  modelSelect.className = "depth-model-select mono";
  modelSelect.setAttribute("aria-label", "Depth model");
  modelSelect.title = "Depth model";
  for (const source of DEPTH_SOURCES) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = source.label;
    modelSelect.appendChild(option);
  }
  controls.appendChild(modelSelect);

  const canvas = document.createElement("canvas");
  canvas.className = "depth-overlay";
  canvas.hidden = true;

  const readout = document.createElement("div");
  readout.className = "depth-cursor microlabel mono";
  readout.hidden = true;

  stageEl.append(canvas, readout);

  const ctx = canvas.getContext("2d");

  let enabled = false;
  let mainCamera = true;
  let source = sourceById(loadDepthModel()) ?? DEPTH_SOURCES[0];
  modelSelect.value = source.id;
  modelSelect.hidden = true;
  /** @type {DepthFrame | null} */
  let frame = null;
  /** @type {ImageData | null} */
  let image = null;
  /** @type {{ x: number, y: number } | null} */
  let pointer = null;
  /** @type {(() => void) | null} */
  let unsubDepth = null;

  const unsubSession = session.onChange(() => {
    mainCamera = session.primaryCamera.name === "main";
    syncButton();
    syncOverlay();
  });

  const observer = "ResizeObserver" in window ? new ResizeObserver(() => syncOverlay()) : null;
  observer?.observe(videoEl);

  const onPointerMove = (event) => {
    pointer = { x: event.clientX, y: event.clientY };
    updateReadout(event.clientX, event.clientY);
  };

  const onPointerLeave = () => {
    pointer = null;
    readout.hidden = true;
  };

  videoEl.addEventListener("pointermove", onPointerMove);
  videoEl.addEventListener("pointerleave", onPointerLeave);
  videoEl.addEventListener("loadedmetadata", syncOverlay);
  window.addEventListener("resize", syncOverlay);

  button.addEventListener("click", () => {
    enabled = !enabled;
    if (enabled) subscribeDepth();
    else unsubscribeDepth();
    syncButton();
    syncOverlay();
  });

  modelSelect.addEventListener("change", () => {
    const next = sourceById(modelSelect.value);
    if (!next || next.id === source.id) return;
    source = next;
    saveDepthModel(next.id);
    frame = null;
    image = null;
    if (enabled) {
      unsubscribeDepth();
      subscribeDepth();
    }
    syncButton();
    syncOverlay();
  });

  syncButton();

  function subscribeDepth() {
    if (unsubDepth) return;
    unsubDepth = rosClient.subscribe(
      source.topic,
      (msg) => {
        const next = decodeDepthMessage(msg);
        if (!next) return;
        frame = next;
        syncOverlay();
      },
      undefined,
      "sensor_msgs/msg/Image",
    );
  }

  function unsubscribeDepth() {
    unsubDepth?.();
    unsubDepth = null;
  }

  function syncButton() {
    button.classList.toggle("active", enabled);
    button.classList.toggle("limited", enabled && !mainCamera);
    button.setAttribute("aria-pressed", String(enabled));
    modelSelect.hidden = !enabled;
    button.title = !enabled
      ? `Show depth overlay (${source.label})`
      : mainCamera
        ? `Hide depth overlay (${source.label})`
        : "Depth overlay is available on Main camera only";
  }

  function syncOverlay() {
    const show = enabled && mainCamera && !!frame && !!ctx;
    canvas.hidden = !show;
    if (!show) {
      readout.hidden = true;
      return;
    }
    syncOverlayGeometry();
    paintDepth();
    if (pointer) updateReadout(pointer.x, pointer.y);
  }

  function syncOverlayGeometry() {
    if (canvas.hidden || !frame) return;
    const box = videoContentBox();
    if (!box) {
      canvas.hidden = true;
      readout.hidden = true;
      return;
    }
    canvas.hidden = false;
    canvas.style.left = `${box.left}px`;
    canvas.style.top = `${box.top}px`;
    canvas.style.width = `${box.width}px`;
    canvas.style.height = `${box.height}px`;
  }

  function videoContentBox() {
    const w = videoEl.clientWidth;
    const h = videoEl.clientHeight;
    if (!w || !h) return null;
    const srcW = videoEl.videoWidth || frame?.width || w;
    const srcH = videoEl.videoHeight || frame?.height || h;
    if (!srcW || !srcH) return null;
    const scale = Math.min(w / srcW, h / srcH);
    const width = srcW * scale;
    const height = srcH * scale;
    return { left: (w - width) / 2, top: (h - height) / 2, width, height };
  }

  function updateReadout(clientX, clientY) {
    if (!enabled || !mainCamera || !frame) {
      readout.hidden = true;
      return;
    }
    const sample = sampleAt(clientX, clientY);
    if (!sample) {
      readout.hidden = true;
      return;
    }
    const d = sample.depth;
    readout.textContent = Number.isFinite(d) ? `${d.toFixed(d < 10 ? 2 : 1)} m` : "no depth";
    const stageRect = stageEl.getBoundingClientRect();
    const x = clientX - stageRect.left + 12;
    const y = clientY - stageRect.top - 18;
    const pad = 8;
    const maxX = Math.max(pad, stageRect.width - pad);
    const maxY = Math.max(pad, stageRect.height - pad);
    readout.style.left = `${Math.min(maxX, Math.max(pad, x))}px`;
    readout.style.top = `${Math.min(maxY, Math.max(pad, y))}px`;
    readout.hidden = false;
  }

  function sampleAt(clientX, clientY) {
    if (!frame) return null;
    const box = videoContentBox();
    if (!box) return null;
    const rect = videoEl.getBoundingClientRect();
    const x = clientX - rect.left - box.left;
    const y = clientY - rect.top - box.top;
    if (x < 0 || y < 0 || x > box.width || y > box.height) return null;
    const u = x / box.width;
    const v = y / box.height;
    const px = Math.min(frame.width - 1, Math.max(0, Math.round(u * (frame.width - 1))));
    const py = Math.min(frame.height - 1, Math.max(0, Math.round(v * (frame.height - 1))));
    return { depth: frame.depthM[py * frame.width + px] };
  }

  function paintDepth() {
    if (!ctx || !frame || canvas.hidden) return;
    const { width, height, depthM } = frame;
    if (!image || image.width !== width || image.height !== height) image = ctx.createImageData(width, height);
    const pixels = image.data;
    for (let i = 0; i < depthM.length; i++) {
      const d = depthM[i];
      const offset = i * 4;
      if (!Number.isFinite(d)) {
        pixels[offset + 3] = 0;
        continue;
      }
      const t = 1 - clamp01((d - DEPTH_NEAR_M) / (DEPTH_FAR_M - DEPTH_NEAR_M));
      const [r, g, b] = depthColor(t);
      pixels[offset] = r;
      pixels[offset + 1] = g;
      pixels[offset + 2] = b;
      pixels[offset + 3] = OVERLAY_ALPHA;
    }
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    ctx.putImageData(image, 0, 0);
  }

  return {
    destroy() {
      unsubSession();
      unsubscribeDepth();
      observer?.disconnect();
      window.removeEventListener("resize", syncOverlay);
      videoEl.removeEventListener("loadedmetadata", syncOverlay);
      videoEl.removeEventListener("pointermove", onPointerMove);
      videoEl.removeEventListener("pointerleave", onPointerLeave);
      controls.remove();
      canvas.remove();
      readout.remove();
    },
  };
}

/** @param {string} id */
function sourceById(id) {
  return DEPTH_SOURCES.find((source) => source.id === id) ?? null;
}

function loadDepthModel() {
  try {
    const value = localStorage.getItem(DEPTH_MODEL_KEY);
    return typeof value === "string" ? value : "";
  } catch {
    return "";
  }
}

/** @param {string} id */
function saveDepthModel(id) {
  try {
    localStorage.setItem(DEPTH_MODEL_KEY, id);
  } catch {
    // Ignore storage errors (private mode, restricted storage).
  }
}

/**
 * @param {any} msg
 * @returns {DepthFrame | null}
 */
function decodeDepthMessage(msg) {
  const width = Number(msg?.width);
  const height = Number(msg?.height);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
  const encoding = typeof msg?.encoding === "string" ? msg.encoding.toLowerCase() : "";
  const bytes = decodeByteField(msg?.data);
  if (!bytes) return null;
  const little = !(msg?.is_bigendian === true || msg?.is_bigendian === 1);
  if (encoding === "16uc1" || encoding === "mono16") {
    return decode16BitDepth(bytes, width, height, Number(msg?.step), little);
  }
  if (encoding === "32fc1" || encoding === "mono32") {
    return decode32BitDepth(bytes, width, height, Number(msg?.step), little);
  }
  return null;
}

/**
 * @param {Uint8Array} bytes
 * @param {number} width
 * @param {number} height
 * @param {number} step
 * @param {boolean} little
 * @returns {DepthFrame | null}
 */
function decode16BitDepth(bytes, width, height, step, little) {
  const rowStep = Number.isFinite(step) && step > 0 ? step : width * 2;
  if (bytes.length < rowStep * height) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const depthM = new Float32Array(width * height);
  for (let y = 0; y < height; y++) {
    const row = y * rowStep;
    for (let x = 0; x < width; x++) {
      const raw = view.getUint16(row + x * 2, little);
      depthM[y * width + x] = raw > 0 ? raw / 1000 : NaN;
    }
  }
  return { width, height, depthM };
}

/**
 * @param {Uint8Array} bytes
 * @param {number} width
 * @param {number} height
 * @param {number} step
 * @param {boolean} little
 * @returns {DepthFrame | null}
 */
function decode32BitDepth(bytes, width, height, step, little) {
  const rowStep = Number.isFinite(step) && step > 0 ? step : width * 4;
  if (bytes.length < rowStep * height) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const depthM = new Float32Array(width * height);
  for (let y = 0; y < height; y++) {
    const row = y * rowStep;
    for (let x = 0; x < width; x++) {
      const value = view.getFloat32(row + x * 4, little);
      depthM[y * width + x] = Number.isFinite(value) && value > 0 ? value : NaN;
    }
  }
  return { width, height, depthM };
}

/**
 * @param {any} field
 * @returns {Uint8Array | null}
 */
function decodeByteField(field) {
  if (field instanceof Uint8Array) return field;
  if (typeof field === "string") {
    try {
      return base64ToBytes(field);
    } catch {
      return null;
    }
  }
  if (ArrayBuffer.isView(field)) return new Uint8Array(field.buffer, field.byteOffset, field.byteLength);
  if (field instanceof ArrayBuffer) return new Uint8Array(field);
  if (Array.isArray(field)) return Uint8Array.from(field);
  return null;
}

/** @param {string} b64 @returns {Uint8Array} */
function base64ToBytes(b64) {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

/** @param {number} t @returns {[number, number, number]} */
function depthColor(t) {
  const c0 = [28, 36, 122];
  const c1 = [53, 143, 210];
  const c2 = [77, 201, 124];
  const c3 = [238, 201, 70];
  const c4 = [226, 70, 61];
  const scaled = clamp01(t) * 4;
  const segment = Math.min(3, Math.floor(scaled));
  const local = scaled - segment;
  const from = segment === 0 ? c0 : segment === 1 ? c1 : segment === 2 ? c2 : c3;
  const to = segment === 0 ? c1 : segment === 1 ? c2 : segment === 2 ? c3 : c4;
  return [
    Math.round(from[0] + (to[0] - from[0]) * local),
    Math.round(from[1] + (to[1] - from[1]) * local),
    Math.round(from[2] + (to[2] - from[2]) * local),
  ];
}

/** @param {number} v */
function clamp01(v) {
  if (v < 0) return 0;
  if (v > 1) return 1;
  return v;
}
