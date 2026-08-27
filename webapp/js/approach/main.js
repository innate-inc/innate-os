// @ts-nocheck
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Approach debug — what pick_any_object / drop_in_box see and aim at.
//
// Subscribes to /brain/approach_debug (published by _FloorApproach._debug) and
// draws the vision model's detection, the back-projected floor point, the near
// rim line, the sweet box and the live flow-tracked pixel over the frame the
// model actually saw, beside a base_link plan view showing the bumper/reach
// squeeze the release pose has to fit into. It runs and cancels the skills too.
//
// Debug-branch only: this page is a tuning instrument, not product surface.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";

const PAGE_CSS = `

      .approach-page {
        --bg: #0d0f12; --panel: #16191f; --panel2: #1d212a; --line: #2a2f3a;
        --txt: #d7dbe2; --dim: #8a92a0; --accent: #5cc8ff; --ok: #3ddc84;
        --bad: #ff5c6c; --warn: #ffcf5c; --det: #ffcf5c; --pt: #ff5cd8;
        --rim: #5cffe0; --trk: #ff9a3d;
        --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
      }
      .approach-page, .approach-page * { box-sizing: border-box; }
      .approach-page header { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
        padding: 10px 14px; border-bottom: 1px solid var(--line); background: var(--panel); }
      .approach-page header h1 { font-size: 14px; margin: 0 10px 0 0; font-weight: 600; }
      .approach-page header h1 small { color: var(--dim); font-weight: 400; }
      .approach-page input, .approach-page select, .approach-page button { font: inherit; color: var(--txt); background: var(--panel2);
        border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; }
      .approach-page input[type="text"] { font-family: var(--mono); }
      .approach-page button { cursor: pointer; }
      .approach-page button:hover:not(:disabled) { border-color: var(--accent); }
      .approach-page button:disabled { opacity: 0.4; cursor: default; }
      .approach-page .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--dim); display: inline-block; flex: none; }
      .approach-page .dot.ok { background: var(--ok); } .dot.bad { background: var(--bad); } .dot.warn { background: var(--warn); }
      .approach-page .spacer { flex: 1; }
      .approach-page { overflow: auto; height: 100%; }
      .approach-page main { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(320px, 420px);
        gap: 12px; padding: 12px; align-items: start; max-width: 1180px; }
      @media (max-width: 940px) { main { grid-template-columns: 1fr; } }
      .approach-page .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 12px; }
      .approach-page .panel h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--dim);
        margin: 0; padding: 8px 12px; border-bottom: 1px solid var(--line);
        display: flex; align-items: center; gap: 8px; }
      .approach-page .panel h2 .spacer { flex: 1; }
      .approach-page .panel .body { padding: 10px 12px; }
      .approach-page canvas { display: block; width: 100%; height: auto; background: #000;
        border-radius: 0 0 8px 8px; }
      .approach-page .kv { display: grid; grid-template-columns: auto 1fr; gap: 3px 12px; font-family: var(--mono); font-size: 12px; }
      .approach-page .kv .k { color: var(--dim); } .kv .v { text-align: right; word-break: break-word; }
      .approach-page .kv .v.bad { color: var(--bad); } .kv .v.ok { color: var(--ok); } .kv .v.warn { color: var(--warn); }
      .approach-page .legend { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--dim);
        padding: 8px 12px; border-top: 1px solid var(--line); }
      .approach-page .legend span::before { content: ""; display: inline-block; width: 9px; height: 9px;
        margin-right: 4px; border-radius: 2px; background: currentColor; vertical-align: -1px; }
      .approach-page #log { max-height: 300px; overflow-y: auto; font-family: var(--mono); font-size: 11.5px; }
      .approach-page #log div { padding: 2px 12px; border-bottom: 1px solid var(--line); white-space: pre-wrap; }
      .approach-page #log div:hover { background: var(--panel2); }
      .approach-page .stage-tag { font-family: var(--mono); color: var(--accent); }
      .approach-page .muted { color: var(--dim); }
    
`;

