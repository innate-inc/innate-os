// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Nav page entry — a live view of everything navigation.
//
// Composition (one-directional: topics → store → views → store actions):
//   navStore.js        mode/maps/currentMap truth + the mode_manager actions
//   mapWidget (shared) the scene: grid, pose, route, scan/costmap/trail layers
//   mapsPanel.js       sidebar roster (switch/delete/map-free/new)
//   mappingSession.js  recording banner over the scene (finish → name → save)
//   panels.js          sensor readouts   plots.js  rolling strip charts
//   driveKit.js        teleop controls mounted only while mapping
//
// This module only composes: it owns the layout, the layer chips, the busy
// veil (mode/map changes block for tens of seconds — the page says so instead
// of going dead), and the one cross-cutting reaction — reshaping the page
// when mapping starts or ends, no matter which client started it.

import { mountPage } from "../pageMount.js";
import {
  AMCL_POSE_TOPIC,
  COMMANDED_GOAL_TOPIC,
  GLOBAL_COSTMAP_TOPIC,
  KEEPOUT_MASK_TOPIC,
  LOCAL_COSTMAP_TOPIC,
  MEMORY_POSITIONS_TOPIC,
  MEMORY_SEARCH_TOPIC,
  NAV_CURRENT_MAP_TOPIC,
  ODOM_TOPIC,
  PLAN_TOPICS,
  SCAN_TOPIC,
} from "../constants.js";
import { createMap, MAP_COLORS } from "../map/mapWidget.js";
import { createNavStore } from "./navStore.js";
import { createMapsPanel } from "./mapsPanel.js";
import { createMappingSession } from "./mappingSession.js";
import { createMemoriesPanel } from "./memoriesPanel.js";
import { createNavPanels } from "./panels.js";
import { createNavPlots } from "./plots.js";
import { createDriveKit } from "./driveKit.js";
import { dismissAllConfirms } from "./confirm.js";

// Scene default: robot-centred window, wheel-zoomable, not the
// fit-whole-grid mode — remembered across visits.
const ZOOM_KEY = "innate.navZoom";
const DEFAULT_ZOOM_M = 14;

// The mobile app's navigation glyph (DistanceIcon, a pin over its landing
// spot) — the page title and the current-map pill wear the same mark the
// phone uses for its map.
const PIN_ICON =
  '<svg viewBox="0 0 24 25" fill="currentColor" aria-hidden="true"><path d="M12 22C10.3103 22 8.92958 21.7577 7.85775 21.273C6.78592 20.7885 6.25 20.1623 6.25 19.3943C6.25 19.0904 6.35292 18.8052 6.55875 18.5385C6.76442 18.2718 7.057 18.0314 7.4365 17.8173C7.61217 17.7019 7.80383 17.6625 8.0115 17.699C8.21917 17.7355 8.38075 17.8416 8.49625 18.0173C8.61158 18.1929 8.64683 18.383 8.602 18.5875C8.55717 18.792 8.44692 18.9519 8.27125 19.0673C8.16225 19.1058 8.05808 19.1538 7.95875 19.2115C7.85925 19.2692 7.79992 19.3268 7.78075 19.3845C7.94608 19.6833 8.43808 19.9439 9.25675 20.1663C10.0752 20.3888 10.9897 20.5 12 20.5C13.0103 20.5 13.9247 20.3888 14.7432 20.1663C15.5619 19.9439 16.0539 19.6833 16.2193 19.3845C16.2001 19.3268 16.1408 19.2692 16.0413 19.2115C15.9419 19.1538 15.8378 19.1058 15.7288 19.0673C15.5531 18.9519 15.4428 18.792 15.398 18.5875C15.3532 18.383 15.3884 18.1929 15.5038 18.0173C15.6193 17.8416 15.7808 17.7355 15.9885 17.699C16.1962 17.6625 16.3878 17.7019 16.5635 17.8173C16.943 18.0314 17.2356 18.2718 17.4413 18.5385C17.6471 18.8052 17.75 19.0904 17.75 19.3943C17.75 20.1623 17.2141 20.7885 16.1423 21.273C15.0704 21.7577 13.6897 22 12 22ZM12.025 17.125C13.6813 15.8698 14.9246 14.6266 15.7548 13.3953C16.5849 12.1638 17 10.941 17 9.727C17 8.00133 16.4599 6.69875 15.3798 5.81925C14.2996 4.93975 13.173 4.5 12 4.5C10.8333 4.5 9.70833 4.93975 8.625 5.81925C7.54167 6.69875 7 8.00133 7 9.727C7 10.8628 7.40992 12.0398 8.22975 13.2578C9.04958 14.4758 10.3147 15.7648 12.025 17.125ZM12 18.5635C11.8192 18.5635 11.6384 18.5317 11.4577 18.4682C11.2769 18.4047 11.1096 18.3127 10.9558 18.1923C9.12375 16.7154 7.7565 15.2744 6.854 13.8693C5.95133 12.4641 5.5 11.0833 5.5 9.727C5.5 8.61417 5.6965 7.63917 6.0895 6.802C6.4825 5.96483 6.98983 5.26383 7.6115 4.699C8.23333 4.13433 8.9305 3.71 9.703 3.426C10.4753 3.142 11.241 3 12 3C12.759 3 13.5247 3.142 14.297 3.426C15.0695 3.71 15.7667 4.13433 16.3885 4.699C17.0102 5.26383 17.5175 5.96483 17.9105 6.802C18.3035 7.63917 18.5 8.61417 18.5 9.727C18.5 11.0833 18.0487 12.4641 17.146 13.8693C16.2435 15.2744 14.8763 16.7154 13.0443 18.1923C12.8904 18.3127 12.7231 18.4047 12.5423 18.4682C12.3616 18.5317 12.1808 18.5635 12 18.5635ZM12 11.3943C12.4987 11.3943 12.9246 11.2193 13.2777 10.8693C13.6311 10.5193 13.8077 10.0917 13.8077 9.5865C13.8077 9.08783 13.6311 8.66192 13.2777 8.30875C12.9246 7.95542 12.4987 7.77875 12 7.77875C11.5077 7.77875 11.0833 7.95542 10.727 8.30875C10.3705 8.66192 10.1923 9.08783 10.1923 9.5865C10.1923 10.0917 10.3705 10.5193 10.727 10.8693C11.0833 11.2193 11.5077 11.3943 12 11.3943Z"/></svg>';

