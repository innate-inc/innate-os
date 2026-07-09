// SimStage — the webapp's camera panel in simulation: a mounted Three.js
// canvas (drag-to-orbit), not a video element; keeps the .video-stage class
// so the webapp's CSS behaves identically. Owns all rendering: primary view
// full-res every frame, PiP thumbnails scissor-rendered from the same GL
// context and blitted out.

import { SimScene, type CameraView } from "./scene";
import { THUMB_H, THUMB_W, type SimSession } from "./simSession";

// One PiP tile refresh per N rendered frames, round-robin: ~30fps per tile
// at most half an extra scene render per frame.
const THUMB_FRAME_DIV = 2;

// 60fps render cap: uncapped 120Hz rAF doubles the page's GPU/CPU for no
// visible gain (~75Hz interpolated state) and the load jitters everything.
const MIN_FRAME_MS = 1000 / 62;

const VIEW_FOR: Record<string, CameraView> = { main: "main", arm: "arm", orbit: "orbit" };

export function createSimStage(parent: HTMLElement, session: SimSession): { audioEl: null; destroy: () => void } {
  const wrap = document.createElement("div");
  wrap.className = "video-stage"; // reuse the webapp's stage styling/CSS ladder
  wrap.style.position = "relative";
  const canvas = document.createElement("canvas");
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  wrap.appendChild(canvas);
  parent.appendChild(wrap);

  // Sim-only debug stack (toggle chips + optional perf HUD), bottom-left just
  // above the webapp's WASD overlay -- the corners and top-center belong to
  // other overlays (arm panel, cam tiles, telemetry, agent status).
  const debugStack = document.createElement("div");
  debugStack.className = "sim-debug-stack"; // pages hide it where it collides
  debugStack.style.cssText =
    "position:absolute;left:14px;bottom:132px;display:flex;flex-direction:column;gap:8px;z-index:5;";
  wrap.appendChild(debugStack);
  const chips = document.createElement("div");
  chips.style.cssText = "display:flex;gap:6px;";
  const OFF_BG = "rgba(0,0,0,.45)";
  const ON_BG = "rgba(0,255,136,.22)";
  const addChip = (label: string, onToggle: (on: boolean) => void) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.style.cssText =
      // No backdrop-filter: re-blurring over an animated canvas costs fps.
      `padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.25);background:${OFF_BG};` +
      "color:rgba(255,255,255,.75);font:500 11px system-ui;cursor:pointer;";
    let on = false;
    const apply = () => {
      b.style.background = on ? ON_BG : OFF_BG;
      b.style.color = on ? "#7dffc4" : "rgba(255,255,255,.75)";
    };
    b.onclick = () => {
      on = !on;
      apply();
      onToggle(on);
    };
    chips.appendChild(b);
    // Programmatic toggle (e.g. set-goal auto-exits after placing one goal).
    return {
      set(v: boolean) {
        if (on === v) return;
        on = v;
        apply();
        onToggle(on);
      },
    };
  };
  addChip("lidar", (on) => session.setLidarVisible(on));
  addChip("collisions", (on) => session.setCollisionHullsVisible(on));
  // Set-goal placement mode ("set goal" chip): click the floor to pick the
  // position, drag to choose the heading, release to send. Auto-exits after
  // one goal; while active the orbit drag is suspended (see the render loop).
  let goalMode = false;
  let goalChip: { set(v: boolean): void } | null = null;
  if (session.navDebug) {
    addChip("follower", (on) => session.setFollowerVisible(on));
    // Freeze pauses the simulated world in place (physics stops, the robot
    // stack keeps running); unfreezing continues the navigation mid-flight.
    addChip("freeze", (on) => session.setFrozen(on));
    goalChip = addChip("set goal", (on) => {
      goalMode = on;
      canvas.style.cursor = on ? "crosshair" : "";
    });
  }
  debugStack.appendChild(chips);

  // ?navdebug: why is the follower doing that? Reports the state MPPI's
  // critics are gated on, since it never publishes the costs themselves.
  let navEl: HTMLElement | null = null;
  let navNextAt = 0;
  if (session.navDebug) {
    navEl = document.createElement("div");
    navEl.style.cssText =
      "align-self:flex-start;max-width:340px;padding:6px 9px;border-radius:6px;" +
      "background:rgba(0,0,0,.72);color:#cfe;font:11px/1.5 ui-monospace,monospace;" +
      "pointer-events:none;white-space:pre;";
    debugStack.prepend(navEl);
  }

  // Loading overlay: spinner + staged label instead of a black canvas until
  // assets and the first world state arrive.
  const loading = document.createElement("div");
  loading.style.cssText =
    "position:absolute;inset:0;z-index:6;display:flex;flex-direction:column;align-items:center;" +
    "justify-content:center;gap:14px;background:#0b0e11;transition:opacity .45s ease;";
  const spinner = document.createElement("div");
  spinner.style.cssText =
    "width:34px;height:34px;border-radius:50%;border:3px solid rgba(255,255,255,.14);" +
    "border-top-color:#7dffc4;animation:sim-stage-spin .9s linear infinite;";
  const spinStyle = document.createElement("style");
  spinStyle.textContent = "@keyframes sim-stage-spin{to{transform:rotate(1turn)}}";
  const loadingLabel = document.createElement("div");
  loadingLabel.style.cssText = "color:rgba(255,255,255,.6);font:500 13px system-ui;";
  loading.append(spinStyle, spinner, loadingLabel);
  wrap.appendChild(loading);

  const setLoading = (text: string) => (loadingLabel.textContent = text);
  const failLoading = (text: string) => {
    spinner.style.display = "none";
    loadingLabel.style.color = "#ff9f9f";
    loadingLabel.textContent = text;
  };
  const hideLoading = () => {
    loading.style.opacity = "0";
    loading.style.pointerEvents = "none"; // never shield the stage while fading
    loading.addEventListener("transitionend", () => loading.remove(), { once: true });
    // transitionend never fires under prefers-reduced-motion (the webapp
    // disables all transitions) or when the fade starts pre-paint -- without
    // this fallback the invisible overlay stayed and ate every click.
    setTimeout(() => loading.remove(), 700);
  };
  // Hide only when the session reports frames actually flowing.
  const unsubscribe = session.onChange((s) => {
    if (s.status === "streaming") {
      hideLoading();
      unsubscribe();
    } else if (s.status === "error") {
      failLoading("simulation view failed to load — see the browser console");
      unsubscribe();
    }
  });

  // ?simperf: frame-time readout (median/p95 of the last second).
  let perfEl: HTMLElement | null = null;
  let frameTimes: number[] = [];
  let perfNextAt = 0;
  let longTaskMs = 0;
  let longTaskObserver: PerformanceObserver | null = null;
  const bare = new URLSearchParams(location.search).has("simbare");
  try {
    longTaskObserver = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) longTaskMs += e.duration;
    });
    longTaskObserver.observe({ type: "longtask", buffered: false });
  } catch {
    /* longtask unsupported -- HUD just shows 0 */
  }
  if (new URLSearchParams(location.search).has("simperf")) {
    perfEl = document.createElement("div");
    perfEl.style.cssText =
      "align-self:flex-start;padding:3px 8px;border-radius:6px;" +
      "background:rgba(0,0,0,.6);color:#9f9;font:11px ui-monospace,monospace;pointer-events:none;";
    debugStack.prepend(perfEl);
  }

  /** Render one frame of the nav-debug HUD. Kept text-only + monospace so the
   * numbers line up and it never costs a layout reflow worth measuring. */
  const renderNavHud = (el: HTMLElement) => {
    const s = session.navDebugSnapshot;
    if (!s) return;
    const n = (v: number | null, digits = 2, unit = "") => (v === null ? "—" : `${v.toFixed(digits)}${unit}`);
    const lines: string[] = [];

    // Make a paused world unmistakable -- the numbers below are live but static.
    if (session.frozen) lines.push("❚❚ WORLD FROZEN — physics paused, unfreeze to continue");

    if (s.cmd === null) {
      lines.push("COMMAND    idle (controller not running)");
    } else {
      const badge = s.stalled ? "  ⚠ STALLED" : s.wzFlips >= 4 ? `  ⚠ OSCILLATING (${s.wzFlips} flips/4s)` : "";
      lines.push(`COMMAND    vx ${n(s.cmd.vx)} m/s   wz ${n(s.cmd.wz)} rad/s${badge}`);
      if (s.finalCmd) lines.push(`  ->wheels vx ${n(s.finalCmd.vx)}      wz ${n(s.finalCmd.wz)}`);
    }

    const goalAt = s.goal
      ? `(${s.goal.x.toFixed(2)}, ${s.goal.y.toFixed(2)}, ${((s.goal.yaw * 180) / Math.PI).toFixed(0)}°) ` +
        (s.goal.commanded ? `[${s.goal.frame ?? "?"} frame, commanded]` : "[plan end]")
      : "none";
    lines.push(`GOAL       ${goalAt}`);
    lines.push(
      `           ${n(s.distanceRemaining, 2, " m")} away   recoveries ${s.recoveries ?? "—"}   t ${n(s.navTimeS, 0, "s")}`,
    );
    lines.push(
      `BLOCKING   under robot: ${s.costUnderRobot}   0.5m ahead: ${s.maxCostAhead}` +
        (s.pathOccupancy === null ? "" : `\n           path blocked ${(s.pathOccupancy * 100).toFixed(0)}%`) +
        (s.headingErrDeg === null ? "" : `   heading err ${s.headingErrDeg.toFixed(0)}°`),
    );
    lines.push("CRITICS");
    for (const c of s.critics) {
      lines.push(`  ${c.active ? "●" : "○"} ${c.name.padEnd(11)}${c.note}`);
    }
    el.textContent = lines.join("\n");
  };

  const scene = new SimScene(canvas, { fixedSize: { width: parent.clientWidth || 1280, height: parent.clientHeight || 720 } });
  scene.followCamera = true;

  // Set-goal placement: hover shows a ghost ring on the floor; press picks
  // the position; dragging picks the heading; release publishes /goal_pose.
  // A sub-15cm drag means "no heading preference" -> face the goal from the
  // robot's side, the direction it will arrive from.
  let goalDrag: { x: number; y: number } | null = null;
  const finishGoal = (e: PointerEvent) => {
    if (!goalDrag) return;
    const start = goalDrag;
    goalDrag = null;
    const cur = scene.groundPoint(e.clientX, e.clientY);
    const dx = cur ? cur.x - start.x : 0;
    const dy = cur ? cur.y - start.y : 0;
    let yaw: number;
    if (Math.hypot(dx, dy) > 0.15) {
      yaw = Math.atan2(dy, dx);
    } else {
      const robot = session.robotPose;
      yaw = robot ? Math.atan2(start.y - robot.y, start.x - robot.x) : 0;
    }
    session.publishGoalPose(start.x, start.y, yaw);
    scene.clearGoalPreview();
    goalChip?.set(false); // one goal per activation, like the map widget
  };
  canvas.addEventListener("pointerdown", (e) => {
    if (!goalMode) return;
    e.preventDefault();
    const p = scene.groundPoint(e.clientX, e.clientY);
    if (!p) return;
    goalDrag = p;
    canvas.setPointerCapture(e.pointerId);
    scene.setGoalPreview(p.x, p.y, null);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!goalMode) {
      scene.clearGoalPreview();
      return;
    }
    const cur = scene.groundPoint(e.clientX, e.clientY);
    if (!cur) return;
    if (goalDrag) {
      const dx = cur.x - goalDrag.x;
      const dy = cur.y - goalDrag.y;
      scene.setGoalPreview(goalDrag.x, goalDrag.y, Math.hypot(dx, dy) > 0.15 ? Math.atan2(dy, dx) : null);
    } else {
      scene.setGoalPreview(cur.x, cur.y, null); // hover ghost while aiming
    }
  });
  canvas.addEventListener("pointerup", finishGoal);
  canvas.addEventListener("pointercancel", () => {
    goalDrag = null;
    scene.clearGoalPreview();
  });

  const resize = () => {
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (!w || !h) return; // hidden (map primary): keep the last real size
    scene.setRenderSize(w, h, Math.min(devicePixelRatio, 2));
  };
  const observer = new ResizeObserver(resize);
  observer.observe(wrap);
  resize();

  let raf = 0;
  let frame = 0;
  let thumbCursor = 0;
  let lastTime = performance.now();
  let disposed = false;

  const loop = (now: number) => {
    raf = requestAnimationFrame(loop);
    if (now - lastTime < MIN_FRAME_MS - 1) return; // 120Hz display -> render every other vsync
    const dt = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    session.tick(scene, dt);

    // Thumbnails first (scissor corner renders, blitted out), one tile per
    // slot -- see THUMB_FRAME_DIV.
    if (!bare && frame % THUMB_FRAME_DIV === 0) {
      const live = session.liveThumbnails();
      if (live.length) {
        const { index, name } = live[thumbCursor++ % live.length];
        scene.setView(VIEW_FOR[name] ?? "orbit");
        scene.renderRegion(0, 0, THUMB_W, THUMB_H);
        // renderRegion speaks logical px; the canvas backing store is
        // scaled by the pixel ratio -- blit the full physical region or
        // the tile shows a zoomed-in crop.
        const ratio = scene.renderer.getPixelRatio();
        session.blitThumbnail(index, canvas, THUMB_W * ratio, THUMB_H * ratio);
      }
    }
    // ...then the primary view full-frame on top.
    scene.setView(VIEW_FOR[session.primaryCamera] ?? "orbit");
    // setView re-enables orbit every frame; goal placement must keep the drag
    // for itself, so re-suspend it here rather than fighting setView's state.
    if (goalMode) scene.controls.enabled = false;
    scene.render();
    frame++;

    // 5Hz: the snapshot walks the costmap along the path, and nothing here
    // changes faster than the eye can read.
    if (navEl && now >= navNextAt) {
      navNextAt = now + 200;
      renderNavHud(navEl);
    }

    if (perfEl) {
      frameTimes.push(performance.now() - now);
      if (now >= perfNextAt) {
        perfNextAt = now + 1000;
        const sorted = [...frameTimes].sort((x, y) => x - y);
        const med = sorted[sorted.length >> 1] ?? 0;
        const p95 = sorted[Math.floor(sorted.length * 0.95)] ?? 0;
        const lag = session.pipelineLag;
        const lagTxt = lag ? `  lag ${lag.curMs.toFixed(0)}ms (min ${lag.minMs.toFixed(0)})` : "";
        perfEl.textContent = `js ${med.toFixed(1)}/${p95.toFixed(1)}ms  lt ${longTaskMs.toFixed(0)}ms  ${frameTimes.length}fps${lagTxt}`;
        frameTimes = [];
        longTaskMs = 0;
      }
    }
  };

  (async () => {
    try {
      setLoading("loading apartment…");
      await scene.loadApartment();
      setLoading("loading robot…");
      await scene.loadRobot();
      setLoading("connecting to the world…");
      session.stageReady();
      if (!disposed) raf = requestAnimationFrame(loop);
    } catch (err) {
      session.stageError(err);
    }
  })();

  return {
    audioEl: null, // sim has no robot mic; the pages skip the mic toggle in sim mode
    destroy() {
      disposed = true;
      unsubscribe();
      cancelAnimationFrame(raf);
      observer.disconnect();
      longTaskObserver?.disconnect();
      scene.dispose();
      wrap.remove();
    },
  };
}
