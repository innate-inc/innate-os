// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Maps panel — the sidebar roster, a pure view of the nav store: list maps,
// switch the active one, delete, toggle map-free, start a new recording.
// Every mutation goes through a store action behind an in-app confirm (all
// four are disruptive: they drop localization, delete files, or stop
// navigation). Any map can be deleted, including the one in use — the store
// drops to map-free first. The roster locks while a recording is in progress.

import { NAV_AVAILABLE_MAPS_TOPIC } from "../constants.js";
import { mapOrientation, squaringRotation, wallOutline } from "../map/orientation.js";
import { confirmDialog } from "./confirm.js";

const PREVIEW_MARGIN_PX = 8;
// The hover card is fixed-width, so a landscape map keeps it short.
const PREVIEW_ASPECT = 1.5;

/**
 * @param {HTMLElement} host
 * @param {ReturnType<typeof import("./navStore.js").createNavStore>} store
 * @returns {{ destroy: () => void }}
 */
export function createMapsPanel(host, store) {
  const section = document.createElement("section");
  section.className = "nav-panel";

  const head = document.createElement("div");
  head.className = "telemetry-head";
  const label = document.createElement("p");
  label.className = "microlabel";
  label.textContent = "Maps";
  label.title = NAV_AVAILABLE_MAPS_TOPIC;
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "maps-new";
  newBtn.textContent = "+ New map";
  head.append(label, newBtn);

  const list = document.createElement("div");
  list.className = "maps-list";

  const mapfreeRow = document.createElement("label");
  mapfreeRow.className = "nav-row maps-mapfree";
  const mapfreeLabel = document.createElement("span");
  mapfreeLabel.className = "nav-row-label";
  mapfreeLabel.textContent = "map-free mode";
  const mapfreeToggle = document.createElement("input");
  mapfreeToggle.type = "checkbox";
  mapfreeRow.append(mapfreeLabel, mapfreeToggle);

  // Result line: the outcome of the last action (in-flight feedback lives on
  // the page's scene veil, not here).
  const status = document.createElement("div");
  status.className = "maps-status mono";
  status.hidden = true;

  section.append(head, list, mapfreeRow, status);
  host.appendChild(section);

  newBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Record a new map?",
      body: "The robot leaves navigation mode and you drive it around to cover the space. Current navigation stops.",
      confirmLabel: "Start mapping",
    });
    if (ok) await store.startMapping();
  });

  mapfreeToggle.addEventListener("change", async () => {
    const toMapfree = mapfreeToggle.checked;
    if (!toMapfree && store.state.maps.length === 0) {
      render(store.state); // snap the toggle back
      return;
    }
    // mode_manager persists the last map server-side, so returning to
    // navigation reloads it — no client bookkeeping.
    const ok = await store.changeMode(toMapfree ? "mapfree" : "navigation");
    if (!ok) render(store.state);
  });

  /** @param {import("./navStore.js").NavState} s */
  function render(s) {
    const mapping = s.mode === "mapping";
    newBtn.disabled = !!s.busy || mapping;
    mapfreeToggle.checked = s.mode === "mapfree";
    mapfreeToggle.disabled = !!s.busy || mapping || (s.mode !== "mapfree" && s.maps.length === 0);

    status.hidden = !s.status;
    if (s.status) {
      status.dataset.kind = s.status.kind;
      status.textContent = s.status.text;
    }

    list.innerHTML = "";
    if (mapping) {
      const note = document.createElement("div");
      note.className = "maps-empty";
      note.textContent = "Recording — finish or discard on the map.";
      list.appendChild(note);
      return;
    }
    if (s.maps.length === 0) {
      const empty = document.createElement("div");
      empty.className = "maps-empty";
      empty.textContent = "No maps yet — record one to navigate.";
      list.appendChild(empty);
      return;
    }
    for (const name of s.maps) {
      const row = document.createElement("div");
      row.className = "maps-row";
      const active = name === s.currentMap;
      if (active) row.classList.add("is-active");

      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "maps-pick";
      pick.disabled = !!s.busy || active;
      pick.title = active ? "Active map" : `Switch to ${name}`;
      const dot = document.createElement("span");
      dot.className = "maps-dot";
      const text = document.createElement("span");
      text.className = "maps-name mono";
      text.textContent = name;
      pick.append(dot, text);
      pick.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Switch map?",
          body: `The robot switches to ${name} and will need to re-localize.`,
          confirmLabel: "Switch",
        });
        if (ok) await store.changeMap(name);
      });

      // Hover preview: a floating render of the saved map (from the proxy's
      // /map/preview), so you can tell maps apart without switching. Fixed
      // positioning escapes the sidebar's clipping; hover-only, so touch
      // clients simply never see it.
      const preview = document.createElement("div");
      preview.className = "maps-preview";
      preview.hidden = true;
      row.addEventListener("mouseenter", () => {
        if (!preview.firstChild) {
          const canvas = document.createElement("canvas");
          canvas.setAttribute("role", "img");
          canvas.setAttribute("aria-label", `Preview of ${name}`);
          const img = new Image();
          img.addEventListener("load", () => drawSquared(canvas, img));
          img.addEventListener("error", () => {
            canvas.remove();
            preview.textContent = "no preview";
          });
          img.src = `/map/preview?name=${encodeURIComponent(name)}`;
          preview.appendChild(canvas);
        }
        const r = row.getBoundingClientRect();
        preview.style.right = `${window.innerWidth - r.left + 10}px`;
        preview.style.top = `${Math.max(12, Math.min(r.top - 60, window.innerHeight - 260))}px`;
        preview.hidden = false;
      });
      row.addEventListener("mouseleave", () => {
        preview.hidden = true;
      });

      const del = document.createElement("button");
      del.type = "button";
      del.className = "maps-delete";
      del.innerHTML =
        '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 8.5h5.6l.7-8.5"/></svg>';
      del.disabled = !!s.busy;
      del.title = `Delete ${name}`;
      del.addEventListener("click", async () => {
        const deletingActive = name === store.state.currentMap && store.state.mode === "navigation";
        const ok = await confirmDialog({
          title: "Delete map?",
          body: deletingActive
            ? `${name} is the map in use — the robot switches to map-free mode, then the map is deleted. This cannot be undone.`
            : `${name} is deleted from the robot. This cannot be undone.`,
          confirmLabel: "Delete",
          danger: true,
        });
        if (ok) await store.deleteMap(name);
      });

      row.append(pick, del, preview);
      list.appendChild(row);
    }
  }

  const unsub = store.onChange(render);

  return {
    destroy() {
      unsub();
      section.remove();
    },
  };
}

