// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The policy's output, drawn where you can judge it: a top-down view of the
// robot and the 8 waypoints it just predicted.
//
// Odom frame, robot-centred and robot-up, so the picture reads the way the
// robot sees it — the path bending left on screen is the robot turning left.
// The waypoints arrive in odom (the node re-anchors them before publishing),
// so the only transform here is world -> screen.

const RANGE_M = 3.0;        // half-extent drawn around the robot
const RING_M = 1.0;         // distance rings

export function createWaypointView(parent) {
  const canvas = document.createElement("canvas");
  canvas.className = "policy-canvas";
  parent.appendChild(canvas);
  const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));

  /** @type {{x:number,y:number}[]} */
  let points = [];
  let pose = { x: 0, y: 0, yaw: 0 };
  let havePose = false;
  let stale = true;

  function resize() {
    const rect = parent.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    draw();
  }

  /** World -> screen, rotated so the robot faces up. */
  function project(x, y) {
    const dx = x - pose.x;
    const dy = y - pose.y;
    // Rotate by -yaw into the robot frame, then map forward to screen -up.
    const c = Math.cos(-pose.yaw);
    const s = Math.sin(-pose.yaw);
    const fx = dx * c - dy * s;
    const fy = dx * s + dy * c;
    const scale = Math.min(canvas.width, canvas.height) / (2 * RANGE_M);
    return [canvas.width / 2 - fy * scale, canvas.height / 2 - fx * scale];
  }

  function draw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const scale = Math.min(canvas.width, canvas.height) / (2 * RANGE_M);

    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.lineWidth = 1;
    for (let r = RING_M; r <= RANGE_M; r += RING_M) {
      ctx.beginPath();
      ctx.arc(cx, cy, r * scale, 0, Math.PI * 2);
      ctx.stroke();
    }

    if (points.length) {
      ctx.globalAlpha = stale ? 0.35 : 1;
      ctx.strokeStyle = "#ffd700";
      ctx.lineWidth = 2 * (window.devicePixelRatio || 1);
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      for (const p of points) {
        const [sx, sy] = project(p.x, p.y);
        ctx.lineTo(sx, sy);
      }
      ctx.stroke();
      ctx.fillStyle = "#00ff88";
      for (const p of points) {
        const [sx, sy] = project(p.x, p.y);
        ctx.beginPath();
        ctx.arc(sx, sy, 3.5 * (window.devicePixelRatio || 1), 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // The robot: a triangle pointing up, since the view is robot-up.
    const r = 7 * (window.devicePixelRatio || 1);
    ctx.fillStyle = havePose ? "#7aa2ff" : "rgba(122,162,255,0.35)";
    ctx.beginPath();
    ctx.moveTo(cx, cy - r);
    ctx.lineTo(cx - r * 0.7, cy + r * 0.8);
    ctx.lineTo(cx + r * 0.7, cy + r * 0.8);
    ctx.closePath();
    ctx.fill();
  }

  const observer = new ResizeObserver(resize);
  observer.observe(parent);
  resize();

  return {
    /** @param {{x:number,y:number,yaw:number}} p */
    setPose(p) {
      pose = p;
      havePose = true;
      draw();
    },
    /** @param {{x:number,y:number}[]} pts */
    setWaypoints(pts) {
      points = pts;
      stale = false;
      draw();
    },
    /** Dim the path once it stops being refreshed — a frozen line that still
     *  looks live is the one thing this view must not show. */
    setStale(value) {
      if (stale === value) return;
      stale = value;
      draw();
    },
    destroy() {
      observer.disconnect();
      canvas.remove();
    },
  };
}
