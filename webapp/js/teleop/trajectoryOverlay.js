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

// The sim renders the head camera as an ideal pinhole at the driver's vertical
// FOV (mars_sim_driver/constants.py CAMERA_FOVY, mirrored by sim/viewer's
// ROBOT_CAMERA_VFOV), so none of the real lens's calibration applies to it.
const SIM_VFOV_DEG = 68.5;
// mars.urdf puts the head camera on the head pivot itself, 0.2585 m above
// base_link — which is the ground-plane frame. Sitting on the pivot, it rises
// by 0.3 mm across the pitch range, so the empirical swing above is not its.
const SIM_HEIGHT_M = 0.2585;
// The head camera streams 640-wide on hardware, which is the image space the
// ribbon's width constants are tuned in — the sim borrows it as a nominal frame.
const SIM_FRAME_W = 640;

/** @typedef {{ fx: number, fy: number, cx: number, cy: number }} Lens pixel intrinsics */
/** @typedef {{ lens: (vw: number, vh: number) => Lens, height: (pitchDeg: number) => number }} CameraModel */

/** The physical head camera: calibrated at 320x240, height measured on the robot. */
export const REAL_CAMERA = {
  lens: (/** @type {number} */ vw, /** @type {number} */ vh) => {
    const sx = vw / CAMERA.CALIB_W;
    const sy = vh / CAMERA.CALIB_H;
    return {
      fx: CAMERA.FX * sx,
      fy: CAMERA.FY * sy * CAMERA.FY_SCALE,
      cx: CAMERA.CX * sx,
      cy: CAMERA.CY * sy,
    };
  },
  height: cameraHeight,
};

/** The same camera as the sim renders it: both halves are exact, so neither
 * the lens calibration nor the pitch-height fudge carries over. */
export const SIM_CAMERA = {
  // Three.js fixes the vertical FOV and widens horizontally with the aspect,
  // which is square pixels — one focal length for both axes.
  lens: (/** @type {number} */ vw, /** @type {number} */ vh) => {
    const f = vh / 2 / Math.tan((SIM_VFOV_DEG * Math.PI) / 360);
    return { fx: f, fy: f, cx: vw / 2, cy: vh / 2 };
  },
  height: () => SIM_HEIGHT_M,
};

const RIBBON_FILL = "rgba(0, 255, 136, 0.85)";
// The planner republishes while driving and stops on arrival.
const NAV_STALE_MS = 4000;
// A route starting farther than this from the robot is not connected to its feet.
const ANCHOR_NEAR_M = 0.4;
// Nearer than this the ground point is level with or behind the lens and has no
// image position at all — the one case that genuinely breaks the route.
const NEAR_PLANE_M = 0.1;
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

/** @typedef {{ x: number, y: number, depth: number }} ImagePoint */

/** Liang-Barsky: the sub-range of the projected edge a->b that lies inside the
 * frame, or null when none of it does.
 * @param {ImagePoint} a @param {ImagePoint} b
 * @param {number} vw @param {number} vh
 * @returns {[number, number] | null} */
function visibleRange(a, b, vw, vh) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const toward = [-dx, dx, -dy, dy];
  const slack = [a.x, vw - a.x, a.y, vh - a.y];
  let t0 = 0;
  let t1 = 1;
  for (let i = 0; i < 4; i++) {
    if (toward[i] === 0) {
      if (slack[i] < 0) return null; // runs parallel to this side, outside it
      continue;
    }
    const t = slack[i] / toward[i];
    if (toward[i] < 0) {
      if (t > t1) return null;
      if (t > t0) t0 = t;
    } else {
      if (t < t0) return null;
      if (t < t1) t1 = t;
    }
  }
  return [t0, t1];
}

/** @param {ImagePoint} a @param {ImagePoint} b @param {number} t @returns {ImagePoint} */
function along(a, b, t) {
  if (t === 0) return a;
  if (t === 1) return b;
  return {
    x: a.x + (b.x - a.x) * t,
    y: a.y + (b.y - a.y) * t,
    // Depth is not linear along a projected edge, but its reciprocal is.
    depth: 1 / (1 / a.depth + (1 / b.depth - 1 / a.depth) * t),
  };
}

/** Clips the route to the frame edge by edge, so a stretch that crosses the
 * view is drawn even when the poses bracketing it both fall outside. Only a
 * genuine break — leaving the frame, or a pose behind the lens — splits the
 * run, so disjoint visible stretches are still never bridged.
 * startAtRobot: segments[0] itself begins at the first path pose and that pose
 * lies at the robot, so it may extend to the feet without fabricating route.
 * @param {Array<{ fwd: number, right: number }>} points
 * @param {number} pitchDeg head pitch, positive up
 * @param {number} vw @param {number} vh video frame size in pixels
 * @param {CameraModel} [camera] defaults to the physical head camera
 * @returns {{ segments: Array<Array<ImagePoint>>, startAtRobot: boolean }} */
