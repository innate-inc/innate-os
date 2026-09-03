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
import {
  requiresCompleteEnvironment,
  resolveEnvironmentSource,
  type EnvironmentSource,
} from "./environmentSource";
import {
  claimEnvironmentSwapKey,
  deferEnvironmentSwapRetry,
  EnvironmentSwapCoordinator,
  prepareEnvironmentSwapScene,
} from "./environmentSwap";

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
const SWITCH_REQUEST_EVENT = "innate:sim-environment-switch-request";
const SWITCH_STATE_EVENT = "innate:sim-environment-switch-state";
const CATALOG_EVENT = "innate:sim-environment-catalog";
const VIEWER_STATE_EVENT = "innate:sim-environment-viewer-state";
const WORLD_STATE_EVENT = "innate:sim-environment-world-state";

interface EnvironmentCatalog {
  schema_version: 1;
  active: { id: string; display_name: string; fingerprint?: string } | null;
  environments: { id: string; display_name: string }[];
}

interface EnvironmentTransition {
  active: boolean;
  generation: number;
  fingerprint: string | null;
}

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
  // Via the ROS node, not the world server: it fails the in-flight arm trajectory.
  onRespawn: () => void,
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
  let canvas = document.createElement("canvas");
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

  const environmentSection = document.createElement("section");
  environmentSection.className = "sim-scene-section sim-environment-picker";
  const environmentLabel = document.createElement("label");
  environmentLabel.htmlFor = "sim-environment-select";
  environmentLabel.textContent = "Environment";
  const environmentControls = document.createElement("div");
  environmentControls.className = "sim-environment-picker__controls";
  const environmentSelect = document.createElement("select");
  environmentSelect.id = "sim-environment-select";
  environmentSelect.setAttribute("aria-label", "Simulator environment");
  environmentSelect.disabled = true;
  const environmentApply = document.createElement("button");
  environmentApply.type = "button";
  environmentApply.textContent = "Switch";
  environmentApply.disabled = true;
  environmentControls.append(environmentSelect, environmentApply);
  environmentSection.append(environmentLabel, environmentControls);
  setupBody.appendChild(environmentSection);

  let activeEnvironmentId = "";
  let environmentSwitching = false;
  let environmentSelectionDirty = false;
  const refreshEnvironmentApply = () => {
    environmentSelect.disabled = environmentSwitching || !activeEnvironmentId || environmentSelect.options.length === 0;
    environmentApply.disabled =
      environmentSwitching ||
      !activeEnvironmentId ||
      !environmentSelect.value ||
      environmentSelect.value === activeEnvironmentId;
  };
  const acceptEnvironmentCatalog = (catalog: EnvironmentCatalog) => {
    if (!catalog || catalog.schema_version !== 1 || !Array.isArray(catalog.environments)) return;
    const selected = environmentSelect.value;
    environmentSelect.replaceChildren(
      ...catalog.environments.map((environment) => {
        const option = document.createElement("option");
        option.value = environment.id;
        option.textContent = environment.display_name;
        return option;
      }),
    );
    activeEnvironmentId = catalog.active?.id ?? "";
    if (!activeEnvironmentId) environmentSelectionDirty = false;
    environmentSelect.value =
      environmentSelectionDirty && selected && catalog.environments.some(({ id }) => id === selected)
        ? selected
        : activeEnvironmentId || selected || catalog.environments[0]?.id || "";
    environmentSelectionDirty = Boolean(activeEnvironmentId) && environmentSelect.value !== activeEnvironmentId;
    refreshEnvironmentApply();
  };
  environmentSelect.onchange = () => {
    environmentSelectionDirty = environmentSelect.value !== activeEnvironmentId;
    refreshEnvironmentApply();
  };
  environmentApply.onclick = () => {
    if (!environmentSelect.value || environmentApply.disabled) return;
    document.dispatchEvent(
      new CustomEvent(SWITCH_REQUEST_EVENT, { detail: { id: environmentSelect.value } }),
    );
  };

  const setupToggle = document.createElement("button");
  setupToggle.type = "button";
  setupToggle.className = "sim-scene-toggle";
  setupToggle.setAttribute("aria-controls", setupBody.id);
  setupToggle.innerHTML =
    '<span class="sim-scene-toggle-icon sim-scene-toggle-shapes" aria-hidden="true"></span>' +
    '<svg class="sim-scene-toggle-icon sim-scene-toggle-close" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';

  const coarsePointer = window.matchMedia("(hover: none)");
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
  // Touch only: on a mouse, clicking the scene is how an armed prop gets placed.
  const onOutsidePointer = (event: PointerEvent) => {
    if (!setupOpen || !coarsePointer.matches) return;
    if (event.target instanceof Node && setup.contains(event.target)) return;
    setSetupOpen(false);
  };
  document.addEventListener("pointerdown", onOutsidePointer, true);
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
  // Page chrome belongs to the shared stage, not to a particular SimScene.
  // Keep it here so a hot-swapped scene gets the same usable viewport before
  // its first frame is revealed.
  let safeInsetRight = 0;
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
  const robotRow = document.createElement("div");
  robotRow.className = "sim-view-aids";
  const robotRowLabel = document.createElement("span");
  robotRowLabel.textContent = "Robot";
  const respawnChip = makeChip("Respawn", "Back to the spawn pose, arm home, every prop parked");
  respawnChip.onclick = () => {
    clearPlacementSelection();
    onRespawn();
  };
  robotRow.append(robotRowLabel, respawnChip);
  utilitySection.append(cameraModes, viewAids, robotRow);
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

  let scene = new SimScene(canvas, {
    fixedSize: { width: parent.clientWidth || 1280, height: parent.clientHeight || 720 },
  });
  scene.followCamera = true;
  // The scene takes chase off when the camera is dragged, so the switch has to
  // follow the scene rather than be the only thing that knows the mode.
  const bindCameraMode = (target: SimScene) => (target.onCameraModeChange = (mode) => {
    const index = CAMERA_MODES.indexOf(mode);
    if (index < 0) return;
    cameraMode = index;
    refreshCameraSwitch();
  });
  bindCameraMode(scene);

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

  const onCanvasPointerDown = (e: PointerEvent) => {
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
  };
  const onCanvasPointerMove = (e: PointerEvent) => {
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
  };
  const onCanvasPointerLeave = () => {
    if (placement.kind === "following") scene.clearPropPlacementPreview();
  };
  const bindCanvasPlacement = (target: HTMLCanvasElement) => {
    target.addEventListener("pointerdown", onCanvasPointerDown);
    target.addEventListener("pointermove", onCanvasPointerMove);
    target.addEventListener("pointerleave", onCanvasPointerLeave);
  };
  const unbindCanvasPlacement = (target: HTMLCanvasElement) => {
    target.removeEventListener("pointerdown", onCanvasPointerDown);
    target.removeEventListener("pointermove", onCanvasPointerMove);
    target.removeEventListener("pointerleave", onCanvasPointerLeave);
  };
  bindCanvasPlacement(canvas);
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

  interface SceneCandidate {
    canvas: HTMLCanvasElement;
    scene: SimScene;
    queue: LoadQueue;
    fingerprint: string;
  }

  // This must belong to the mounted stage. The page-global fingerprint is a
  // useful boot hint for the shell watcher, but it outlives a canvas when the
  // SPA remounts the stage and therefore cannot prove that this stage rendered
  // anything.
  let currentSceneFingerprint: string | null = null;
  const announceViewer = (fingerprint: string, ready: boolean, error?: string) => {
    document.dispatchEvent(
      new CustomEvent(VIEWER_STATE_EVENT, { detail: { fingerprint, ready, ...(error ? { error } : {}) } }),
    );
  };
  const markLoaded = (fingerprint: string) => {
    currentSceneFingerprint = fingerprint;
    const shared = globalThis as typeof globalThis & { __innateSimLoadedEnvironmentFingerprint?: string };
    shared.__innateSimLoadedEnvironmentFingerprint = fingerprint;
    announceViewer(fingerprint, true);
  };
  const loadSceneAssets = async (targetScene: SimScene, targetQueue: LoadQueue, strictEnvironment = false) => {
    const layout = await targetScene.loadApartmentLayout();
    targetScene.frameLayout(layout);
    const { done: robotDone } = await targetScene.loadRobot(targetQueue);
    const environmentDone = targetScene.streamApartment(targetQueue, layout, {
      strict: requiresCompleteEnvironment(layout.fingerprint, strictEnvironment),
    });
    await Promise.all([robotDone, environmentDone]);
    targetScene.markEnvironmentReady();
    targetScene.prefetchPropModels();
    return layout.fingerprint ?? null;
  };

  // One shared bounded queue drives real byte progress for the initial load;
  // later environments build with a separate hidden canvas and queue.
  let queue = new LoadQueue(2, ({ loaded, total }) => setProgress(loaded, total));
  queue.setEstimatedTotal(42e6);
  const swaps = new EnvironmentSwapCoordinator<SceneCandidate>();
  const desiredSwap = { key: "" };
  let latestTransition: EnvironmentTransition = { active: false, generation: 0, fingerprint: null };
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const buildScene = async (
    fingerprint: string,
    swapGeneration: number,
  ): Promise<SceneCandidate> => {
    let source: EnvironmentSource | null = null;
    let sourceAttempts = 0;
    while (!disposed && swaps.isCurrent(swapGeneration) && sourceAttempts < 20) {
      sourceAttempts += 1;
      try {
        const candidate = await resolveEnvironmentSource(undefined, undefined, 1);
        if (candidate.fingerprint === fingerprint) source = candidate;
      } catch {
        // The proxy is expected to disappear while the container restarts.
      }
      if (source) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    if (!source || disposed || !swaps.isCurrent(swapGeneration)) {
      throw new Error(
        disposed || !swaps.isCurrent(swapGeneration)
          ? "stale environment load"
          : "could not resolve the selected environment descriptor",
      );
    }

    const nextCanvas = document.createElement("canvas");
    nextCanvas.style.cssText = "width:100%;height:100%;display:block;position:absolute;inset:0;visibility:hidden";
    const nextQueue = new LoadQueue(2);
    nextQueue.setEstimatedTotal(42e6);
    const nextScene = new SimScene(nextCanvas, {
      fixedSize: { width: wrap.clientWidth || 1280, height: wrap.clientHeight || 720 },
      environmentSource: source,
    });
    nextScene.followCamera = true;
    try {
      const loadedFingerprint = await loadSceneAssets(nextScene, nextQueue, true);
      if (loadedFingerprint !== fingerprint) throw new Error("environment changed while its scene was loading");
      return { canvas: nextCanvas, scene: nextScene, queue: nextQueue, fingerprint };
    } catch (error) {
      nextQueue.cancel();
      nextScene.dispose();
      throw error;
    }
  };

  const requestSceneSwap = (fingerprint: string, transitionGeneration: number) => {
    const key = `${transitionGeneration}:${fingerprint}`;
    const claimedKey = claimEnvironmentSwapKey(desiredSwap.key, key);
    if (disposed || claimedKey === null) return;
    // Claim before announcing viewer state. dispatchEvent is synchronous: the
    // watcher immediately echoes a switch-state event, which must see this key
    // as already handled instead of recursively announcing readiness forever.
    desiredSwap.key = claimedKey;
    if (currentSceneFingerprint === fingerprint) {
      swaps.invalidate();
      announceViewer(fingerprint, true);
      return;
    }
    announceViewer(fingerprint, false);
    void swaps
      .replace(
        (swapGeneration) => buildScene(fingerprint, swapGeneration),
        (candidate) => {
          if (disposed || desiredSwap.key !== key) {
            candidate.queue.cancel();
            candidate.scene.dispose();
            return;
          }
          const oldCanvas = canvas;
          const oldScene = scene;
          const oldQueue = queue;
          bindCameraMode(candidate.scene);
          candidate.canvas.style.visibility = "";
          const width = wrap.clientWidth || 1280;
          const height = wrap.clientHeight || 720;
          // Upload textures and draw one complete candidate frame off-DOM. No
          // visible state changes until every fallible preparation step passes.
          prepareEnvironmentSwapScene(
            candidate.scene,
            (target) => session.tick(target, 0, { strictTrafficManifest: true }),
            {
              width,
              height,
              pixelRatio: Math.min(devicePixelRatio, 2),
              safeInsetRight,
              // The first tick may call spawnAt(), which deliberately cancels an
              // in-flight overview tween. The helper restores this afterwards.
              cameraMode: CAMERA_MODES[cameraMode],
              view: VIEW_FOR[session.primaryCamera] ?? "orbit",
            },
          );

          clearPlacementSelection();
          unbindCanvasPlacement(oldCanvas);
          oldCanvas.replaceWith(candidate.canvas);
          canvas = candidate.canvas;
          scene = candidate.scene;
          queue = candidate.queue;
          bindCanvasPlacement(canvas);
          try {
            oldQueue.cancel();
            oldScene.dispose();
          } catch (error) {
            console.warn("[sim-viewer] previous environment cleanup failed:", error);
          }
          markLoaded(candidate.fingerprint);
        },
        (candidate) => {
          candidate.queue.cancel();
          candidate.scene.dispose();
        },
      )
      .catch((error) => {
        if (disposed || desiredSwap.key !== key) return;
        if (retryTimer !== null) clearTimeout(retryTimer);
        retryTimer = deferEnvironmentSwapRetry(
          desiredSwap,
          key,
          () => announceViewer(fingerprint, false, error instanceof Error ? error.message : "scene load failed"),
          (callback, delayMs) =>
            setTimeout(() => {
              retryTimer = null;
              callback();
            }, delayMs),
          () =>
            latestTransition.active &&
            latestTransition.generation === transitionGeneration &&
            latestTransition.fingerprint === fingerprint,
          () => {
            retryTimer = null;
            requestSceneSwap(fingerprint, transitionGeneration);
          },
        );
      });
  };

  const onCatalogEvent = (event: Event) => {
    acceptEnvironmentCatalog((event as CustomEvent<EnvironmentCatalog>).detail);
  };
  const onSwitchState = (event: Event) => {
    const state = (event as CustomEvent<EnvironmentTransition>).detail;
    if (!state || typeof state.active !== "boolean") return;
    if (state.generation !== latestTransition.generation) {
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      desiredSwap.key = "";
      swaps.invalidate();
    }
    latestTransition = state;
    environmentSwitching = state.active;
    refreshEnvironmentApply();
    if (state.active && typeof state.fingerprint === "string" && state.fingerprint) {
      requestSceneSwap(state.fingerprint, state.generation);
    }
  };
  document.addEventListener(CATALOG_EVENT, onCatalogEvent);
  document.addEventListener(SWITCH_STATE_EVENT, onSwitchState);
  const unsubscribeEnvironment = session.onEnvironment((environment) => {
    document.dispatchEvent(new CustomEvent(WORLD_STATE_EVENT, { detail: environment }));
  });
  void fetch("/sim-environments.json", { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : null))
    .then((catalog) => {
      if (catalog) acceptEnvironmentCatalog(catalog as EnvironmentCatalog);
    })
    .catch(() => {});

  const initialScene = scene;
  const initialQueue = queue;
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
      setLoading("loading robot and apartment...");
      const fingerprint = await loadSceneAssets(initialScene, initialQueue);
      if (disposed || scene !== initialScene) return;
      hideLoading();
      if (fingerprint) markLoaded(fingerprint);
    } catch (err) {
      if (!disposed && scene === initialScene) session.stageError(err);
    }
  })();

  return {
    audioEl: null, // sim has no robot mic; the pages skip the mic toggle in sim mode
    setSafeInsets: (insets) => {
      safeInsetRight = Math.max(0, insets.right ?? 0);
      scene.setSafeInsets({ right: safeInsetRight });
    },
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
      safeInsetRight = 0;
      scene.setSafeInsets({ right: 0 }); // the agent dock's inset must not follow the stage
      clearPlacementSelection(); // an armed prop must not follow the pointer either
      wrap.remove();
    },
    destroy() {
      disposed = true;
      swaps.dispose();
      if (retryTimer !== null) clearTimeout(retryTimer);
      queue.cancel(); // stop pulling new downloads for a stage that's gone
      unsubscribe();
      unsubscribeProps();
      stopLoop();
      observer.disconnect();
      longTaskObserver?.disconnect();
      window.removeEventListener("pointerup", finishDrop);
      window.removeEventListener("pointercancel", cancelDrop);
      document.removeEventListener(PANEL_OPEN_EVENT, onPanelOpen);
      document.removeEventListener("pointerdown", onOutsidePointer, true);
      document.removeEventListener(CATALOG_EVENT, onCatalogEvent);
      document.removeEventListener(SWITCH_STATE_EVENT, onSwitchState);
      unsubscribeEnvironment();
      unbindCanvasPlacement(canvas);
      scene.dispose();
      wrap.remove();
    },
  };
}
