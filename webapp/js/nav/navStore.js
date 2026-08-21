// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Nav store — the single source of truth for navigation mode and map state,
// and the only module that calls the mode_manager services. Ports the mobile
// app's MapDataContext to the webapp: views (maps panel, mapping banner, the
// page itself) render from the store and call its actions; robot truth flows
// in exclusively via the /nav/* topics, so a change made by any client — the
// mobile app included — updates every view here identically.
//
// Actions are serialized: mode/map changes drive whole Nav2 lifecycle stacks
// up and down (tens of seconds), and mode_manager itself rejects overlapping
// requests. While one is in flight, `busy` carries its label so the page can
// show blocking feedback instead of a dead UI.

import { ros } from "../rosClient.js";
import {
  NAV_AVAILABLE_MAPS_TOPIC,
  NAV_CHANGE_MAP_SERVICE,
  NAV_CHANGE_MODE_SERVICE,
  CANCEL_SKILL_SERVICE,
  NAV_CURRENT_MAP_TOPIC,
  NAV_CURRENT_MODE_TOPIC,
  NAV_DELETE_MAP_SERVICE,
  NAV_SAVE_MAP_SERVICE,
} from "../constants.js";

const MODE_CHANGE_TIMEOUT_MS = 60_000;
// Older robot images may not expose the cross-client skill-cancel service.
// Keep this short and best-effort so a missing brain never blocks nav controls.
const SKILL_CANCEL_TIMEOUT_MS = 2_000;
// mode_manager's own save_map validation, mirrored for inline feedback: it
// strips _ and - then requires isalnum(), so at least one alphanumeric is
// needed — "___" is rejected server-side.
export const MAP_NAME_RE = /^[A-Za-z0-9_-]*[A-Za-z0-9][A-Za-z0-9_-]*$/;

/**
 * @typedef {{
 *   mode: string,
 *   maps: string[],
 *   currentMap: string,
 *   busy: string | null,
 *   status: { kind: "ok" | "fail", text: string } | null,
 * }} NavState
 */

/**
 * @returns {{
 *   state: NavState,
 *   onChange: (cb: (state: NavState) => void) => () => void,
 *   startMapping: () => Promise<boolean>,
 *   changeMode: (mode: string) => Promise<boolean>,
 *   changeMap: (name: string) => Promise<boolean>,
 *   deleteMap: (name: string) => Promise<boolean>,
 *   saveAndActivate: (name: string, overwrite: boolean) => Promise<boolean>,
 *   destroy: () => void,
 * }}
 * @param {import("../rosClient.js").RosClient} [rosClient]
 */
