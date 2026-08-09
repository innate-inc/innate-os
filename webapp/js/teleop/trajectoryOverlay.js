// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Trajectory-on-camera overlay — the planner's route projected onto the main
// camera feed as a green ground ribbon, a port of the controller app's
// TrajectoryOverlay. Plan points arrive in the map or odom frame; each draw
// re-expresses them relative to the robot's current pose, drops them onto the
// floor plane, and runs them through the head camera's calibrated pinhole
// model. A right-rail toggle (persisted) turns the whole thing off, dropping
// every subscription with it.

import {
  PLAN_TOPICS,
  ODOM_TOPIC,
  AMCL_POSE_TOPIC,
  HEAD_CURRENT_POSITION_TOPIC,
} from "../constants.js";

// Head-camera model from the controller app's OpenCV calibration at 320x240.
// The camera sits ~0.28 m off the floor and slides a little as the head
// pitches; FY_SCALE is the app's empirical vertical-FOV fudge — keep them in
// sync with innate-controller-app's TrajectoryOverlay or the two UIs drift.
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
// The planner republishes ~1 Hz while driving and stops on arrival, so a lull
// means navigation ended (same rule as the map widget's route).
const NAV_STALE_MS = 4000;
const STORE_KEY = "innate.trajOverlay";

/** @param {number} pitchDeg @returns {number} camera height above the floor, metres */
export function cameraHeight(pitchDeg) {
  const span = CAMERA.MAX_PITCH_DEG - CAMERA.MIN_PITCH_DEG;
  const t = Math.max(0, Math.min(1, (pitchDeg - CAMERA.MIN_PITCH_DEG) / span));
  return CAMERA.HEIGHT_M - CAMERA.PITCH_HEIGHT_COMP_M + t * 2 * CAMERA.PITCH_HEIGHT_COMP_M;
}

/**
 * Re-express world-frame plan points relative to the robot: fwd along its
 * heading, right positive (camera x), both in metres on the floor.
 * @param {Array<{ x: number, y: number }>} points
 * @param {{ x: number, y: number, yaw: number }} pose robot pose in the same frame
 * @returns {Array<{ fwd: number, right: number }>}
 */
export function robotRelative(points, pose) {
  const c = Math.cos(-pose.yaw);
  const s = Math.sin(-pose.yaw);
  return points.map((p) => {
    const dx = p.x - pose.x;
    const dy = p.y - pose.y;
    return { fwd: dx * c - dy * s, right: -(dx * s + dy * c) };
  });
}

/**
 * Pinhole-project robot-relative floor points into image pixels, culling
 * anything behind the camera or outside the frame.
 * @param {Array<{ fwd: number, right: number }>} points
 * @param {number} pitchDeg head pitch, positive up
 * @param {number} vw @param {number} vh video frame size in pixels
 * @returns {Array<{ x: number, y: number, depth: number }>}
 */
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
  const out = [];
  for (const p of points) {
    const rotY = -h * cp - p.fwd * sp;
    const rotZ = -h * sp + p.fwd * cp;
    if (rotZ <= 0.1) continue;
    const u = fx * (p.right / rotZ) + cx;
    const v = fy * (-rotY / rotZ) + cy;
    if (u < 0 || u > vw || v < 0 || v > vh) continue;
    out.push({ x: u, y: v, depth: rotZ });
  }
  return out;
}

/**
 * Build the ribbon polygon: the projected centreline extended down to the
 * bottom edge (so it starts at the robot's feet), widened perpendicular to
 * its direction, tapering with depth.
 * @param {Array<{ x: number, y: number, depth: number }>} pts
 * @param {number} bottomY
 * @returns {Array<{ x: number, y: number }> | null}
 */
export function ribbon(pts, bottomY) {
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

  const p0 = pts[0];
  const p1 = pts[1];
  let startX = p0.x;
  let startY = p0.y;
  const dy0 = p1.y - p0.y;
  if (dy0 !== 0 && p0.y < bottomY) {
    const t = (bottomY - p0.y) / dy0;
    startX = p0.x + t * (p1.x - p0.x);
    startY = bottomY;
  }
  const start = { x: startX, y: startY };
  const sp = perp(null, start, p0);
  const shw = halfWidth(p0.depth);
  left.push({ x: startX + sp.nx * shw, y: startY + sp.ny * shw });
  right.push({ x: startX - sp.nx * shw, y: startY - sp.ny * shw });

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

  // Same pose composition as the map widget: anchor at the last /amcl_pose fix
  // and add the odometry delta since (raw /odom is off by the whole map->odom
  // correction on a real robot). No AMCL yet -> raw odom is the map frame.
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let odomPose = null;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let amclPose = null;
  /** @type {{ x: number, y: number, yaw: number } | null} odom pose at the AMCL fix */
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

  /** @param {any} msg pose-carrying message → {x, y, yaw} or null */
  function poseOf(msg) {
    const p = msg?.pose?.pose;
    const x = p?.position?.x;
    const y = p?.position?.y;
    const q = p?.orientation;
    if (typeof x !== "number" || typeof y !== "number" || !q) return null;
    return { x, y, yaw: Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) };
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
      // Empty path = navigation finished/aborted — but only from the planner
      // that owns the displayed route; the inactive one must not clear it.
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

  /** @param {any} msg geometry_msgs/PoseWithCovarianceStamped (map frame) */
  function onAmcl(msg) {
    const p = poseOf(msg);
    if (!p) return;
    amclPose = p;
    odomAtAmcl = odomPose;
    schedule();
  }

  /** @param {any} payload std_msgs/String carrying a HeadPosition JSON */
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

    const projected = projectToImage(robotRelative(plan, pose), pitchDeg, vw, vh);
    const poly = ribbon(projected, vh);
    if (!poly) return;

    // Draw in image pixels; the transform maps them onto the video's
    // object-fit: contain rectangle inside the stage.
    const cw = stage.clientWidth;
    const ch = stage.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    const fit = Math.min(cw / vw, ch / vh);
    const offX = (cw - vw * fit) / 2;
    const offY = (ch - vh * fit) / 2;
    ctx.setTransform(dpr * fit, 0, 0, dpr * fit, dpr * offX, dpr * offY);

    ctx.beginPath();
    ctx.moveTo(poly[0].x, poly[0].y);
    for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i].x, poly[i].y);
    ctx.closePath();
    ctx.fillStyle = RIBBON_FILL;
    ctx.fill();
  }

  const resize = new ResizeObserver(() => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(stage.clientWidth * dpr);
    canvas.height = Math.round(stage.clientHeight * dpr);
    schedule();
  });
  resize.observe(stage);
  video.addEventListener("resize", schedule); // videoWidth/Height became known
  const unsubSession = session.onChange(schedule); // primary-camera swaps

  /** @type {Array<() => void>} */
  let unsubs = [];
  let enabled = localStorage.getItem(STORE_KEY) !== "0";

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
    localStorage.setItem(STORE_KEY, enabled ? "1" : "0");
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