export function projectToImage(points, pitchDeg, vw, vh, camera = REAL_CAMERA) {
  const { fx, fy, cx, cy } = camera.lens(vw, vh);
  const h = camera.height(pitchDeg);
  const pitch = (pitchDeg * Math.PI) / 180;
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);

  /** @type {Array<ImagePoint | null>} */
  const seen = points.map((p) => {
    const rotY = -h * cp - p.fwd * sp;
    const rotZ = -h * sp + p.fwd * cp;
    if (rotZ <= NEAR_PLANE_M) return null;
    return { x: fx * (p.right / rotZ) + cx, y: fy * (-rotY / rotZ) + cy, depth: rotZ };
  });

  const head = points[0];
  const nearRobot = !!head && Math.hypot(head.fwd, head.right) <= ANCHOR_NEAR_M;

  /** @type {Array<Array<ImagePoint>>} */
  const segments = [];
  /** @type {Array<ImagePoint> | null} */
  let seg = null;
  let startAtRobot = false;
  for (let i = 0; i + 1 < seen.length; i++) {
    const a = seen[i];
    const b = seen[i + 1];
    if (!a || !b) {
      seg = null; // a pose behind the lens has no image position to clip against
      continue;
    }
    const range = visibleRange(a, b, vw, vh);
    if (!range) {
      seg = null;
      continue;
    }
    const [enter, exit] = range;
    // enter > 0 means this edge crossed in from outside, so it starts a run
    // rather than continuing the previous one.
    if (!seg || enter > 0) {
      segments.push((seg = [along(a, b, enter)]));
      // Earned by this run, not by its index: enter === 0 on the very first
      // edge is what puts the first pose at the head of segments[0]. Drop that
      // edge — the next pose is behind the lens — and a later run takes index 0
      // without touching the robot, so it must not inherit the anchor.
      if (i === 0 && enter === 0) startAtRobot = nearRobot;
    }
    seg.push(along(a, b, exit));
    if (exit < 1) seg = null; // the route leaves the frame here
  }
  return { segments, startAtRobot };
}

/** Only a segment whose route truly starts at the robot may anchor to its feet.
 * @param {Array<{ x: number, y: number, depth: number }>} pts
 * @param {number} vw frame width, bounds the anchor extrapolation
 * @param {number} bottomY
 * @param {boolean} [anchorBottom]
 * @returns {Array<{ x: number, y: number }> | null} */
export function ribbon(pts, vw, bottomY, anchorBottom = true) {
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

  if (anchorBottom) {
    const p0 = pts[0];
    const p1 = pts[1];
    let startX = p0.x;
    let startY = p0.y;
    const dy0 = p1.y - p0.y;
    if (dy0 !== 0 && p0.y < bottomY) {
      const t = (bottomY - p0.y) / dy0;
      // Clamp near-horizontal extrapolation.
      startX = Math.min(2 * vw, Math.max(-vw, p0.x + t * (p1.x - p0.x)));
      startY = bottomY;
    }
    const start = { x: startX, y: startY };
    const sp = perp(null, start, p0);
    const shw = halfWidth(p0.depth);
    left.push({ x: startX + sp.nx * shw, y: startY + sp.ny * shw });
    right.push({ x: startX - sp.nx * shw, y: startY - sp.ny * shw });
  }

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

/** SimSession names the primary camera with a bare string, WebRtcSession with
 * an {index, name} pair — both call the head camera "main".
 * @param {any} session @returns {string | undefined} */
function primaryCameraName(session) {
  const cam = session.primaryCamera;
  return typeof cam === "string" ? cam : cam?.name;
}

/**
 * @param {HTMLElement} stage the .video-stage wrap the canvas lives in
 * @param {HTMLVideoElement | null} video the stage's video element (frame size
 *   + fit) on hardware; null in sim, where a Three.js canvas renders the same
 *   head camera at the stage's own size
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

    // The sim's canvas has no frame size of its own — it renders at whatever
    // the stage is. Project into a nominal frame the size hardware streams and
    // let the fit below scale it up, so the ribbon's pixel-tuned width lands on
    // screen at the same thickness under both stages. Matching the stage's
    // aspect keeps the sim's fixed vertical FOV and widening horizontal one.
    const vw = video ? video.videoWidth : SIM_FRAME_W;
    const vh = video
      ? video.videoHeight
      : Math.round((SIM_FRAME_W * stage.clientHeight) / (stage.clientWidth || 1));
    if (!plan || !vw || !vh || primaryCameraName(session) !== "main") return;
    const pose = planFrame === "odom" ? odomPose : mapPose();
    if (!pose) return;

    const camera = video ? REAL_CAMERA : SIM_CAMERA;
    const { segments, startAtRobot } = projectToImage(robotRelative(plan, pose), pitchDeg, vw, vh, camera);
    /** @type {Array<Array<{ x: number, y: number }>>} */
    const polys = [];
    for (let i = 0; i < segments.length; i++) {
      const poly = ribbon(segments[i], vw, vh, i === 0 && startAtRobot);
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

    ctx.fillStyle = RIBBON_FILL;
    for (const poly of polys) {
      ctx.beginPath();
      ctx.moveTo(poly[0].x, poly[0].y);
      for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i].x, poly[i].y);
      ctx.closePath();
      ctx.fill();
    }
  }

  const resize = new ResizeObserver(() => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(stage.clientWidth * dpr);
    canvas.height = Math.round(stage.clientHeight * dpr);
    schedule();
  });
  resize.observe(stage);
  // The sim has no stream to change shape — its size follows the stage, which
  // the ResizeObserver above already watches.
  video?.addEventListener("resize", schedule);
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
      video?.removeEventListener("resize", schedule);
      clearTimeout(staleTimer);
      cancelAnimationFrame(raf);
      canvas.remove();
      button.remove();
    },
  };
}