export function mount(stage) {
  return mountPage(stage, "approach-page", buildView);
}

function buildView(root) {
  const style = document.createElement("style");
  style.textContent = PAGE_CSS;
  root.append(style);
  root.insertAdjacentHTML("beforeend", `
<header>
      <h1>Approach Debug <small>/brain/approach_debug</small></h1>
      <span class="spacer"></span>
      <select id="skill"><option value="">(connect for skills)</option></select>
      <input type="text" id="prompt" value="the box" style="width:150px" />
      <button id="run">Run</button>
      <button id="stop">Stop</button>
      <button id="clear">Clear</button>
    </header>

    <main>
      <div>
        <div class="panel">
          <h2>Head camera <span class="spacer"></span><span id="frameAge" class="muted"></span></h2>
          <canvas id="view" width="640" height="480"></canvas>
          <div class="legend">
            <span style="color:var(--det)">detection box</span>
            <span style="color:var(--pt)">floor-contact point</span>
            <span style="color:var(--rim)">near rim line</span>
            <span style="color:var(--ok)">sweet box (park here)</span>
            <span style="color:var(--trk)">flow-tracked pixel</span>
            <span style="color:var(--dim)">plan view also draws the reach limit at the release height</span>
          </div>
        </div>
        <div class="panel">
          <h2>Plan view <span class="spacer"></span><span class="muted">base_link, metres</span></h2>
          <canvas id="plan" width="640" height="300"></canvas>
        </div>
      </div>

      <div>
        <div class="panel">
          <h2>State</h2>
          <div class="body"><div class="kv" id="state-kv"></div></div>
        </div>
        <div class="panel">
          <h2>Run</h2>
          <div class="body"><div class="kv" id="run-kv"></div></div>
        </div>
        <div class="panel">
          <h2>Records <span class="spacer"></span><span class="muted" id="count">0</span></h2>
          <div id="log"></div>
        </div>
      </div>
    </main>
  `);

  // Scoped to this page's subtree: two pages may share an element id, and the
  // shell keeps the outgoing page in the DOM until destroy() returns.
  const $ = (id) => root.querySelector(`#${id}`);
  const unsubs = [];

  const DEBUG_TOPIC = "/brain/approach_debug";
  const EXECUTE_SKILL_ACTION = "/execute_skill";
  const EXECUTE_SKILL_ACTION_TYPE = "brain_messages/action/ExecuteSkill";
  const CANCEL_SKILL_SERVICE = "/brain/cancel_skill";
  const AVAILABLE_SKILLS_TOPIC = "/brain/available_skills";
  // The debug records carry a frame only on a Gemini look — a 55 kB base64
  // JPEG per servo tick would stall rosbridge — so the background comes from
  // the camera topic instead and the overlays are drawn over whatever is
  // current. frameAge below says how stale the overlays themselves are.
  const CAM_TOPIC = "/mars/main_camera/left/image_raw/compressed";
  const CAM_THROTTLE_MS = 200;
  // Overlays are computed for the frame the model saw. Drawn at full strength
  // over a LIVE background they lie — an 80-second-old detection box sitting
  // on the sofa while the container it named is somewhere else entirely — so
  // past this age they fade to a ghost.
  const OVERLAY_FRESH_MS = 2500;
  // Namespace is workspace-dependent (innate_skills -> "innate-os"), so the
  // picker reads the live roster instead of hardcoding ids that silently
  // 404 when a skill moves package.
  const WANTED = ["drop_in_box", "pick_any_object"];
  const IMG_W = 640, IMG_H = 480;

  // base_link geometry the drop has to fit inside. Front bumper from the
  // nav costmap footprint, reach box from Manipulation.REACH_X/REACH_Y —
  // the release pose lives in the 15 cm between them.
  const FOOT = { front: 0.25, back: -0.20, half: 0.165 };
  const REACH = { x0: 0.22, x1: 0.40, y0: -0.10, y1: 0.10 };

  const view = $("view").getContext("2d");
  const plan = $("plan").getContext("2d");

  /** Last record of each stage, so a tick keeps the last frame's overlays. */
  const last = { detect: null, localize: null, follow: null, rim: null, release: null, hold: null, verify: null };
  let frame = null;          // HTMLImageElement of the newest published JPEG
  let frameStamp = 0;
  let live = null;             // newest camera frame, drawn under the overlays
  let liveStamp = 0;
  let livePending = false;
  let records = 0;
  let run = null;            // { cancel, skill }

  // ---- drawing -----------------------------------------------------------

  function cross(ctx, x, y, r, color, w = 2) {
    ctx.strokeStyle = color; ctx.lineWidth = w;
    ctx.beginPath();
    ctx.moveTo(x - r, y); ctx.lineTo(x + r, y);
    ctx.moveTo(x, y - r); ctx.lineTo(x, y + r);
    ctx.stroke();
  }

  function label(ctx, text, x, y, color) {
    ctx.font = "11px ui-monospace, Menlo, monospace";
    const w = ctx.measureText(text).width + 6;
    ctx.fillStyle = "rgba(0,0,0,.65)";
    ctx.fillRect(x, y - 12, w, 14);
    ctx.fillStyle = color;
    ctx.fillText(text, x + 3, y - 1);
  }

  function drawView() {
    view.clearRect(0, 0, IMG_W, IMG_H);
    const bg = live ?? frame;
    if (bg) view.drawImage(bg, 0, 0, IMG_W, IMG_H);
    else {
      view.fillStyle = "#111"; view.fillRect(0, 0, IMG_W, IMG_H);
      view.fillStyle = "#666"; view.font = "13px system-ui";
      view.fillText("waiting for a detection frame…", 18, 26);
    }

    const sweet = last.localize?.sweet ?? last.follow?.sweet;
    if (sweet?.center_px) {
      const [cu, cv] = sweet.center_px;
      view.setLineDash([5, 4]);
      view.strokeStyle = "rgba(61,220,132,.55)"; view.lineWidth = 1.5;
      view.strokeRect(cu - sweet.outer_px, cv - sweet.outer_px, sweet.outer_px * 2, sweet.outer_px * 2);
      view.setLineDash([]);
      view.strokeStyle = "#3ddc84"; view.lineWidth = 2;
      view.strokeRect(cu - sweet.accept_px, cv - sweet.accept_px, sweet.accept_px * 2, sweet.accept_px * 2);
      cross(view, cu, cv, 7, "#3ddc84");
      label(view, `park x=${sweet.xy?.[0]?.toFixed(2)}`, cu - sweet.accept_px, cv - sweet.accept_px - 2, "#3ddc84");
    }

    // The sweet box is geometry (a fixed pixel target for this tilt and park
    // distance), true of any frame — it never fades. Everything below is an
    // observation of one particular frame.
    const ageOf = (rec) => (rec?._rx ? performance.now() - rec._rx : Infinity);
    const det = last.detect;
    view.globalAlpha = ageOf(det) < OVERLAY_FRESH_MS ? 1 : 0.28;
    if (det?.box_px) {
      const [x, y, w, h] = det.box_px;
      view.strokeStyle = "#ffcf5c"; view.lineWidth = 2;
      view.strokeRect(x, y, w, h);
      label(view, det.label ?? "target", x, y, "#ffcf5c");
      if (det.near_rim_v != null) {
        view.strokeStyle = "#5cffe0"; view.lineWidth = 2;
        view.beginPath(); view.moveTo(x, det.near_rim_v); view.lineTo(x + w, det.near_rim_v); view.stroke();
        label(view, "near rim", x + w - 54, det.near_rim_v - 1, "#5cffe0");
      }
    }
    if (det?.point_px) cross(view, det.point_px[0], det.point_px[1], 10, "#ff5cd8", 2.5);

    const trk = last.follow?.track_px;
    view.globalAlpha = ageOf(last.follow) < OVERLAY_FRESH_MS ? 1 : 0.28;
    if (trk) {
      view.fillStyle = "#ff9a3d";
      view.beginPath(); view.arc(trk[0], trk[1], 5, 0, Math.PI * 2); view.fill();
    }
    view.globalAlpha = 1;
  }

  function drawPlan() {
    const W = 640, H = 300;
    // x forward (right on screen), y left (up on screen).
    const X0 = -0.30, X1 = 1.30, s = W / (X1 - X0);
    const px = (x) => (x - X0) * s;
    const py = (y) => H / 2 - y * s;

    plan.fillStyle = "#0b0d10"; plan.fillRect(0, 0, W, H);
    plan.strokeStyle = "#1e232c"; plan.lineWidth = 1;
    plan.font = "10px ui-monospace, Menlo, monospace";
    for (let x = 0; x <= X1; x += 0.1) {
      plan.beginPath(); plan.moveTo(px(x), 0); plan.lineTo(px(x), H); plan.stroke();
      if (Math.abs(x * 10 - Math.round(x * 10)) < 1e-6 && Math.round(x * 10) % 2 === 0) {
        plan.fillStyle = "#4a515e"; plan.fillText(x.toFixed(1), px(x) + 2, H - 4);
      }
    }
    plan.strokeStyle = "#2a2f3a";
    plan.beginPath(); plan.moveTo(0, py(0)); plan.lineTo(W, py(0)); plan.stroke();

    plan.fillStyle = "rgba(90,100,120,.35)"; plan.strokeStyle = "#6a7385"; plan.lineWidth = 1.5;
    plan.beginPath();
    plan.rect(px(FOOT.back), py(FOOT.half), (FOOT.front - FOOT.back) * s, FOOT.half * 2 * s);
    plan.fill(); plan.stroke();
    plan.fillStyle = "#8a92a0"; plan.fillText("robot", px(FOOT.back) + 6, py(0) + 3);

    plan.fillStyle = "rgba(92,200,255,.13)"; plan.strokeStyle = "#5cc8ff";
    plan.setLineDash([4, 3]);
    plan.beginPath();
    plan.rect(px(REACH.x0), py(REACH.y1), (REACH.x1 - REACH.x0) * s, (REACH.y1 - REACH.y0) * s);
    plan.fill(); plan.stroke();
    plan.setLineDash([]);
    plan.fillStyle = "#5cc8ff"; plan.fillText("reach", px(REACH.x0) + 4, py(REACH.y1) - 4);

    const sweetX = last.localize?.sweet?.xy?.[0];
    if (sweetX != null) {
      plan.strokeStyle = "#3ddc84"; plan.lineWidth = 2; plan.setLineDash([6, 4]);
      plan.beginPath(); plan.moveTo(px(sweetX), 10); plan.lineTo(px(sweetX), H - 14); plan.stroke();
      plan.setLineDash([]);
      plan.fillStyle = "#3ddc84"; plan.fillText(`park ${sweetX.toFixed(2)}`, px(sweetX) + 4, 20);
    }

    const target = last.localize?.target_xy;
    if (target) {
      plan.fillStyle = "#ff5cd8";
      plan.beginPath(); plan.arc(px(target[0]), py(target[1]), 6, 0, Math.PI * 2); plan.fill();
      plan.fillText(`target ${target[0].toFixed(2)}`, px(target[0]) + 9, py(target[1]) + 3);
    }

    // The reach limit is a function of the release height, so it only has a
    // value once a release pose exists — drawn beside the park line to show
    // how much of the 0.25→0.40 band that height actually leaves.
    const reachLimit = last.release?.reach_x_max;
    if (reachLimit != null) {
      plan.strokeStyle = "#ff9a3d"; plan.lineWidth = 1.5; plan.setLineDash([3, 3]);
      plan.beginPath(); plan.moveTo(px(reachLimit), 10); plan.lineTo(px(reachLimit), H - 14); plan.stroke();
      plan.setLineDash([]);
      plan.fillStyle = "#ff9a3d";
      plan.fillText(`reach @z ${reachLimit.toFixed(2)}`, px(reachLimit) + 4, 34);
    }

    const rel = last.release?.target_xyz;
    if (rel) {
      const inReach =
        rel[0] >= REACH.x0 - 1e-6 && rel[0] <= REACH.x1 + 1e-6 && (reachLimit == null || rel[0] <= reachLimit);
      plan.strokeStyle = inReach ? "#ffcf5c" : "#ff5c6c"; plan.lineWidth = 2.5;
      cross(plan, px(rel[0]), py(rel[1]), 9, plan.strokeStyle, 2.5);
      plan.fillStyle = plan.strokeStyle;
      plan.fillText(`release z=${rel[2].toFixed(2)}`, px(rel[0]) + 11, py(rel[1]) - 8);
    }
  }

  // ---- panels ------------------------------------------------------------

  function kv(el, rows) {
    el.innerHTML = rows
      .map(([k, v, cls]) => `<div class="k">${k}</div><div class="v ${cls ?? ""}">${v ?? "—"}</div>`)
      .join("");
  }

  const n = (v, d = 3) => (typeof v === "number" ? v.toFixed(d) : null);

  function render() {
    drawView();
    drawPlan();
    const l = last.localize, r = last.rim, rel = last.release, h = last.hold, v = last.verify;
    const newest = Object.values(last).filter(Boolean).sort((a, b) => b.t - a.t)[0];
    kv($("state-kv"), [
      ["skill", newest?.skill],
      ["stage", newest ? `<span class="stage-tag">${newest.stage}</span>` : null],
      ["tilt", newest ? `${newest.tilt_deg}°` : null],
      ["detection", last.detect?.box_px ? last.detect.box_px.map((q) => Math.round(q)).join(", ") : last.detect?.note],
      ["floor point px", last.detect?.point_px?.map((q) => Math.round(q)).join(", ")],
      ["target x,y", l?.target_xy ? `${n(l.target_xy[0])}, ${n(l.target_xy[1])}` : null],
      ["follow", last.follow?.note ?? (last.follow?.track_px ? "tracking" : null)],
      ["cmd vx,wz", last.follow?.cmd ? last.follow.cmd.map((q) => n(q, 3)).join(", ") : null],
    ]);
    kv($("run-kv"), [
      ["rim z", n(r?.rim_z), r ? "ok" : ""],
      ["rim raw", n(r?.rim_raw)],
      ["rim from", r?.rim_source, r?.rim_source === "bbox_top" ? "warn" : ""],
      ["release x,y,z", rel?.target_xyz ? rel.target_xyz.map((q) => n(q, 3)).join(", ") : null],
      ["reach x_max", n(rel?.reach_x_max)],
      ["j6", n(h?.j6)],
      ["holding", h ? String(h.held) : null, h && !h.held ? "bad" : "ok"],
      ["landed", v ? String(v.landed) : null, v && v.landed === false ? "bad" : "ok"],
      ["verdict", v?.reply ? String(v.reply).slice(0, 80) : null],
    ]);
    const ageOf = (stamp) => `${((performance.now() - stamp) / 1000) | 0}s`;
    const stale = performance.now() - frameStamp > OVERLAY_FRESH_MS;
    $("frameAge").textContent = live
      ? `live${frameStamp ? ` · overlays ${ageOf(frameStamp)} old${stale ? " (faded)" : ""}` : ""}`
      : frameStamp
        ? `frame ${ageOf(frameStamp)} ago`
        : "";
  }

  function logLine(rec) {
    const el = document.createElement("div");
    const bits = Object.entries(rec)
      .filter(([k]) => !["image", "skill", "t", "tilt_deg", "stage", "sweet"].includes(k))
      .map(([k, val]) => `${k}=${typeof val === "number" ? val.toFixed(3) : JSON.stringify(val)}`);
    el.textContent = `${rec.stage.padEnd(9)} ${bits.join(" ")}`;
    $("log").prepend(el);
    while ($("log").childElementCount > 300) $("log").lastElementChild.remove();
    $("count").textContent = String(++records);
  }

  // ---- wire up -----------------------------------------------------------

  function onRecord(rec) {
    if (!rec || typeof rec.stage !== "string") return;
    // A tick keeps the previous stage's overlays: only a record that
    // carries a frame replaces the image the overlays are drawn on.
    if (rec.stage === "detect" || rec.stage === "localize") last.follow = null;
    rec._rx = performance.now();
    last[rec.stage] = rec;
    if (rec.image) {
      const img = new Image();
      img.onload = () => { frame = img; frameStamp = performance.now(); render(); };
      img.src = `data:image/jpeg;base64,${rec.image}`;
    }
    logLine(rec);
    render();
  }


  unsubs.push(ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    let roster;
    try {
      roster = JSON.parse(msg.data)?.skills ?? [];
    } catch {
      return;
    }
    const ids = roster.map((s) => s?.id).filter((id) => WANTED.some((w) => id?.endsWith(`/${w}`)));
    if (!ids.length) return;
    const keep = $("skill").value;
    $("skill").innerHTML = ids.map((id) => `<option value="${id}">${id}</option>`).join("");
    if (ids.includes(keep)) $("skill").value = keep;
  }, 0, "std_msgs/msg/String"));

  // Decode at most one camera frame at a time: under load the arrivals outrun
  // decoding and the queue grows without the view ever getting fresher.
  unsubs.push(
    ros.subscribe(
      CAM_TOPIC,
      (msg) => {
        if (livePending || !msg?.data) return;
        livePending = true;
        const img = new Image();
        img.onload = () => {
          live = img;
          liveStamp = performance.now();
          livePending = false;
          render();
        };
        img.onerror = () => { livePending = false; };
        img.src = `data:image/jpeg;base64,${msg.data}`;
      },
      CAM_THROTTLE_MS,
      "sensor_msgs/msg/CompressedImage",
    ),
  );

  unsubs.push(ros.subscribe(DEBUG_TOPIC, (msg) => {
    try {
      onRecord(JSON.parse(msg.data));
    } catch (e) {
      console.warn("bad debug record", e);
    }
  }, 0, "std_msgs/msg/String"));

  $("clear").onclick = () => {
    for (const k of Object.keys(last)) last[k] = null;
    frame = null; frameStamp = 0; live = null; liveStamp = 0; records = 0;
    $("log").innerHTML = ""; $("count").textContent = "0";
    render();
  };

  $("run").onclick = () => {
    const skill = $("skill").value;
    if (run || !skill) return;
    const { promise, cancel } = ros.sendActionGoal(
      EXECUTE_SKILL_ACTION,
      EXECUTE_SKILL_ACTION_TYPE,
      { skill_type: skill, inputs: JSON.stringify({ prompt: $("prompt").value }) },
    );
    run = { cancel, skill };
    $("run").disabled = true;
    promise
      .then((res) => logLine({ stage: "result", ok: res?.success, message: res?.message }))
      .catch((e) => logLine({ stage: "result", ok: false, message: String(e?.message ?? e) }))
      .finally(() => { run = null; $("run").disabled = ros.state !== "connected"; });
  };

  // Cancel through the service, not the goal handle: an action cancel only
  // binds to the client that sent the goal, so this also stops a run the
  // agent or another page started.
  $("stop").onclick = async () => {
    try {
      await ros.callService(CANCEL_SKILL_SERVICE, {});
    } catch (e) {
      logLine({ stage: "stop", error: String(e?.message ?? e) });
    }
    run?.cancel?.();
  };

  // The Run button follows the shared socket; the shell owns connecting.
  unsubs.push(
    ros.onStateChange((state) => {
      $("run").disabled = state !== "connected" || !!run;
    }),
  );

  render();
  const timer = setInterval(render, 1000);

  return {
    destroy() {
      for (const off of unsubs) off();
      clearInterval(timer);
      root.replaceChildren();
    },
  };
}
