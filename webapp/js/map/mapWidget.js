// @ts-check
// Reusable nav-map widget — a plain 2D <canvas> rendering the occupancy grid
// (/map), robot pose (/odom), planner route (/plan), and click-to-navigate
// (publishes /goal_pose). Sizes itself to its container via a ResizeObserver, so
// it can be the standalone Map page OR a teleop PiP tile that reparents between
// a small thumbnail and the full stage. No three.js — a canvas + putImageData
// is all a 2D map needs.

import { ros } from "../rosClient.js";
import { MAP_TOPIC, ODOM_TOPIC, PLAN_TOPICS, CMD_VEL_RAW_TOPIC, GOAL_POSE_TOPIC, CANCEL_NAVIGATION_SERVICE } from "../constants.js";

// Wheel-zoom bounds (metres of real-world width shown).
const MIN_ZOOM_M = 1;
const MAX_ZOOM_M = 60;
const ZOOM_STEP = 1.15; // per wheel notch

/**
 * @param {HTMLElement} root container the map fills (sized via ResizeObserver).
 * @param {{ zoom?: number, onZoomChange?: (meters: number) => void }} [opts] zoom = metres of real-world
 *   width to show, centred on the robot pose (keeps the map legible when small); enables scroll-to-zoom.
 *   Omit to fit the whole grid (the standalone page). onZoomChange fires after each wheel-zoom.
 * @returns {{ destroy: () => void, setZoom: (meters: number) => void }}
 */
