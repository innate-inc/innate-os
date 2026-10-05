// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
/** Strict transient rehearsal layer; coordinates are always in odom. */
export function parseSockLayout(msg) {
  try {
    const d = JSON.parse(msg?.data ?? "");
    if (d.version !== 1 || d.frame !== "odom" || typeof d.run !== "string" ||
        typeof d.status !== "string" || !Array.isArray(d.points) || d.points.length > 4 ||
        ![d.origin?.x, d.origin?.y, d.origin?.theta].every(Number.isFinite)) return null;
    const ids = new Set();
    for (const p of d.points) {
      if (!["sock-1", "sock-2", "sock-3", "box"].includes(p.id) || ids.has(p.id) ||
          ![p.x, p.y].every(Number.isFinite) || typeof p.label !== "string" ||
          !["pending", "held", "released", "target"].includes(p.status)) return null;
      ids.add(p.id);
    }
    return d;
  } catch { return null; }
}

/** Transform an odom target to the displayed map frame. */
export function projectSockPoint(p, tf) {
  const c = Math.cos(tf.theta), s = Math.sin(tf.theta);
  return { x: c*p.x-s*p.y+tf.tx, y: s*p.x+c*p.y+tf.ty };
}

/** @param {CanvasRenderingContext2D} ctx */
export function drawSockLayout(ctx, layout, project, d = 1) {
  if (!layout) return;
  ctx.save();
  ctx.font = `${12*d}px ui-monospace, monospace`;
  ctx.textAlign = "left";
  for (const p of layout.points) {
    const {px, py} = project(p);
    const color = p.id === "box" ? "#f1bd69" : p.status === "released" ? "#69d9a0" : p.status === "held" ? "#e5a0fa" : "#82cfff";
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(px, py, 5*d, 0, Math.PI*2); ctx.fill();
    const label = p.id === "box" ? "Box" : `${p.label} · ${p.status}`;
    const w = ctx.measureText(label).width;
    ctx.fillStyle = "rgba(10,10,12,.88)";
    ctx.fillRect(px+8*d, py-14*d, w+8*d, 19*d);
    ctx.fillStyle = color; ctx.fillText(label, px+12*d, py);
  }
  ctx.restore();
}

/** Local top-down layout when the robot has no occupancy map. */
export function drawLocalSockLayout(ctx, canvas, layout, robot, d=1) {
  const scale = Math.min(canvas.width, canvas.height) / 1.5;
  const project = p => ({px: canvas.width/2-(p.y-layout.origin.y)*scale,
                         py: canvas.height/2-(p.x-layout.origin.x)*scale});
  ctx.save();
  ctx.strokeStyle = "#25252d"; ctx.lineWidth = d;
  for (let r=.2; r<=.6; r+=.2) {
    ctx.beginPath();ctx.arc(canvas.width/2,canvas.height/2,r*scale,0,Math.PI*2);ctx.stroke();
  }
  const p=project(robot ?? layout.origin);
  const yaw=robot?.yaw ?? layout.origin.theta;
  ctx.fillStyle="#ee8dab";ctx.beginPath();ctx.arc(p.px,p.py,7*d,0,Math.PI*2);ctx.fill();
  ctx.strokeStyle="#ee8dab";ctx.beginPath();ctx.moveTo(p.px,p.py);
  ctx.lineTo(p.px-Math.sin(yaw)*25*d,p.py-Math.cos(yaw)*25*d);ctx.stroke();
  ctx.fillStyle="#ccc";ctx.font=`${13*d}px ui-monospace, monospace`;ctx.textAlign="left";
  ctx.fillText("Stationary sock layout · "+layout.status,16*d,25*d);
  ctx.fillStyle="#888";ctx.fillText("Local odometry · rings every 20 cm",16*d,45*d);
  drawSockLayout(ctx,layout,project,d);
  ctx.restore();
}
