// SimStage — the webapp's camera panel in simulation: a mounted Three.js
// canvas (drag-to-orbit), not a video element; keeps the .video-stage class
// so the webapp's CSS behaves identically. Owns all rendering: primary view
// full-res every frame, PiP thumbnails scissor-rendered from the same GL
// context and blitted out.
//
// One stage serves every page: detach() parks it out of the DOM, attach()
// drops it into the next one. Rebuilding it per page refetched ~80 MB of
// models and never gave the memory back (~450 MB of renderer RSS a switch),
// which walked a phone into the OS memory killer.

import * as THREE from "three";
import { SimScene, type CameraMode, type CameraView } from "./scene";
import type { PropInfo } from "./props";
import { LoadQueue } from "./loadQueue";
import { THUMB_H, THUMB_W, type SimSession } from "./simSession";

// One PiP tile refresh per N rendered frames, round-robin: ~30fps per tile
// at most half an extra scene render per frame.
const THUMB_FRAME_DIV = 2;

// 60fps render cap: uncapped 120Hz rAF doubles the page's GPU/CPU for no
// visible gain (~75Hz interpolated state) and the load jitters everything.
const MIN_FRAME_MS = 1000 / 62;

// Scene setup and the webapp's challenge panel expand over the same corner of
// the stage, so at most one may be open. They ship in separate bundles -- this
// one is vite-built and imported at runtime -- so the handshake is a document
// event rather than a shared module: the event name and the detail.panel values
// are a contract with webapp/js/agent/challengePanel.js.
const PANEL_OPEN_EVENT = "innate:panel-open";
const PANEL_ID = "sim-scene-setup";

const VIEW_FOR: Record<string, CameraView> = { main: "main", arm: "arm", orbit: "orbit" };
const ROTATION_DRAG_PX = 6;
const PROP_FORWARD_ANGLE = Math.PI / 2;

interface PlacementDrag {
  origin: THREE.Vector3;
  pointerId: number;
  pointerX: number;
  pointerY: number;
  yaw: number;
}

type PlacementState =
  | { kind: "near-robot" }
  | { kind: "choose-prop" }
  | { kind: "following"; prop: string }
  | { kind: "rotating"; prop: string; drag: PlacementDrag };

const PLACEMENT_HINT: Record<PlacementState["kind"], string> = {
  "near-robot": "Select an object to place it within the robot's reach.",
  "choose-prop": "Select an object, then click where it should go.",
  following: "Move over the scene · Click to place · Drag to rotate.",
  rotating: "Drag to rotate · Release to place.",
};

// Touch has no hover to preview the drop with, and the panel covers the scene
// being aimed at, so there it steps aside and this replaces its hint.
const TOUCH_PLACEMENT_HINT: Partial<Record<PlacementState["kind"], string>> = {
  following: "Tap to place it",
  rotating: "Drag to rotate · Release to place",
};

