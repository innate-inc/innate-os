// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Projects the planner's route onto the main camera as a green ground ribbon.

import {
  PLAN_TOPICS,
  ODOM_TOPIC,
  AMCL_POSE_TOPIC,
  HEAD_CURRENT_POSITION_TOPIC,
} from "../constants.js";

// Keep in sync with innate-controller-app's calibrated TrajectoryOverlay.
export const CAMERA = {
  HEIGHT_M: 0.28,
  MIN_PITCH_DEG: -30,
  MAX_PITCH_DEG: 30,
  PITCH_HEIGHT_COMP_M: 0.05,
  CALIB_W: 320,
  CALIB_H: 240,
  FX: 132.858,
  FY: 132.71,
  CX: 152.966,
  CY: 119.129,
  FY_SCALE: 1.15,
};

const RIBBON_FILL = "rgba(0, 255, 136, 0.85)";
// The planner republishes while driving and stops on arrival.
const NAV_STALE_MS = 4000;
const NEAR_CLIP_M = 0.1;
const STORE_KEY = "innate.trajOverlay";

/** @param {number} pitchDeg @returns {number} height above the floor in metres */
export function cameraHeight(pitchDeg) {
  const span = CAMERA.MAX_PITCH_DEG - CAMERA.MIN_PITCH_DEG;
  const t = Math.max(0, Math.min(1, (pitchDeg - CAMERA.MIN_PITCH_DEG) / span));
  return CAMERA.HEIGHT_M - CAMERA.PITCH_HEIGHT_COMP_M + t * 2 * CAMERA.PITCH_HEIGHT_COMP_M;
}

/** @param {Array<{ x: number, y: number }>} points
 * @param {{ x: number, y: number, yaw: number }} pose
 * @returns {Array<{ fwd: number, right: number }>} */
export function robotRelative(points, pose) {
  const c = Math.cos(-pose.yaw);
  const s = Math.sin(-pose.yaw);
  return points.map((p) => {
    const dx = p.x - pose.x;
    const dy = p.y - pose.y;
    return { fwd: dx * c - dy * s, right: -(dx * s + dy * c) };
  });
}

/** Every ribbon point is a published pose: the route splits only at the near
 * plane, where projection is undefined, and off-frame points stay so the
 * canvas clip — not point culling — trims the ribbon to the video rect.
 * @param {Array<{ fwd: number, right: number }>} points
 * @param {number} pitchDeg head pitch, positive up
 * @param {number} vw @param {number} vh video frame size in pixels
 * @returns {Array<Array<{ x: number, y: number, depth: number }>>} */
export function projectToImage(points, pitchDeg, vw, vh) {
  const sx = vw / CAMERA.CALIB_W;
  const sy = vh / CAMERA.CALIB_H;
  const fx = CAMERA.FX * sx;
  const fy = CAMERA.FY * sy * CAMERA.FY_SCALE;
  const cx = CAMERA.CX * sx;
  const cy = CAMERA.CY * sy;
  const h = cameraHeight(pitchDeg);
  const pitch = (pitchDeg * Math.PI) / 180;
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  /** @type {Array<Array<{ x: number, y: number, depth: number }>>} */
  const segments = [];
  /** @type {Array<{ x: number, y: number, depth: number }> | null} */
  let seg = null;
  for (const p of points) {
    const rotZ = -h * sp + p.fwd * cp;
    if (rotZ <= NEAR_CLIP_M) {
      seg = null;
      continue;
    }
    const rotY = -h * cp - p.fwd * sp;
    if (!seg) segments.push((seg = []));
    seg.push({ x: fx * (p.right / rotZ) + cx, y: fy * (-rotY / rotZ) + cy, depth: rotZ });
  }
  return segments;
}

/** @param {Array<{ x: number, y: number, depth: number }>} pts
 * @returns {Array<{ x: number, y: number }> | null} */
