// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pure helpers shared by the spatial-memory map layer and sidebar panel:
// payload parsing, /memory/image URLs, age formatting, and the layer's color
// math. No DOM, no ROS — node-testable (tests/memories.test.js).

/** The memory layer's hue — violet, deliberately distinct from every other map
 * mark (robot pink, goal green, route blue, scan salmon, costmap amber/red). */
export const MEMORY_COLOR = "#b48cff";

/** A latched search verdict older than this is history, not news. Only the
 * first (replay) message after subscribing is stamp-gated — the robot's wall
 * clock can disagree with the browser's (no RTC before NTP settles), so live
 * arrivals always show. */
export const SEARCH_REPLAY_FRESH_S = 20;

/** @typedef {{ id: number, x: number, y: number, theta: number, stamp: number }} Memory */
/** @typedef {{ map: string, fingerprint: string, cache: string, memories: Memory[] }} MemoryState */

/**
 * Parse a /brain/memory_positions message. Null when malformed — the layer
 * just keeps its last good state.
 * @param {any} msg std_msgs/String @returns {MemoryState | null}
 */
export function parseMemories(msg) {
  try {
    const data = JSON.parse(msg?.data ?? "");
    if (!Array.isArray(data?.positions)) return null;
    /** @type {Memory[]} */
    const memories = [];
    for (const p of data.positions) {
      if (typeof p?.id !== "number" || typeof p?.x !== "number" || typeof p?.y !== "number") continue;
      memories.push({ id: p.id, x: p.x, y: p.y, theta: typeof p.theta === "number" ? p.theta : 0, stamp: typeof p.stamp === "number" ? p.stamp : 0 });
    }
    return {
      map: typeof data.map === "string" ? data.map : "",
      // Identity of the map's content: a same-name re-map wipes the memories
      // and restarts ids, and this is the only field that changes with it.
      fingerprint: typeof data.fingerprint === "string" ? data.fingerprint : "",
      cache: typeof data.cache === "string" ? data.cache : "off",
      memories,
    };
  } catch {
    return null;
  }
}

/**
 * Parse a /brain/memory_search verdict. Null when malformed.
 * @param {any} msg std_msgs/String
 * @returns {{ query: string, found: boolean, stamp: number, id?: number, x?: number, y?: number, theta?: number, seen_stamp?: number, explanation?: string, error?: string, latency_sec?: number, cached?: boolean } | null}
 */
export function parseSearch(msg) {
  try {
    const data = JSON.parse(msg?.data ?? "");
    if (typeof data?.query !== "string" || typeof data?.stamp !== "number") return null;
    return data;
  } catch {
    return null;
  }
}

/**
 * Seconds to add to the browser's clock to get the robot's — stamps are robot
 * time, and a robot without NTP can sit hours off. Learned from any *streaming*
 * stamped message (odom): a latched topic can't teach the clock, because its
 * replay arrives arbitrarily long after publish. Null when there's no stamp.
 * @param {any} msg any ROS message with a header.stamp
 * @returns {number | null}
 */
export function headerSkew(msg) {
  const stamp = msg?.header?.stamp;
  if (typeof stamp?.sec !== "number") return null;
  return stamp.sec + (stamp.nanosec ?? 0) / 1e9 - Date.now() / 1000;
}

/**
 * Same-origin URL of a memory's JPEG. The stamp rides along as a cache-buster:
 * a refreshed viewpoint overwrites its file in place, so the URL changes
 * exactly when the pixels do and the browser may cache hard.
 * @param {string} map the payload's map name ("Home.yaml") @param {Memory} memory
 */
export function memoryImageUrl(map, memory) {
  return `/memory/image?map=${encodeURIComponent(map)}&id=${memory.id}&v=${Math.round(memory.stamp)}`;
}

/**
 * "just now" → "5 min ago" → "3 h ago" → "2 d ago".
 * @param {number} stamp epoch seconds @param {number} [now] epoch seconds (tests)
 */
export function ageText(stamp, now = Date.now() / 1000) {
  const s = Math.max(0, now - stamp);
  if (s < 90) return "just now";
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/**
 * Draw opacity for a memory's marks: fresh memories glow, old ones recede but
 * never vanish. Full for the first 10 minutes, easing to a 0.35 floor by 24 h
 * (fast early fade, long tail — the difference between "this drive" and
 * "yesterday" matters more than 3 d vs 4 d).
 * @param {number} stamp epoch seconds @param {number} [now] epoch seconds (tests)
 */
export function ageAlpha(stamp, now = Date.now() / 1000) {
  const s = Math.max(0, now - stamp);
  const FULL_S = 600;
  const FLOOR_S = 86400;
  const FLOOR = 0.35;
  if (s <= FULL_S) return 1;
  if (s >= FLOOR_S) return FLOOR;
  return 1 - (1 - FLOOR) * Math.sqrt((s - FULL_S) / (FLOOR_S - FULL_S));
}

/**
 * A hex color at an alpha, as a canvas/CSS color string.
 * @param {string} hex "#rrggbb" @param {number} alpha 0–1 (clamped)
 */
export function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  const a = Math.max(0, Math.min(1, alpha));
  return `rgb(${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255} / ${a})`;
}

/**
 * Chip copy for the payload's cache state — how the next recall will feel.
 * "cold" self-heals (the robot re-warms ~30 s after recording settles), so it
 * reads as in-progress, not as an error.
 * @param {string} state @returns {{ text: string, kind: "ok" | "warn" | "muted" }}
 */
export function cacheLabel(state) {
  switch (state) {
    case "warm":
      return { text: "instant recall ready", kind: "ok" };
    case "inline":
      return { text: "recall ready", kind: "ok" };
    case "cold":
      return { text: "recall warming…", kind: "warn" };
    case "unsupported":
      return { text: "recall uncached", kind: "warn" };
    default:
      return { text: "recall offline", kind: "muted" };
  }
}
