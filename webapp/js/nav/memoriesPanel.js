// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// "Memories" sidebar panel — the robot's visual memory of the current map made
// tangible: a count, a recall-cache health chip, and a reel of remembered
// views (newest first). Hovering a thumbnail lights its viewpoint on the map;
// clicking centres the map there and opens the Go-here popup. Data comes from
// the two memory topics; the images ride same-origin HTTP (/memory/image), so
// the reel costs no rosbridge bandwidth.

import { ros } from "../rosClient.js";
import { CLEAR_MEMORIES_SERVICE, MEMORY_POSITIONS_TOPIC, MEMORY_SEARCH_TOPIC } from "../constants.js";
import { SEARCH_REPLAY_FRESH_S, ageText, cacheLabel, memoryImageUrl, parseMemories, parseSearch } from "../map/memories.js";

// Age labels ("5 min ago") drift while nothing else changes — refresh slowly.
const AGE_REFRESH_MS = 30_000;

/**
 * @param {HTMLElement} root sidebar host.
 * @param {{ highlightMemory: (id: number | null) => void, focusMemory: (id: number) => void, robotNowS: () => number }} map
 *   the scene widget — the reel drives its memory highlight/focus.
 * @returns {{ destroy: () => void }}
 */
export function createMemoriesPanel(root, map) {
  const section = document.createElement("section");
  section.className = "nav-panel mem-panel";

  const head = document.createElement("div");
  head.className = "mem-panel-head";
  const label = document.createElement("p");
  label.className = "microlabel";
  label.textContent = "Memories";
  label.title = MEMORY_POSITIONS_TOPIC;
  const count = document.createElement("span");
  count.className = "mem-panel-count mono";
  count.textContent = "—";
  const chip = document.createElement("span");
  chip.className = "mem-panel-chip mono";
  chip.hidden = true;
  chip.title = "Whether the remembered views are pre-cached with the vision model (searches answer in ~a second)";
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "mem-panel-clear mono";
  clearBtn.hidden = true;
  clearBtn.textContent = "forget all";
  clearBtn.title = "Forget every remembered view on this map";
  clearBtn.addEventListener("click", () => {
    const n = count.textContent;
    if (!window.confirm(`Forget all ${n} remembered views on this map? The robot rebuilds them as it drives around.`)) {
      return;
    }
    clearBtn.disabled = true;
    ros
      .callService(CLEAR_MEMORIES_SERVICE)
      .catch(() => {}) // the reel simply not emptying is the failure signal
      .finally(() => {
        clearBtn.disabled = false;
      });
  });
  head.append(label, count, chip, clearBtn);
  section.appendChild(head);

  const empty = document.createElement("p");
  empty.className = "mem-panel-empty";
  empty.textContent =
    "Nothing remembered on this map yet — drive around while localized and the robot will remember what it sees.";
  section.appendChild(empty);

  const reel = document.createElement("div");
  reel.className = "mem-reel";
  reel.hidden = true;
  section.appendChild(reel);

  const lastSearch = document.createElement("p");
  lastSearch.className = "mem-panel-search mono";
  lastSearch.title = `the most recent recall verdict — ${MEMORY_SEARCH_TOPIC}`;
  lastSearch.hidden = true;
  section.appendChild(lastSearch);

  root.appendChild(section);

  let signature = ""; // map + ids + stamps of the rendered reel; rebuild only on change
  const robotNowS = () => map.robotNowS(); // stamps are robot time; the widget owns the clock

  /** @param {import("../map/memories.js").MemoryState} state */
  function renderReel(state) {
    map.highlightMemory(null); // a hovered thumb may be about to disappear
    reel.innerHTML = "";
    const newestFirst = [...state.memories].sort((a, b) => b.stamp - a.stamp);
    for (const m of newestFirst) {
      const thumb = document.createElement("button");
      thumb.type = "button";
      thumb.className = "mem-thumb";
      thumb.title = `x ${m.x.toFixed(2)} m, y ${m.y.toFixed(2)} m — click to show on the map`;
      const img = new Image();
      img.className = "mem-thumb-img";
      img.loading = "lazy";
      img.alt = "remembered view";
      img.src = memoryImageUrl(state.map, m);
      const age = document.createElement("span");
      age.className = "mem-thumb-age mono";
      age.dataset.stamp = String(m.stamp);
      age.textContent = ageText(m.stamp, robotNowS());
      thumb.append(img, age);
      thumb.addEventListener("mouseenter", () => map.highlightMemory(m.id));
      thumb.addEventListener("mouseleave", () => map.highlightMemory(null));
      thumb.addEventListener("click", () => map.focusMemory(m.id));
      reel.appendChild(thumb);
    }
  }

  function refreshAges() {
    for (const el of section.querySelectorAll(".mem-thumb-age")) {
      const stamp = Number(/** @type {HTMLElement} */ (el).dataset.stamp);
      if (stamp) el.textContent = ageText(stamp, robotNowS());
    }
  }
  const ageTimer = setInterval(refreshAges, AGE_REFRESH_MS);

  const unsubPositions = ros.subscribe(
    MEMORY_POSITIONS_TOPIC,
    (msg) => {
      const state = parseMemories(msg);
      if (!state) return;
      count.textContent = String(state.memories.length);
      const cache = cacheLabel(state.cache);
      chip.hidden = state.memories.length === 0;
      chip.textContent = cache.text;
      chip.dataset.kind = cache.kind;
      clearBtn.hidden = state.memories.length === 0;
      empty.hidden = state.memories.length > 0;
      reel.hidden = state.memories.length === 0;
      const sig = `${state.map}|${state.memories.map((m) => `${m.id}:${m.stamp}`).join(",")}`;
      if (sig !== signature) {
        signature = sig;
        renderReel(state);
      }
    },
    0,
    "std_msgs/msg/String",
  );

  // Same replay policy as the map layer: the first message after subscribing
  // is the latched history — shown only while fresh, so a page opened hours
  // later doesn't advertise an old recall as news.
  let searchSeen = false;
  const unsubSearch = ros.subscribe(
    MEMORY_SEARCH_TOPIC,
    (msg) => {
      const verdict = parseSearch(msg);
      if (!verdict) return;
      const replay = !searchSeen;
      searchSeen = true;
      if (replay && robotNowS() - verdict.stamp > SEARCH_REPLAY_FRESH_S) return;
      lastSearch.hidden = false;
      const outcome = verdict.found ? "found" : verdict.error ? "failed" : "no match";
      lastSearch.textContent = `last recall · “${verdict.query}” → ${outcome}`;
    },
    0,
    "std_msgs/msg/String",
  );

  return {
    destroy() {
      clearInterval(ageTimer);
      unsubPositions();
      unsubSearch();
      map.highlightMemory(null);
      section.remove();
    },
  };
}