/** @type {Array<{ key: import("../map/mapWidget.js").LayerName, label: string, on: boolean, topic: string }>} */
const LAYERS = [
  { key: "keepout", label: "Keepout zones", on: true, topic: KEEPOUT_MASK_TOPIC },
  { key: "scan", label: "LIDAR", on: true, topic: SCAN_TOPIC },
  { key: "costmap", label: "Global costmap", on: false, topic: GLOBAL_COSTMAP_TOPIC },
  { key: "local", label: "Local costmap", on: false, topic: LOCAL_COSTMAP_TOPIC },
  { key: "trail", label: "Path traveled", on: true, topic: ODOM_TOPIC },
  { key: "memories", label: "Memories", on: true, topic: MEMORY_POSITIONS_TOPIC },
];

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "nav-page", buildView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  // ---- layout --------------------------------------------------------------
  const head = document.createElement("div");
  head.className = "page-head";
  const heading = document.createElement("h1");
  heading.className = "page-title page-title-iconed";
  heading.innerHTML = `${PIN_ICON}Nav`;
  const chips = document.createElement("div");
  chips.className = "layer-chips";
  head.append(heading, chips);

  const grid = document.createElement("div");
  grid.className = "nav-grid";
  const scene = document.createElement("div");
  scene.className = "nav-scene";
  const side = document.createElement("aside");
  side.className = "nav-side";
  grid.append(scene, side);
  root.append(head, grid);

  // Blocking feedback while a mode/map change is in flight (they bring whole
  // Nav2 lifecycle stacks up and down — up to a minute).
  const veil = document.createElement("div");
  veil.className = "nav-veil";
  veil.hidden = true;
  const veilText = document.createElement("span");
  veilText.className = "nav-veil-text mono";
  veil.appendChild(veilText);
  scene.appendChild(veil);

  // Map-free runs Nav2 without map_server or AMCL, so the scene has no map
  // behind it — the widget either waits on /map forever or keeps drawing the
  // previously loaded grid. Say so rather than let either state mislead.
  const mapfreeNote = document.createElement("div");
  mapfreeNote.className = "nav-scene-note mono";
  mapfreeNote.textContent = "map-free mode — no map is loaded; the scene may show the last map, which is not in use";
  mapfreeNote.hidden = true;
  scene.appendChild(mapfreeNote);

  // Current-map pill (bottom-left) — the map-name pill the mobile app pins to
  // its expanded map, same glyph.
  const mapPill = document.createElement("div");
  mapPill.className = "map-name-pill mono";
  mapPill.title = NAV_CURRENT_MAP_TOPIC;
  mapPill.innerHTML = PIN_ICON;
  const mapPillName = document.createElement("span");
  mapPill.appendChild(mapPillName);
  scene.appendChild(mapPill);

  // ---- store + scene ---------------------------------------------------------
  const store = createNavStore();

  const savedZoom = Number(localStorage.getItem(ZOOM_KEY));
  const map = createMap(scene, {
    zoom: savedZoom > 0 ? savedZoom : DEFAULT_ZOOM_M,
    onZoomChange: (m) => localStorage.setItem(ZOOM_KEY, String(m)),
    layers: Object.fromEntries(LAYERS.map(({ key, on }) => [key, on])),
    keepoutEditing: true,
  });

  // ---- layer chips -----------------------------------------------------------
  /** @type {Map<string, HTMLButtonElement>} */
  const chipEls = new Map();
  for (const { key, label, on, topic } of LAYERS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `layer-chip${on ? " is-on" : ""}`;
    chip.textContent = label;
    chip.title = topic;
    chip.addEventListener("click", () => {
      const next = !chip.classList.contains("is-on");
      chip.classList.toggle("is-on", next);
      map.setLayer(key, next);
      legend.sync();
    });
    chipEls.set(key, chip);
    chips.appendChild(chip);
  }

  // Toggling the trail chip only hides/shows — this is the one way to wipe it.
  const clearTrailBtn = document.createElement("button");
  clearTrailBtn.type = "button";
  clearTrailBtn.className = "layer-chip layer-chip-action";
  clearTrailBtn.textContent = "Clear path";
  clearTrailBtn.title = "Erase the traveled-path line";
  clearTrailBtn.addEventListener("click", () => map.clearTrail());
  chips.appendChild(clearTrailBtn);

  const legend = createLegend(scene, chipEls);

  /** @param {string} key @param {boolean} on */
  function forceLayer(key, on) {
    const chip = chipEls.get(key);
    if (chip) chip.classList.toggle("is-on", on);
    map.setLayer(/** @type {import("../map/mapWidget.js").LayerName} */ (key), on);
    legend.sync();
  }

  // ---- mapping reaction --------------------------------------------------------
  // Mapping reshapes the page: the scene shows only the growing map + live
  // scan (the costmap belongs to the previous map; chips lock so the view
  // can't drift mid-recording), the widget swaps to /mapping_pose, and the
  // teleop drive kit mounts — you drive the robot to build the map.
  /** @type {Record<string, boolean> | null} chip states to restore after mapping */
  let savedLayers = null;
  /** @type {{ destroy: () => void } | null} */
  let driveKit = null;
  let kitGen = 0; // guards the async kit mount against a fast mapping exit

  /** @param {boolean} mapping */
  function onMappingChange(mapping) {
    map.setMappingMode(mapping);
    legend.setHidden(mapping); // the drive kit's overlays own the scene corners
    mapPill.hidden = mapping;
    const gen = ++kitGen;
    clearTrailBtn.disabled = mapping;
    if (mapping) {
      savedLayers = {};
      for (const [key, chip] of chipEls) {
        savedLayers[key] = chip.classList.contains("is-on");
        chip.disabled = true;
      }
      forceLayer("scan", true);
      forceLayer("costmap", false);
      forceLayer("trail", false);
      forceLayer("memories", true); // the tour records them; watching them land is the point
      createDriveKit(scene).then((kit) => {
        if (gen !== kitGen) kit.destroy(); // mapping ended while mounting
        else driveKit = kit;
      });
    } else {
      driveKit?.destroy();
      driveKit = null;
      for (const [key, chip] of chipEls) {
        chip.disabled = false;
        if (savedLayers) forceLayer(key, savedLayers[key]);
      }
      savedLayers = null;
    }
  }

  let wasMapping = false;
  let lastMap = store.state.currentMap;
  const unsubStore = store.onChange((s) => {
    veil.hidden = !s.busy;
    if (s.busy) veilText.textContent = `${s.busy}…`;
    mapfreeNote.hidden = s.mode !== "mapfree";
    const noMap = !s.currentMap || s.mode === "mapfree";
    mapPill.classList.toggle("is-empty", noMap);
    mapPillName.textContent = noMap ? "no map" : s.currentMap;
    const mapping = s.mode === "mapping";
    if (mapping !== wasMapping) {
      wasMapping = mapping;
      onMappingChange(mapping);
    }
    if (s.currentMap !== lastMap) {
      // Trail and AMCL anchor drawn against the previous map are nonsense in
      // the new frame — reset both.
      if (lastMap) map.mapChanged();
      lastMap = s.currentMap;
    }
  });

  // ---- sidebar -----------------------------------------------------------------
  // Maps (the interactive panel) on top, then numeric readouts, then the
  // rolling plots. Each in its own host so no module's teardown can clear
  // another's DOM.
  const mapsHost = document.createElement("div");
  const memoriesHost = document.createElement("div");
  const readoutHost = document.createElement("div");
  const plotHost = document.createElement("div");
  side.append(mapsHost, memoriesHost, readoutHost, plotHost);

  const parts = [
    createMapsPanel(mapsHost, store),
    createMemoriesPanel(memoriesHost, map),
    createMappingSession(scene, store),
    createNavPanels(readoutHost, store),
    createNavPlots(plotHost),
  ];

  return {
    destroy() {
      // Dialogs live on document.body, not under root — sweep them or a
      // confirm orphaned by navigation floats over the next page.
      dismissAllConfirms();
      kitGen++; // cancel any in-flight drive-kit mount
      driveKit?.destroy();
      for (const part of parts) part.destroy();
      unsubStore();
      store.destroy();
      legend.destroy();
      map.destroy();
      root.innerHTML = "";
    },
  };
}