export function ribbon(pts) {
  if (pts.length < 2) return null;

  /** @param {number} depth */
  const halfWidth = (depth) => Math.max(1, 3 / depth) / 2;

  /**
   * @param {{ x: number, y: number } | null} prev
   * @param {{ x: number, y: number }} curr
   * @param {{ x: number, y: number } | null} next
   */
  const perp = (prev, curr, next) => {
    let dx = 0;
    let dy = 0;
    if (prev && next) {
      dx = next.x - prev.x;
      dy = next.y - prev.y;
    } else if (next) {
      dx = next.x - curr.x;
      dy = next.y - curr.y;
    } else if (prev) {
      dx = curr.x - prev.x;
      dy = curr.y - prev.y;
    }
    const len = Math.hypot(dx, dy) || 1;
    return { nx: -dy / len, ny: dx / len };
  };

  /** @type {Array<{ x: number, y: number }>} */
  const left = [];
  /** @type {Array<{ x: number, y: number }>} */
  const right = [];

  for (let i = 0; i < pts.length; i++) {
    const prev = i > 0 ? pts[i - 1] : null;
    const curr = pts[i];
    const next = i < pts.length - 1 ? pts[i + 1] : null;
    const p = perp(prev, curr, next);
    const hw = halfWidth(curr.depth);
    left.push({ x: curr.x + p.nx * hw, y: curr.y + p.ny * hw });
    right.push({ x: curr.x - p.nx * hw, y: curr.y - p.ny * hw });
  }

  right.reverse();
  return left.concat(right);
}

/**
 * @param {HTMLElement} stage the .video-stage wrap the canvas lives in
 * @param {HTMLVideoElement} video the stage's video element (frame size + fit)
 * @param {HTMLElement} rail right-edge overlay that hosts the toggle
 * @param {import("../rosClient.js").RosClient} ros
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ destroy: () => void }}
 */