export function createNavStore(rosClient = ros) {
  /** @type {NavState} */
  const state = { mode: "", maps: [], currentMap: "", busy: null, status: null };
  /** @type {Set<(state: NavState) => void>} */
  const listeners = new Set();
  let destroyed = false;

  function emit() {
    for (const cb of [...listeners]) cb(state);
  }

  /** @param {Partial<NavState>} patch */
  function set(patch) {
    Object.assign(state, patch);
    emit();
  }

  async function cancelActiveSkillBestEffort() {
    try {
      // Trigger success=false simply means there was no active skill. Either
      // way it is safe to continue with the requested nav operation.
      await rosClient.callService(CANCEL_SKILL_SERVICE, {}, SKILL_CANCEL_TIMEOUT_MS);
    } catch (err) {
      console.warn("Could not cancel the active brain skill before nav change:", err);
    }
  }

  /**
   * One serialized service call: publishes its label via `busy`, its outcome
   * via `status` (failures keep mode_manager's message — e.g. "A mode or map
   * change is in progress").
   * @param {string} service
   * @param {object} args
   * @param {string} doing
   * @param {{ cancelSkill?: boolean }} [options]
   * @returns {Promise<boolean>}
   */
  async function call(service, args, doing, { cancelSkill = false } = {}) {
    // destroyed: the page is gone, but a caller may still resolve later (a
    // confirm dialog orphaned by navigation) — never command the robot then.
    if (destroyed || state.busy) return false;
    set({ busy: doing, status: null });
    try {
      if (cancelSkill) await cancelActiveSkillBestEffort();
      if (destroyed) return false;
      const res = await rosClient.callService(service, args, MODE_CHANGE_TIMEOUT_MS);
      if (destroyed) return false;
      const ok = !!res?.success;
      if (!ok) set({ status: { kind: "fail", text: res?.message || `${doing} failed` } });
      return ok;
    } catch (err) {
      if (!destroyed) {
        set({ status: { kind: "fail", text: `${doing} failed — ${err instanceof Error ? err.message : String(err)}` } });
      }
      return false;
    } finally {
      if (!destroyed) set({ busy: null });
    }
  }

  const unsubs = [
    rosClient.subscribe(NAV_AVAILABLE_MAPS_TOPIC, (msg) => {
      if (typeof msg?.data !== "string") return;
      try {
        const parsed = JSON.parse(msg.data);
        // mode_manager re-publishes the (usually unchanged) roster at 1 Hz;
        // emit only on real change, like the mode/map subscriptions below —
        // otherwise every listener re-renders each second, and the maps
        // panel's rebuilt rows eat any click whose press straddles the emit.
        if (Array.isArray(parsed?.available_maps) && parsed.available_maps.join("\n") !== state.maps.join("\n")) {
          set({ maps: parsed.available_maps });
        }
      } catch {
        // torn payload — keep the last good roster
      }
    }, 0, "std_msgs/msg/String"),
    rosClient.subscribe(NAV_CURRENT_MAP_TOPIC, (msg) => {
      if (typeof msg?.data === "string" && msg.data !== state.currentMap) set({ currentMap: msg.data });
    }, 0, "std_msgs/msg/String"),
    rosClient.subscribe(NAV_CURRENT_MODE_TOPIC, (msg) => {
      if (typeof msg?.data === "string" && msg.data && msg.data !== state.mode) set({ mode: msg.data });
    }, 0, "std_msgs/msg/String"),
  ];

  return {
    state,

    /** @param {(state: NavState) => void} cb fires immediately, then on every change. */
    onChange(cb) {
      listeners.add(cb);
      cb(state);
      return () => listeners.delete(cb);
    },

    // The Slow preset that mapping runs at is mars_app's business: it watches
    // /nav/current_mode, so it covers the runs no client asked for (a mapless
    // boot, mode_manager redirecting a navigation request) and restores the
    // operator's preset on the way out.
    startMapping() {
      return call(NAV_CHANGE_MODE_SERVICE, { mode: "mapping" }, "Starting mapping", {
        cancelSkill: true,
      });
    },

    /** @param {string} mode "navigation" | "mapping" | "autonomous_mapping" | "mapfree" */
    changeMode(mode) {
      const doing =
        mode === "mapfree"
          ? "Switching to map-free"
          : mode === "mapping"
            ? "Starting mapping"
            : mode === "autonomous_mapping"
              ? "Starting autonomous mapping"
              : "Returning to navigation";
      return call(NAV_CHANGE_MODE_SERVICE, { mode }, doing, { cancelSkill: true });
    },

    /** @param {string} name map filename, e.g. "home.yaml" */
    changeMap(name) {
      return call(NAV_CHANGE_MAP_SERVICE, { map_name: name }, `Switching to ${name}`, { cancelSkill: true });
    },

    /**
     * Deleting the map navigation is localized against is refused by
     * mode_manager, so drop to map-free first. Works for the last remaining
     * map too — the robot just stays map-free.
     * @param {string} name
     */
    async deleteMap(name) {
      if (name === state.currentMap && state.mode === "navigation") {
        if (!(await call(NAV_CHANGE_MODE_SERVICE, { mode: "mapfree" }, "Switching to map-free", { cancelSkill: true }))) {
          return false;
        }
      }
      return call(NAV_DELETE_MAP_SERVICE, { map_name: name }, `Deleting ${name}`);
    },

    /**
     * The mobile app's save sequence: save the recording, return to
     * navigation, make the new map active. Each step reports its own failure;
     * a success lands with a hint about re-localizing.
     * @param {string} name base name (no .yaml) @param {boolean} overwrite
     */
    async saveAndActivate(name, overwrite) {
      if (!(await call(NAV_SAVE_MAP_SERVICE, { map_name: name, overwrite }, `Saving ${name}`, { cancelSkill: true }))) {
        return false;
      }
      if (!(await call(NAV_CHANGE_MODE_SERVICE, { mode: "navigation" }, "Returning to navigation"))) return false;
      if (!(await call(NAV_CHANGE_MAP_SERVICE, { map_name: `${name}.yaml` }, `Loading ${name}`))) return false;
      set({ status: { kind: "ok", text: `Saved ${name}.yaml — use Locate if the robot isn't where the map thinks` } });
      return true;
    },

    destroy() {
      destroyed = true;
      listeners.clear();
      for (const unsub of unsubs) unsub();
    },
  };
}