/**
 * Scene-corner legend for the map's marks, using the widget's own palette.
 * Layer-bound rows follow their chips; robot/goal/route are always drawn by
 * the widget so their rows always show.
 * @param {HTMLElement} scene
 * @param {Map<string, HTMLButtonElement>} chipEls
 * @returns {{ sync: () => void, setHidden: (hidden: boolean) => void, destroy: () => void }}
 */
function createLegend(scene, chipEls) {
  const el = document.createElement("div");
  el.className = "map-legend mono";
  /** @type {Array<{ keys: string[] | null, row: HTMLElement }>} */
  const rows = [];

  /** @param {string[] | null} keys chips gating this row (null = always) @param {string} swatch @param {string} label @param {string} topic tooltip: where the mark's data comes from */
  function row(keys, swatch, label, topic) {
    const r = document.createElement("div");
    r.className = "legend-row";
    r.innerHTML = `${swatch}<span>${label}</span>`;
    r.title = topic;
    el.appendChild(r);
    rows.push({ keys, row: r });
  }
  /** @param {string} color */
  const dot = (color) => `<span class="legend-swatch legend-dot" style="background:${color}"></span>`;
  /** @param {string} color */
  const line = (color) => `<span class="legend-swatch legend-line" style="background:${color}"></span>`;

  row(null, dot(MAP_COLORS.robot), "robot", `${AMCL_POSE_TOPIC} + ${ODOM_TOPIC}`);
  row(null, dot(MAP_COLORS.goal), "goal", COMMANDED_GOAL_TOPIC);
  row(null, line(MAP_COLORS.route), "planned path", PLAN_TOPICS.join(" · "));
  row(["scan"], dot(MAP_COLORS.scan), "lidar", SCAN_TOPIC);
  row(["trail"], line(MAP_COLORS.trail), "path traveled", ODOM_TOPIC);
  row(["memories"], dot(MAP_COLORS.memory), "remembered view", `${MEMORY_POSITIONS_TOPIC} · ${MEMORY_SEARCH_TOPIC}`);
  row(["keepout"], dot(MAP_COLORS.keepout), "keepout zone", KEEPOUT_MASK_TOPIC);
  row(["costmap", "local"], '<span class="legend-swatch legend-cost"></span>', "cost low → lethal", `${GLOBAL_COSTMAP_TOPIC} · ${LOCAL_COSTMAP_TOPIC}`);

  function sync() {
    for (const { keys, row: r } of rows) {
      if (!keys) continue;
      r.hidden = !keys.some((k) => chipEls.get(k)?.classList.contains("is-on"));
    }
  }
  sync();
  scene.appendChild(el);

  return {
    sync,
    /** @param {boolean} hidden hide wholesale (mapping mode — the drive kit owns the corners) */
    setHidden(hidden) {
      el.hidden = hidden;
    },
    destroy() {
      el.remove();
    },
  };
}
