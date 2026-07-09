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
    b.onclick = () => {
      on = !on;
      b.style.background = on ? ON_BG : OFF_BG;
      b.style.color = on ? "#7dffc4" : "rgba(255,255,255,.75)";
      onToggle(on);
    };
    chips.appendChild(b);
  };
  addChip("lidar", (on) => session.setLidarVisible(on));
  addChip("collisions", (on) => session.setCollisionHullsVisible(on));
  addChip("follower", (on) => session.setFollowerVisible(on));
  debugStack.appendChild(chips);

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

  const scene = new SimScene(canvas, { fixedSize: { width: parent.clientWidth || 1280, height: parent.clientHeight || 720 } });
  scene.followCamera = true;

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
    scene.render();
    frame++;

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