export function createTrajectoryOverlay(stage, video, rail, ros, session) {
  const canvas = document.createElement("canvas");
  canvas.className = "traj-canvas";
  stage.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  const button = document.createElement("button");
  button.className = "icon-toggle traj-toggle";
  button.type = "button";
  button.innerHTML =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="6" cy="19" r="2.5"/>' +
    '<path d="M8.5 19h8a3.5 3.5 0 0 0 0-7h-9a3.5 3.5 0 0 1 0-7H15"/>' +
    '<circle cx="18" cy="5" r="2.5"/>' +
    "</svg>";
  rail.appendChild(button);

  /** @type {Array<{ x: number, y: number }> | null} plan points in their own frame */
  let plan = null;
  /** @type {"map" | "odom"} */
  let planFrame = "map";
  /** @type {string | null} only the topic that owns the route may clear it */
  let activePlanTopic = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let staleTimer;

  // Compose map pose from the last AMCL fix plus subsequent odometry.
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let odomPose = null;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let amclPose = null;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let odomAtAmcl = null;

  let pitchDeg = 0;

  function mapPose() {
    if (!amclPose) return odomPose;
    if (!odomAtAmcl || !odomPose) return amclPose;
    const ca = Math.cos(-odomAtAmcl.yaw);
    const sa = Math.sin(-odomAtAmcl.yaw);
    const dxo = odomPose.x - odomAtAmcl.x;
    const dyo = odomPose.y - odomAtAmcl.y;
    const dx = dxo * ca - dyo * sa;
    const dy = dxo * sa + dyo * ca;
    const dyaw = odomPose.yaw - odomAtAmcl.yaw;
    const c = Math.cos(amclPose.yaw);
    const s = Math.sin(amclPose.yaw);
    return { x: amclPose.x + dx * c - dy * s, y: amclPose.y + dx * s + dy * c, yaw: amclPose.yaw + dyaw };
  }

  /** @param {any} msg */
  function poseOf(msg) {
    const p = msg?.pose?.pose;
    const x = p?.position?.x;
    const y = p?.position?.y;
    const q = p?.orientation;
    if (typeof x !== "number" || typeof y !== "number" || !q) return null;
    const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
    if (!Number.isFinite(yaw)) return null;
    return { x, y, yaw };
  }

  let raf = 0;
  function schedule() {
    if (!raf) raf = requestAnimationFrame(draw);
  }

  function clearPlan() {
    plan = null;
    activePlanTopic = null;
    clearTimeout(staleTimer);
    schedule();
  }

  function armStale() {
    clearTimeout(staleTimer);
    staleTimer = setTimeout(clearPlan, NAV_STALE_MS);
  }

  /** @param {string} topic @param {any} msg nav_msgs/Path */
  function onPlan(topic, msg) {
    const poses = msg?.poses;
    if (!Array.isArray(poses)) return;
    const pts = [];
    for (const ps of poses) {
      const pos = ps?.pose?.position;
      if (typeof pos?.x === "number" && typeof pos?.y === "number") pts.push({ x: pos.x, y: pos.y });
    }
    if (!pts.length) {
      // An inactive planner must not clear the displayed route.
      if (!activePlanTopic || topic === activePlanTopic) clearPlan();
      return;
    }
    plan = pts;
    activePlanTopic = topic;
    const frameId = typeof msg?.header?.frame_id === "string" ? msg.header.frame_id : "";
    planFrame = frameId.includes("odom") ? "odom" : "map";
    armStale();
    schedule();
  }

  /** @param {any} msg nav_msgs/Odometry */
  function onOdom(msg) {
    const p = poseOf(msg);
    if (!p) return;
    odomPose = p;
    schedule();
  }

  /** @param {any} msg */
  function onAmcl(msg) {
    const p = poseOf(msg);
    if (!p) return;
    amclPose = p;
    odomAtAmcl = odomPose;
    schedule();
  }

  /** @param {any} payload */
  function onHead(payload) {
    if (typeof payload?.data !== "string") return;
    /** @type {HeadPosition} */
    let parsed;
    try {
      parsed = JSON.parse(payload.data);
    } catch {
      return;
    }
    if (typeof parsed.current_position !== "number") return;
    pitchDeg = parsed.current_position;
    schedule();
  }

  function draw() {
    raf = 0;
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!plan || !vw || !vh || session.primaryCamera.name !== "main") return;
    const pose = planFrame === "odom" ? odomPose : mapPose();
    if (!pose) return;

    const segments = projectToImage(robotRelative(plan, pose), pitchDeg, vw, vh);
    /** @type {Array<Array<{ x: number, y: number }>>} */
    const polys = [];
    for (const seg of segments) {
      const poly = ribbon(seg);
      if (poly) polys.push(poly);
    }
    if (!polys.length) return;

    // Map image pixels onto the video's object-fit: contain rectangle.
    const cw = stage.clientWidth;
    const ch = stage.clientHeight;
    if (!cw || !ch) return;
    // Derive DPR from the backing store so monitor moves cannot desync it.
    const dpr = canvas.width / cw;
    const fit = Math.min(cw / vw, ch / vh);
    const offX = (cw - vw * fit) / 2;
    const offY = (ch - vh * fit) / 2;
    ctx.setTransform(dpr * fit, 0, 0, dpr * fit, dpr * offX, dpr * offY);

    // Off-frame poses survive projection; the clip is what trims the ribbon.
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, vw, vh);
    ctx.clip();
    ctx.fillStyle = RIBBON_FILL;
    for (const poly of polys) {
      ctx.beginPath();
      ctx.moveTo(poly[0].x, poly[0].y);
      for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i].x, poly[i].y);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  const resize = new ResizeObserver(() => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(stage.clientWidth * dpr);
    canvas.height = Math.round(stage.clientHeight * dpr);
    schedule();
  });
  resize.observe(stage);
  video.addEventListener("resize", schedule);
  const unsubSession = session.onChange(schedule);

  /** @type {Array<() => void>} */
  let unsubs = [];
  let enabled = true;
  try {
    enabled = localStorage.getItem(STORE_KEY) !== "0";
  } catch {
    // Default on when storage is unavailable.
  }

  function apply() {
    button.classList.toggle("active", enabled);
    button.title = enabled
      ? `Trajectory overlay on — planned route on the camera (${PLAN_TOPICS.join(" · ")})`
      : "Trajectory overlay off — click to project the planned route onto the camera";
    button.setAttribute("aria-pressed", String(enabled));
    button.setAttribute("aria-label", "Trajectory overlay");
    if (enabled && !unsubs.length) {
      unsubs = [
        ...PLAN_TOPICS.map((t) => ros.subscribe(t, (msg) => onPlan(t, msg), 250, "nav_msgs/msg/Path")),
        ros.subscribe(ODOM_TOPIC, onOdom, 100, "nav_msgs/msg/Odometry"),
        ros.subscribe(AMCL_POSE_TOPIC, onAmcl, 0, "geometry_msgs/msg/PoseWithCovarianceStamped"),
        ros.subscribe(HEAD_CURRENT_POSITION_TOPIC, onHead, undefined, "std_msgs/msg/String"),
      ];
    } else if (!enabled) {
      for (const unsub of unsubs) unsub();
      unsubs = [];
      clearPlan();
    }
  }
  apply();

  button.addEventListener("click", () => {
    enabled = !enabled;
    try {
      localStorage.setItem(STORE_KEY, enabled ? "1" : "0");
    } catch {
      // The toggle still applies when storage is unavailable.
    }
    apply();
  });

  return {
    destroy() {
      for (const unsub of unsubs) unsub();
      unsubSession();
      resize.disconnect();
      video.removeEventListener("resize", schedule);
      clearTimeout(staleTimer);
      cancelAnimationFrame(raf);
      canvas.remove();
      button.remove();
    },
  };
}