export function createMap(root, opts = {}) {
  let zoomMeters = opts.zoom;
  const canvas = document.createElement("canvas");
  canvas.className = "map-canvas";
  root.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  const goalBtn = document.createElement("button");
  goalBtn.className = "map-goal-btn sim-button";
  goalBtn.textContent = "Set Goal";
  const stopBtn = document.createElement("button");
  stopBtn.className = "map-stop-btn sim-button";
  stopBtn.textContent = "Stop";
  const controls = document.createElement("div");
  controls.className = "map-controls";
  controls.appendChild(goalBtn);
  controls.appendChild(stopBtn);
  root.appendChild(controls);

  // Offscreen 1px-per-cell buffer; scaled to the canvas on draw (crisp + cheap).
  const off = document.createElement("canvas");
  const offCtx = off.getContext("2d");

  /** @type {{ width: number, height: number, resolution: number, originX: number, originY: number } | null} */
  let grid = null;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let pose = null;
  /** @type {Array<{ x: number, y: number }> | null} world-frame plan points */
  let plan = null;
  /** @type {{ vx: number, wz: number } | null} latest follower command (body frame) */
  let cmd = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} clears the arrow when the controller goes quiet */
  let cmdStaleTimer;

  // Last draw's grid→canvas placement, so pointer handlers can invert it.
  /** @type {{ ox: number, oy: number, scale: number } | null} */
  let view = null;

  // Goal-setting: click sets the position, drag sets the heading.
  let goalMode = false;
  /** @type {{ start: { x: number, y: number }, cur: { x: number, y: number } } | null} */
  let goalDrag = null;
  /** @type {{ x: number, y: number, yaw: number } | null} the active goal */
  let goalMarker = null;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let navStaleTimer;

  const dpr = () => window.devicePixelRatio || 1;

  function fit() {
    const r = root.getBoundingClientRect();
    const d = dpr();
    canvas.width = Math.max(1, Math.floor(r.width * d));
    canvas.height = Math.max(1, Math.floor(r.height * d));
    canvas.style.width = `${r.width}px`;
    canvas.style.height = `${r.height}px`;
    draw();
  }

  /** @param {number} x @param {number} y world metres → canvas pixels */
  function worldToCanvas(x, y) {
    const g = /** @type {NonNullable<typeof grid>} */ (grid);
    const v = /** @type {NonNullable<typeof view>} */ (view);
    const col = (x - g.originX) / g.resolution;
    const rowFromBottom = (y - g.originY) / g.resolution;
    return { px: v.ox + col * v.scale, py: v.oy + (g.height - rowFromBottom) * v.scale };
  }

  /** @param {number} px @param {number} py canvas pixels → world metres */
  function canvasToWorld(px, py) {
    const g = /** @type {NonNullable<typeof grid>} */ (grid);
    const v = /** @type {NonNullable<typeof view>} */ (view);
    const col = (px - v.ox) / v.scale;
    const rowFromBottom = g.height - (py - v.oy) / v.scale;
    return { x: g.originX + col * g.resolution, y: g.originY + rowFromBottom * g.resolution };
  }

  /** @param {PointerEvent} e → canvas-pixel coords */
  function eventToCanvas(e) {
    const rect = canvas.getBoundingClientRect();
    const d = dpr();
    return { px: (e.clientX - rect.left) * d, py: (e.clientY - rect.top) * d };
  }

  /** @param {any} msg nav_msgs/OccupancyGrid */
  function onMap(msg) {
    const info = msg?.info;
    const data = msg?.data;
    const width = info?.width | 0;
    const height = info?.height | 0;
    if (!info || !Array.isArray(data) || width <= 0 || height <= 0 || data.length < width * height) return;
    off.width = width;
    off.height = height;
    if (!offCtx) return;
    const img = offCtx.createImageData(width, height);
    for (let row = 0; row < height; row++) {
      const srcRow = height - 1 - row; // flip so canvas-top = highest world-y
      for (let col = 0; col < width; col++) {
        const v = data[srcRow * width + col];
        const di = (row * width + col) * 4;
        let shade;
        let a = 255;
        if (v < 0) {
          shade = 105; // unknown
          a = 200;
        } else {
          shade = 255 - Math.round((Math.max(0, Math.min(100, v)) / 100) * 255);
        }
        img.data[di] = shade;
        img.data[di + 1] = shade;
        img.data[di + 2] = shade;
        img.data[di + 3] = a;
      }
    }
    offCtx.putImageData(img, 0, 0);
    grid = {
      width,
      height,
      resolution: info.resolution || 0.05,
      originX: info.origin?.position?.x ?? 0,
      originY: info.origin?.position?.y ?? 0,
    };
    draw();
  }

  /** @param {any} msg nav_msgs/Odometry */
  function onOdom(msg) {
    const p = msg?.pose?.pose;
    const x = p?.position?.x;
    const y = p?.position?.y;
    const q = p?.orientation;
    if (typeof x !== "number" || typeof y !== "number" || !q) return;
    const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
    pose = { x, y, yaw };
    draw();
  }

  // The planner republishes /plan (~1 Hz) the whole time it's driving to a goal,
  // and stops once it arrives. So a lull in /plan means navigation has ended —
  // that's when we drop the goal marker and the route, rather than on a timer.
  const NAV_STALE_MS = 4000;

  function armNavStale() {
    clearTimeout(navStaleTimer);
    navStaleTimer = setTimeout(() => {
      goalMarker = null;
      plan = null;
      draw();
    }, NAV_STALE_MS);
  }

  /** @param {any} msg nav_msgs/Path */
  function onPlan(msg) {
    const poses = msg?.poses;
    if (!Array.isArray(poses)) return;
    const pts = [];
    for (const ps of poses) {
      const pos = ps?.pose?.position;
      if (typeof pos?.x === "number" && typeof pos?.y === "number") pts.push({ x: pos.x, y: pos.y });
    }
    if (pts.length) {
      plan = pts;
      armNavStale(); // route still streaming → keep the goal visible
    } else {
      plan = null; // empty path = navigation finished/aborted
      goalMarker = null;
      clearTimeout(navStaleTimer);
    }
    draw();
  }

  /** @param {any} msg geometry_msgs/Twist — the follower's commanded velocity */
  function onCmd(msg) {
    const vx = msg?.linear?.x;
    const wz = msg?.angular?.z;
    if (typeof vx !== "number" || typeof wz !== "number") return;
    cmd = { vx, wz };
    // /cmd_vel_raw is published only while a controller is active; a lull means
    // it stopped, so drop the arrow after a short quiet period.
    clearTimeout(cmdStaleTimer);
    cmdStaleTimer = setTimeout(() => {
      cmd = null;
      draw();
    }, 500);
    draw();
  }

  function draw() {
    if (!ctx) return;
    ctx.fillStyle = "#0a0a0c";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (!grid) {
      ctx.fillStyle = "#8a8a93";
      ctx.font = `${14 * dpr()}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.fillText("waiting for /map…", canvas.width / 2, canvas.height / 2);
      return;
    }

    let scale, ox, oy;
    if (zoomMeters && pose) {
      // Robot-centred zoom: show a fixed real-world window (zoomMeters across) centred on the pose, so the
      // map stays legible at thumbnail size instead of fitting the whole world into a few pixels. Anything
      // outside the window is simply clipped by the canvas bounds.
      const cellsAcross = zoomMeters / grid.resolution;
      scale = Math.min(canvas.width, canvas.height) / cellsAcross;
      const poseCol = (pose.x - grid.originX) / grid.resolution;
      const poseRowFromBottom = (pose.y - grid.originY) / grid.resolution;
      ox = canvas.width / 2 - poseCol * scale;
      oy = canvas.height / 2 - (grid.height - poseRowFromBottom) * scale;
    } else {
      // Fit the whole grid (standalone page, or before the first pose arrives).
      const pad = 16 * dpr();
      scale = Math.min((canvas.width - 2 * pad) / grid.width, (canvas.height - 2 * pad) / grid.height);
      ox = (canvas.width - grid.width * scale) / 2;
      oy = (canvas.height - grid.height * scale) / 2;
    }
    view = { ox, oy, scale };
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, ox, oy, grid.width * scale, grid.height * scale);

    if (plan && plan.length >= 2) {
      ctx.strokeStyle = "#00b7ff";
      ctx.lineWidth = 2 * dpr();
      ctx.lineJoin = "round";
      ctx.beginPath();
      plan.forEach((p, i) => {
        const { px, py } = worldToCanvas(p.x, p.y);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    // Goal: a green dot, plus a heading arrow while dragging.
    const goalAt = goalDrag ? { x: goalDrag.start.x, y: goalDrag.start.y } : goalMarker;
    if (goalAt) {
      const { px, py } = worldToCanvas(goalAt.x, goalAt.y);
      const r = Math.max(4, 6 * dpr());
      ctx.fillStyle = "#00ff88";
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      const yaw = goalDrag ? Math.atan2(goalDrag.cur.y - goalDrag.start.y, goalDrag.cur.x - goalDrag.start.x) : goalMarker?.yaw;
      if (typeof yaw === "number") {
        ctx.strokeStyle = "#00ff88";
        ctx.lineWidth = 2 * dpr();
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(px + Math.cos(yaw) * r * 2.4, py - Math.sin(yaw) * r * 2.4);
        ctx.stroke();
      }
    }

    if (pose) {
      const { px, py } = worldToCanvas(pose.x, pose.y);
      const rad = Math.max(4, 6 * dpr());
      ctx.fillStyle = "#e8a33d";
      ctx.beginPath();
      ctx.arc(px, py, rad, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#e8a33d";
      ctx.lineWidth = 2 * dpr();
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px + Math.cos(pose.yaw) * rad * 2.4, py - Math.sin(pose.yaw) * rad * 2.4);
      ctx.stroke();

      // Follower command: an arrow from the robot showing the commanded linear
      // velocity (green forward, red reverse), length ~1.5 s of travel. A
      // near-zero or flipping arrow is the visible signature of a stuck
      // controller. Canvas y is flipped vs world y, hence -sin.
      if (cmd && Math.abs(cmd.vx) > 0.01) {
        const pxPerM = view.scale / grid.resolution;
        const len = Math.max(-1.2, Math.min(1.2, cmd.vx * 1.5)) * pxPerM;
        const dir = cmd.vx >= 0 ? pose.yaw : pose.yaw + Math.PI;
        const ex = px + Math.cos(dir) * Math.abs(len);
        const ey = py - Math.sin(dir) * Math.abs(len);
        ctx.strokeStyle = cmd.vx >= 0 ? "#33ff88" : "#ff5544";
        ctx.lineWidth = 3 * dpr();
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(ex, ey);
        ctx.stroke();
        // Arrowhead.
        const head = 6 * dpr();
        ctx.beginPath();
        ctx.moveTo(ex, ey);
        ctx.lineTo(ex - Math.cos(dir - 0.4) * head, ey + Math.sin(dir - 0.4) * head);
        ctx.moveTo(ex, ey);
        ctx.lineTo(ex - Math.cos(dir + 0.4) * head, ey + Math.sin(dir + 0.4) * head);
        ctx.stroke();
        ctx.lineCap = "butt";
      }
    }
  }

  /** @param {boolean} on */
  function setGoalMode(on) {
    goalMode = on;
    goalBtn.classList.toggle("is-active", on);
    goalBtn.textContent = on ? "Click map…" : "Set Goal";
    canvas.style.cursor = on ? "crosshair" : "";
    if (!on) goalDrag = null;
  }

  /** @param {number} x @param {number} y @param {number} yaw */
  function publishGoal(x, y, yaw) {
    const qz = Math.sin(yaw / 2);
    const qw = Math.cos(yaw / 2);
    const now = Date.now();
    ros.publish(GOAL_POSE_TOPIC, {
      header: { stamp: { sec: Math.floor(now / 1000), nanosec: (now % 1000) * 1_000_000 }, frame_id: "map" },
      pose: { position: { x, y, z: 0 }, orientation: { x: 0, y: 0, z: qz, w: qw } },
    });
    plan = null; // drop the stale route; the new one streams in on /plan
    goalMarker = { x, y, yaw };
    armNavStale(); // hold the goal until the route starts, then while it runs
  }

  /** @param {PointerEvent} e */
  function onPointerDown(e) {
    if (!goalMode || !grid || !view) return;
    e.preventDefault();
    const { px, py } = eventToCanvas(e);
    const w = canvasToWorld(px, py);
    goalDrag = { start: w, cur: w };
    canvas.setPointerCapture(e.pointerId);
    draw();
  }

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (!goalDrag) return;
    const { px, py } = eventToCanvas(e);
    goalDrag.cur = canvasToWorld(px, py);
    draw();
  }

  /** @param {PointerEvent} e */
  function onPointerUp(e) {
    if (!goalDrag) return;
    const { start, cur } = goalDrag;
    goalDrag = null;
    const dx = cur.x - start.x;
    const dy = cur.y - start.y;
    // Short drag → no meaningful heading, just face "east".
    const yaw = Math.hypot(dx, dy) > 0.1 ? Math.atan2(dy, dx) : 0;
    publishGoal(start.x, start.y, yaw);
    setGoalMode(false);
    draw();
  }

  // Scroll to zoom (only in robot-centred mode). Scroll up = zoom in = show fewer metres.
  /** @param {WheelEvent} e */
  function onWheel(e) {
    if (!zoomMeters) return; // fit-whole mode (standalone page) doesn't zoom
    e.preventDefault();
    const next = zoomMeters * (e.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
    zoomMeters = Math.min(MAX_ZOOM_M, Math.max(MIN_ZOOM_M, next));
    draw();
    opts.onZoomChange?.(zoomMeters);
  }

  goalBtn.addEventListener("click", () => setGoalMode(!goalMode));

  // Stop cancels every active navigation goal server-side, then drops the
  // local goal marker and route.
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    stopBtn.textContent = "Stopping…";
    try {
      await ros.callService(CANCEL_NAVIGATION_SERVICE, {});
      goalMarker = null;
      plan = null;
      setGoalMode(false);
      draw();
      stopBtn.textContent = "Stopped";
    } catch (err) {
      console.error("[map] cancel navigation failed:", err);
      stopBtn.textContent = "Stop failed";
    } finally {
      stopBtn.disabled = false;
      setTimeout(() => {
        stopBtn.textContent = "Stop";
      }, 1500);
    }
  });
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });

  // Resize with the container, not just the window — covers reparenting between
  // the small PiP tile and the full stage in teleop.
  const ro = new ResizeObserver(() => fit());
  ro.observe(root);
  fit();

  const unsubMap = ros.subscribe(MAP_TOPIC, onMap, 250);
  const unsubOdom = ros.subscribe(ODOM_TOPIC, onOdom, 100);
  // Only the active planner publishes, so both feeds can share one handler.
  const unsubPlans = PLAN_TOPICS.map((topic) => ros.subscribe(topic, onPlan, 250, "nav_msgs/msg/Path"));
  const unsubCmd = ros.subscribe(CMD_VEL_RAW_TOPIC, onCmd, 100, "geometry_msgs/msg/Twist");

  return {
    /** Swap to a saved zoom (e.g. when this widget reparents between thumbnail and full stage). */
    setZoom(meters) {
      if (typeof meters === "number" && meters > 0 && meters !== zoomMeters) {
        zoomMeters = meters;
        draw();
      }
    },
    destroy() {
      clearTimeout(navStaleTimer);
      clearTimeout(cmdStaleTimer);
      ro.disconnect();
      unsubMap();
      unsubOdom();
      for (const unsub of unsubPlans) unsub();
      unsubCmd();
      canvas.remove();
      controls.remove();
    },
  };
}