/** A saved map's preview, squared like the live map (see map/orientation.js)
 * and cropped to its walls.
 * @param {HTMLCanvasElement} canvas @param {HTMLImageElement} img */
function drawSquared(canvas, img) {
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  const source = document.createElement("canvas");
  source.width = w;
  source.height = h;
  const sourceCtx = source.getContext("2d");
  const ctx = canvas.getContext("2d");
  if (!sourceCtx || !ctx) return;
  sourceCtx.drawImage(img, 0, 0);
  // map_saver's trinary PGM: walls 0, unknown 205, free 254.
  const gray = sourceCtx.getImageData(0, 0, w, h).data;
  const cells = new Int8Array(w * h);
  for (let row = 0; row < h; row++) {
    for (let col = 0; col < w; col++) {
      if (gray[(row * w + col) * 4] < 128) cells[(h - 1 - row) * w + col] = 100; // grid rows run bottom-up
    }
  }
  const orientation = mapOrientation(cells, w, h);
  const rot = squaringRotation(orientation, PREVIEW_ASPECT);
  const c = Math.cos(rot);
  const s = Math.sin(rot);
  const corners = wallOutline(orientation, PREVIEW_MARGIN_PX).map(({ x, y }) => ({
    x: x * c - (h - y) * s,
    y: x * s + (h - y) * c,
  }));
  const left = Math.min(...corners.map((p) => p.x));
  const top = Math.min(...corners.map((p) => p.y));
  canvas.width = Math.ceil(Math.max(...corners.map((p) => p.x)) - left);
  canvas.height = Math.ceil(Math.max(...corners.map((p) => p.y)) - top);
  ctx.fillStyle = "rgb(205 205 205)";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.translate(-left, -top);
  ctx.rotate(rot);
  ctx.drawImage(img, 0, 0);
}