export function createSimStage(
  parent: HTMLElement,
  session: SimSession,
): {
  audioEl: null;
  setSafeInsets: (insets: { right?: number }) => void;
  attach: (parent: HTMLElement) => void;
  detach: () => void;
  destroy: () => void;
} {
  const wrap = document.createElement("div");
  // The class's position:absolute+inset:0 must survive: overriding it once had
  // the wrap size itself off the canvas buffer, ignoring window resizes.
  wrap.className = "video-stage";
  const canvas = document.createElement("canvas");
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  wrap.appendChild(canvas);
  parent.appendChild(wrap);

  // Sim-only scene setup, bottom-left above the webapp's WASD overlay.
  const debugStack = document.createElement("div");
  debugStack.className = "sim-debug-stack";
  wrap.appendChild(debugStack);

  const setup = document.createElement("div");
  setup.className = "sim-scene-setup";
  const setupBody = document.createElement("div");
  setupBody.id = "sim-scene-setup-body";
  setupBody.className = "sim-scene-panel";
  const setupHeader = document.createElement("div");
  setupHeader.className = "sim-scene-header";
  setupHeader.innerHTML = "<strong>Scene setup</strong><small>Add and place objects</small>";
  setupBody.appendChild(setupHeader);

  const setupToggle = document.createElement("button");
  setupToggle.type = "button";
  setupToggle.className = "sim-scene-toggle";
  setupToggle.setAttribute("aria-controls", setupBody.id);
  setupToggle.innerHTML =
    '<span class="sim-scene-toggle-icon sim-scene-toggle-shapes" aria-hidden="true"></span>' +
    '<svg class="sim-scene-toggle-icon sim-scene-toggle-close" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';

  let setupOpen = localStorage.getItem("sim-scene-panel-open") === "true";
  const setSetupOpen = (open: boolean) => {
    setupOpen = open;
    setup.classList.toggle("open", open);
    setupToggle.setAttribute("aria-expanded", String(open));
    setupToggle.setAttribute("aria-label", open ? "Close scene setup" : "Open scene setup");
    localStorage.setItem("sim-scene-panel-open", String(open));
    if (open) document.dispatchEvent(new CustomEvent(PANEL_OPEN_EVENT, { detail: { panel: PANEL_ID } }));
  };
  const onPanelOpen = (event: Event) => {
    const opened = (event as CustomEvent<{ panel?: string }>).detail?.panel;
    if (opened === PANEL_ID || !setupOpen) return;
    setSetupOpen(false);
    // An armed prop would keep following the cursor with the panel gone.
    clearPlacementSelection();
  };
  document.addEventListener(PANEL_OPEN_EVENT, onPanelOpen);
  setupToggle.onclick = () => setSetupOpen(!setupOpen);
  setup.append(setupBody, setupToggle);
  debugStack.appendChild(setup);
  setSetupOpen(setupOpen);

  const setChipOn = (el: HTMLElement, on: boolean) => {
    el.classList.toggle("is-active", on);
    el.setAttribute("aria-pressed", String(on));
  };

  const makeChip = (label: string, title?: string) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sim-scene-button";
    b.textContent = label;
    if (title) b.title = title;
    return b;
  };

  const addToggle = (parent: HTMLElement, label: string, onToggle: (on: boolean) => void) => {
    const b = makeChip(label);
    setChipOn(b, false);
    parent.appendChild(b);
    let on = false;
    b.onclick = () => {
      on = !on;
      setChipOn(b, on);
      onToggle(on);
    };
  };

  const objectsSection = document.createElement("section");
  objectsSection.className = "sim-scene-section";
  const objectsHeader = document.createElement("div");
  objectsHeader.className = "sim-scene-section-header";
  const objectsLabel = document.createElement("h3");
  objectsLabel.textContent = "Objects";
  const clearChip = makeChip("Clear all", "Send every prop back off-map");
  clearChip.classList.add("sim-clear-props");
  const clearIcon = document.createElement("span");
  clearIcon.className = "sim-clear-props-icon";
  clearIcon.setAttribute("aria-hidden", "true");
  clearChip.prepend(clearIcon);
  clearChip.onclick = () => {
    clearPlacementSelection();
    session.removeAllProps();
  };
  const propRows = document.createElement("div");
  propRows.className = "sim-prop-grid";
  objectsHeader.append(objectsLabel, clearChip);
  objectsSection.append(objectsHeader, propRows);
  setupBody.appendChild(objectsSection);

  const placementSection = document.createElement("section");
  placementSection.className = "sim-scene-section sim-placement-section";
  const placementLabel = document.createElement("h3");
  placementLabel.textContent = "Place objects";
  const modeSwitch = document.createElement("div");
  modeSwitch.className = "sim-placement-switch";
  modeSwitch.setAttribute("role", "group");
  modeSwitch.setAttribute("aria-label", "Object placement");
  const robotButton = document.createElement("button");
  robotButton.type = "button";
  robotButton.textContent = "Near robot";
  const spotButton = document.createElement("button");
  spotButton.type = "button";
  spotButton.textContent = "Choose spot";
  modeSwitch.append(robotButton, spotButton);
  const placementHint = document.createElement("p");
  placementHint.className = "sim-placement-hint";
  // Lives on the stage, not in the panel, so it survives the panel closing.
  const placementToast = document.createElement("p");
  placementToast.className = "sim-placement-toast";
  placementToast.hidden = true;
  wrap.appendChild(placementToast);
  const coarsePointer = window.matchMedia("(hover: none)");
  let placement: PlacementState = { kind: "choose-prop" };
  const selectedProp = (): string | null =>
    placement.kind === "following" || placement.kind === "rotating" ? placement.prop : null;
  const refreshPlacementHint = () => {
    placementHint.textContent = PLACEMENT_HINT[placement.kind];
    const toast = TOUCH_PLACEMENT_HINT[placement.kind];
    placementToast.hidden = !toast || !coarsePointer.matches;
    placementToast.textContent = toast ?? "";
  };
  const refreshPlacementUi = () => {
    const choosingSpot = placement.kind !== "near-robot";
    modeSwitch.classList.toggle("spot-selected", choosingSpot);
    robotButton.classList.toggle("is-active", !choosingSpot);
    spotButton.classList.toggle("is-active", choosingSpot);
    robotButton.setAttribute("aria-pressed", String(!choosingSpot));
    spotButton.setAttribute("aria-pressed", String(choosingSpot));
    refreshPlacementHint();
  };
  robotButton.onclick = () => {
    setPlacement({ kind: "near-robot" });
  };
  spotButton.onclick = () => {
    if (placement.kind === "near-robot") setPlacement({ kind: "choose-prop" });
  };
  refreshPlacementUi();
  placementSection.append(placementLabel, modeSwitch, placementHint);
  setupBody.appendChild(placementSection);

  const utilitySection = document.createElement("section");
  utilitySection.className = "sim-scene-section sim-scene-utilities";
  const cameraModes = document.createElement("div");
  cameraModes.className = "sim-view-aids sim-camera-modes";
  const cameraModesLabel = document.createElement("span");
  cameraModesLabel.textContent = "Camera";
  const CAMERA_MODES = ["free", "chase", "top"] as const satisfies readonly CameraMode[];
  const cameraButtons = CAMERA_MODES.map((mode) => {
    const button = makeChip(mode);
    button.title = `Use the ${mode} camera mode`;
    button.onclick = () => scene.setCameraMode(mode);
    return button;
  });
  let cameraMode = 0;
  const refreshCameraSwitch = () => {
    cameraButtons.forEach((button, index) => setChipOn(button, index === cameraMode));
  };
  cameraModes.append(cameraModesLabel, ...cameraButtons);
  refreshCameraSwitch();
  const viewAids = document.createElement("div");
  viewAids.className = "sim-view-aids";
  const viewAidsLabel = document.createElement("span");
  viewAidsLabel.textContent = "View aids";
  viewAids.appendChild(viewAidsLabel);
  addToggle(viewAids, "Lidar", (on) => session.setLidarVisible(on));
  addToggle(viewAids, "Collisions", (on) => session.setCollisionHullsVisible(on));
  utilitySection.append(cameraModes, viewAids);
  setupBody.appendChild(utilitySection);

  const propChips = new Map<string, HTMLButtonElement>();
  const unsubscribeProps = session.onProps((props: PropInfo[]) => {
    propRows.replaceChildren();
    propChips.clear();

    const groups = new Map<string, PropInfo[]>();
    for (const prop of props) {
      if (prop.group) {
        const members = groups.get(prop.group);
        if (members) members.push(prop);
        else groups.set(prop.group, [prop]);
      }
      const chip = makeChip("", prop.title);
      chip.classList.add("sim-prop-button");
      const icon = document.createElement("span");
      icon.className = "sim-prop-icon";
      icon.textContent = prop.label;
      const label = document.createElement("span");
      label.className = "sim-prop-name";
      label.textContent = prop.title;
      chip.append(icon, label);
      setChipOn(chip, prop.name === selectedProp());
      chip.onclick = () => {
        if (placement.kind === "near-robot") {
          session.placePropAtRobot(prop.name);
          return;
        }
        setPlacement(selectedProp() === prop.name ? { kind: "choose-prop" } : { kind: "following", prop: prop.name });
      };
      propChips.set(prop.name, chip);
      propRows.appendChild(chip);
    }

    for (const [group, members] of groups) {
      if (members.length < 2) continue;
      const addSet = makeChip("", `Set down all ${members.length} ${group} props in front of the robot`);
      addSet.classList.add("sim-prop-button", "sim-add-all");
      const icon = document.createElement("span");
      icon.className = "sim-prop-add-icon";
      icon.textContent = "+";
      const label = document.createElement("span");
      label.className = "sim-prop-name";
      label.textContent = "Add set";
      addSet.append(icon, label);
      addSet.onclick = () => {
        clearPlacementSelection();
        session.placePropGroup(group);
      };
      propRows.appendChild(addSet);
    }
  });

  // Loading indicator: a compact pill holding a progress bar, centered just
  // below the camera thumbnail strip (webapp's .cam-strip pins tiles at the top
  // edge, ~14px + 120px tall) so the two don't overlap. The canvas shows
  // immediately underneath (robot first, then rooms stream in), not a full
  // black block. Fades out when the download finishes. pointer-events:none so
  // it never shields the stage; the bar re-enables them for its hover.
  const loading = document.createElement("div");
  loading.style.cssText =
    "position:absolute;top:150px;left:50%;transform:translateX(-50%);z-index:6;pointer-events:none;" +
    "display:flex;flex-direction:column;align-items:center;gap:8px;padding:12px 18px;border-radius:12px;" +
    "background:rgba(0,0,0,.42);transition:opacity .5s ease;";
  const bar = document.createElement("div");
  bar.style.cssText =
    "width:min(280px,60%);height:6px;border-radius:999px;background:rgba(255,255,255,.12);" +
    "overflow:hidden;transition:background .2s;pointer-events:auto;";
  const barFill = document.createElement("div");
  barFill.style.cssText = "height:100%;width:0%;background:#7dffc4;border-radius:999px;transition:width .3s ease;";
  bar.appendChild(barFill);
  bar.onmouseenter = () => (bar.style.background = "rgba(255,255,255,.22)");
  bar.onmouseleave = () => (bar.style.background = "rgba(255,255,255,.12)");
  const loadingLabel = document.createElement("div");
  loadingLabel.style.cssText = "color:rgba(255,255,255,.6);font:500 13px system-ui;";
  const readout = document.createElement("div");
  readout.style.cssText = "color:rgba(255,255,255,.35);font:500 11px ui-monospace,monospace;";
  loading.append(bar, loadingLabel, readout);
  wrap.appendChild(loading);

  const setLoading = (text: string) => (loadingLabel.textContent = text);
  const mb = (bytes: number) => (bytes / 1e6).toFixed(1);
  const setProgress = (loaded: number, total: number) => {
    barFill.style.width = `${total > 0 ? Math.min(100, (loaded / total) * 100) : 0}%`;
    readout.textContent = `${mb(loaded)} / ${mb(total)} MB`;
  };
  const failLoading = (text: string) => {
    barFill.style.background = "#ff9f9f";
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
  // The scrim fades when the download finishes (see the load sequence below);
  // here we only surface load failures.
  const unsubscribe = session.onChange((s) => {
    if (s.status === "error") {
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
  // The scene takes chase off when the camera is dragged, so the switch has to
  // follow the scene rather than be the only thing that knows the mode.
  scene.onCameraModeChange = (mode) => {
    const index = CAMERA_MODES.indexOf(mode);
    if (index < 0) return;
    cameraMode = index;
    refreshCameraSwitch();
  };

  function setPlacement(next: PlacementState): void {
    if (placement.kind === "rotating" && canvas.hasPointerCapture(placement.drag.pointerId)) {
      canvas.releasePointerCapture(placement.drag.pointerId);
    }
    placement = next;
    scene.clearPropPlacementPreview();
    const prop = selectedProp();
    // Get out of the way of the tap that places it.
    if (prop !== null && coarsePointer.matches) setSetupOpen(false);
    scene.setPlacementMode(prop !== null);
    canvas.style.cursor = prop ? "crosshair" : "";
    for (const [propName, chip] of propChips) {
      setChipOn(chip, propName === prop);
    }
    refreshPlacementUi();
  }
  function clearPlacementSelection(): void {
    setPlacement(placement.kind === "near-robot" ? { kind: "near-robot" } : { kind: "choose-prop" });
  }
  const yawFromDirection = (direction: THREE.Vector3): number =>
    Math.atan2(direction.y, direction.x) - PROP_FORWARD_ANGLE;

  canvas.addEventListener("pointerdown", (e) => {
    if (placement.kind !== "following" || e.button !== 0) return;
    const origin = scene.screenToFloor(e.clientX, e.clientY);
    if (!origin) return;
    placement = {
      kind: "rotating",
      prop: placement.prop,
      drag: {
        origin,
        pointerId: e.pointerId,
        pointerX: e.clientX,
        pointerY: e.clientY,
        yaw: 0,
      },
    };
    canvas.setPointerCapture(e.pointerId);
    scene.showPropPlacementPreview(placement.prop, origin.x, origin.y, 0);
    refreshPlacementUi();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (placement.kind === "near-robot" || placement.kind === "choose-prop") return;
    if (placement.kind === "rotating" && e.pointerId !== placement.drag.pointerId) return;
    const cur = scene.screenToFloor(e.clientX, e.clientY);
    if (!cur) return;
    if (placement.kind === "following") {
      scene.showPropPlacementPreview(placement.prop, cur.x, cur.y, 0);
      return;
    }
    const pointerDistance = Math.hypot(e.clientX - placement.drag.pointerX, e.clientY - placement.drag.pointerY);
    if (pointerDistance < ROTATION_DRAG_PX) return;
    const direction = cur.sub(placement.drag.origin).setZ(0);
    if (direction.lengthSq() === 0) return;
    placement.drag.yaw = yawFromDirection(direction);
    scene.showPropPlacementPreview(
      placement.prop,
      placement.drag.origin.x,
      placement.drag.origin.y,
      placement.drag.yaw,
    );
  });
  canvas.addEventListener("pointerleave", () => {
    if (placement.kind === "following") scene.clearPropPlacementPreview();
  });
  // On window, not canvas: releasing outside the canvas must still finish (or
  // cancel) the drag rather than leaving the prop armed and the preview behind.
  const finishDrop = (e: PointerEvent) => {
    if (placement.kind !== "rotating" || e.pointerId !== placement.drag.pointerId) return;
    session.dropPropAt(placement.prop, placement.drag.origin.x, placement.drag.origin.y, placement.drag.yaw);
    setPlacement({ kind: "choose-prop" });
  };
  const cancelDrop = (e: PointerEvent) => {
    if (placement.kind !== "rotating" || e.pointerId !== placement.drag.pointerId) return;
    setPlacement({ kind: "following", prop: placement.prop });
  };
  window.addEventListener("pointerup", finishDrop);
  window.addEventListener("pointercancel", cancelDrop);

  const resize = () => {
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (!w || !h) return; // hidden (map primary): keep the last real size
    scene.setRenderSize(w, h, Math.min(devicePixelRatio, 2));
    // setSize cleared the buffer (the spec clears a resized canvas) and the
    // browser paints before the next rAF, so the stage would flash black.
    scene.setView(VIEW_FOR[session.primaryCamera] ?? "orbit");
    scene.render();
  };
  const observer = new ResizeObserver(resize);
  observer.observe(wrap);
  resize();

  let raf = 0;
  let frame = 0;
  let thumbCursor = 0;
  let lastTime = performance.now();
  let disposed = false;
  let attached = true;

  // rAF is throttled for a hidden tab but not for a detached canvas, so a
  // parked stage would render full-rate into nothing.
  const startLoop = () => {
    if (raf !== 0 || !attached || disposed) return;
    lastTime = performance.now(); // a paused stage must not integrate the gap as one dt
    raf = requestAnimationFrame(loop);
  };
  const stopLoop = () => {
    cancelAnimationFrame(raf);
    raf = 0;
  };

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

  // One shared bounded queue drives real byte progress for the whole load;
  // seed an estimate so the bar has a width before Content-Lengths arrive
  // (apartment ~35 MB + robot ~7 MB), refined as real sizes land.
  const queue = new LoadQueue(2, ({ loaded, total }) => setProgress(loaded, total));
  queue.setEstimatedTotal(42e6);
  (async () => {
    try {
      // Start rendering + accept poses right away: the worldstate socket is
      // already connecting (session.start), so the robot's placeholder box
      // snaps to its real spawn pose while the STLs stream, then the mesh
      // replaces it. Bail at each await if the stage was destroyed mid-load
      // (SPA remount) -- else we'd mutate a disposed scene.
      session.stageReady();
      startLoop();
      // The apartment manifest first (a few KB, unqueued): it draws every
      // room's placeholder box and frames the camera on them, so the first
      // frames show the apartment's wireframe layout rather than an empty
      // void while the meshes are still being fetched.
      setLoading("loading layout...");
      const layout = await scene.loadApartmentLayout();
      if (disposed) return;
      scene.frameLayout(layout);
      setLoading("loading robot and apartment...");
      // Await once: the robot's STLs are now in the shared queue. Enqueue the
      // apartment rooms after, so they land behind the robot (deterministic
      // robot-first, no timing guess).
      const { done: robotDone } = await scene.loadRobot(queue);
      if (disposed) return;
      const apartment = scene.streamApartment(queue, layout);
      // Await twice: the rest of both loads.
      await Promise.all([robotDone, apartment]);
      if (disposed) return;
      hideLoading();
      // Only now parse the prop models, so they queue behind the robot and
      // apartment and stay out of the progress bar -- but land well before
      // anyone clicks a prop chip. See PropLibrary.prefetchModels.
      scene.prefetchPropModels();
    } catch (err) {
      if (!disposed) session.stageError(err);
    }
  })();

  return {
    audioEl: null, // sim has no robot mic; the pages skip the mic toggle in sim mode
    setSafeInsets: (insets) => scene.setSafeInsets(insets),
    attach(next: HTMLElement) {
      attached = true;
      next.appendChild(wrap);
      startLoop();
      resize();
      // A page used to get a new scene, so entering one always framed the
      // robot in free orbit; that is page state, not session state.
      scene.setCameraMode("free");
      scene.frameRobot();
    },
    detach() {
      attached = false;
      stopLoop();
      // The agent page lifts this into its own layout; take it back before
      // that page clears its DOM.
      wrap.appendChild(debugStack);
      scene.setSafeInsets({ right: 0 }); // the agent dock's inset must not follow the stage
      clearPlacementSelection(); // an armed prop must not follow the pointer either
      wrap.remove();
    },
    destroy() {
      disposed = true;
      queue.cancel(); // stop pulling new downloads for a stage that's gone
      unsubscribe();
      unsubscribeProps();
      stopLoop();
      observer.disconnect();
      longTaskObserver?.disconnect();
      window.removeEventListener("pointerup", finishDrop);
      window.removeEventListener("pointercancel", cancelDrop);
      document.removeEventListener(PANEL_OPEN_EVENT, onPanelOpen);
      scene.dispose();
      wrap.remove();
    },
  };
}
