// @ts-check
// Shared brain agent/directive state. The agents roster comes from
// /brain/get_available_directives; the live bits (active flag, current
// directive, active skills) are also pushed by the brain on /brain/agent_status
// (latched + heartbeat), so a stop/start/directive change made from another
// client (e.g. the mobile app) updates this UI without polling. Both the chat
// agent picker and the skills active-skill toggles read from here so they stay
// in sync.

import { ros } from "../rosClient.js";
import {
  AGENT_STATUS_TOPIC,
  GET_AVAILABLE_DIRECTIVES_SERVICE,
  SET_DIRECTIVE_TOPIC,
  SET_BRAIN_ACTIVE_SERVICE,
  SET_ACTIVE_SKILLS_TOPIC,
  RESET_BRAIN_SERVICE,
  SAVE_AGENT_SERVICE,
  DELETE_AGENT_SERVICE,
} from "../constants.js";

/**
 * @typedef {{
 *   id: string, name: string, prompt: string, skills: string[],
 *   source: "shipped" | "user", listen: boolean, gaze: boolean, listed: boolean,
 *   path: string, editable: boolean,
 * }} AgentEntry
 * @typedef {{ id: string, name: string, error: string, path: string }} BrokenEntry
 * @typedef {{ id: string, display_name: string, prompt: string, skill_ids: string[], listen: boolean, gaze: boolean }} AgentSpec
 * @typedef {{
 *   agents: AgentEntry[],
 *   broken: BrokenEntry[],
 *   currentDirective: string,
 *   activeSkills: Set<string>,
 *   brainActive: boolean,
 * }} AgentSnapshot
 */

/** @type {ReturnType<typeof createAgentState> | undefined} */
let _shared;

/** The session's agent state, built on first use and kept for the whole session.
 *
 * There is deliberately only one: the shell's "agent is running" pill and the
 * Agent page's panel both read it, and a second instance would double every
 * get_available_directives against the brain's single-threaded executor — the
 * very query refresh() already has to defend with a retry backoff. Pages drop
 * their own subscriptions on unmount; nothing destroys the state itself.
 */
export function sharedAgentState() {
  return (_shared ??= createAgentState());
}

function createAgentState() {
  /** @type {AgentSnapshot} */
  let state = { agents: [], broken: [], currentDirective: "", activeSkills: new Set(), brainActive: false };
  /** @type {Set<(s: AgentSnapshot) => void>} */
  const listeners = new Set();

  function emit() {
    for (const cb of listeners) {
      try {
        cb(state);
      } catch (err) {
        console.error("[agentState] listener threw:", err);
      }
    }
  }

  // The brain serves get_available_directives on a single-threaded executor, so
  // while the agent loop is busy the query can be dropped (rmw_zenoh queue
  // overflow) or lose the race at connect. Rather than blank the picker on a
  // transient miss, we keep the last known roster and retry with backoff.
  const RETRY_DELAYS = [1000, 2000, 4000];
  let retryIndex = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let retryTimer = null;

  function resetRetry() {
    retryIndex = 0;
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  function scheduleRetry() {
    if (retryTimer || retryIndex >= RETRY_DELAYS.length) return;
    const delay = RETRY_DELAYS[retryIndex++];
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void refresh();
    }, delay);
  }

  // The brain only publishes agent_status once its services exist, so this
  // gates refresh() against racing its boot.
  let brainSeen = false;

  async function refresh() {
    if (ros.state !== "connected" || !brainSeen) return;
    try {
      const v = await ros.callService(GET_AVAILABLE_DIRECTIVES_SERVICE, {});
      const entries = Array.isArray(v?.directives)
        ? v.directives
        : typeof v?.directives === "string"
          ? [v.directives]
          : [];
      let agentsRaw;
      try {
        agentsRaw = JSON.parse(entries[0] ?? "[]");
      } catch {
        agentsRaw = [];
      }
      let meta;
      try {
        meta = JSON.parse(entries[1] ?? "{}");
      } catch {
        meta = {};
      }
      /** @type {any[]} */
      const list = Array.isArray(agentsRaw) ? agentsRaw : (agentsRaw && agentsRaw.agents) || [];
      const agents = list
        .filter((a) => a && a.id)
        .map((a) => ({
          id: String(a.id),
          name: String(a.display_name || a.id),
          prompt: String(a.prompt ?? ""),
          skills: Array.isArray(a.skills) ? a.skills.map(String) : [],
          // Agent detail fields: innate agents and files edited in code are read-only.
          source: a.source === "shipped" ? /** @type {const} */ ("shipped") : /** @type {const} */ ("user"),
          listed: a.listed !== false,
          listen: a.listen === true,
          gaze: a.gaze === true,
          path: typeof a.path === "string" ? a.path : "",
          editable: a.editable === true,
        }));
      // Agents that failed to load (broken module/class). Shown disabled with
      // their error — same treatment as broken skills in the skills menu.
      /** @type {any[]} */
      const brokenRaw = Array.isArray(meta?.broken_agents) ? meta.broken_agents : [];
      const broken = brokenRaw
        .filter((a) => a && a.id)
        .map((a) => ({
          id: String(a.id),
          name: String(a.display_name || a.id),
          error: String(a.load_error || "failed to load"),
          path: typeof a.path === "string" ? a.path : "",
        }));
      const activeSkills = new Set((Array.isArray(meta?.active_skills) ? meta.active_skills : []).map(String));
      const brainActive =
        typeof meta?.brain_active === "boolean"
          ? meta.brain_active
          : typeof v?.brain_active === "boolean"
            ? v.brain_active
            : false;
      if (agents.length === 0 && broken.length === 0) {
        // Empty roster: almost always a transient stall/lost race rather than a
        // brain with no agents. Keep whatever we already have and retry.
        scheduleRetry();
        return;
      }
      resetRetry();
      // Idle brain → no current directive (toggles disabled, picker shows None).
      state = {
        agents,
        broken,
        currentDirective: brainActive ? String(v?.current_directive ?? "") : "",
        activeSkills,
        brainActive,
      };
      emit();
    } catch {
      // Dropped query / timeout — keep the last known roster, retry with backoff.
      scheduleRetry();
    }
  }

  /**
   * @param {string} id Directive id to run; "" deactivates the brain.
   * Not optimistic — we don't claim the new directive/active set locally; the
   * brain is the source of truth, so we re-pull get_available_directives and let
   * its response drive the UI.
   */
  async function setDirective(id) {
    try {
      if (id) {
        ros.publish(SET_DIRECTIVE_TOPIC, { data: id });
        await ros.callService(SET_BRAIN_ACTIVE_SERVICE, { data: true });
      } else {
        await ros.callService(SET_BRAIN_ACTIVE_SERVICE, { data: false });
      }
    } catch {
      // The refresh below reflects the brain's real state regardless.
    }
    resetRetry(); // a deliberate change deserves a fresh round of retries
    await refresh();
  }

  /** Replace one agent's active subset outright (the story's grants). The brain
   * drops the update unless that agent is the running directive, so a grant
   * can never trim whichever agent happens to be selected.
   * @param {string[]} skills @param {string} agentId */
  function setActiveSkills(skills, agentId) {
    if (!agentId) return;
    ros.publish(SET_ACTIVE_SKILLS_TOPIC, { data: JSON.stringify({ agent_id: agentId, skills }) });
    // Re-pull so the UI shows what the brain actually registered (it drops unavailable skills).
    setTimeout(() => void refresh(), 400);
  }

  /** Write an agent file from the detail form; the brain reloads the roster before
   * answering, and the refresh here shows it. @param {AgentSpec} spec
   * @returns {Promise<{ success: boolean, message: string, path: string }>} */
  async function saveAgent(spec) {
    const res = await ros.callService(SAVE_AGENT_SERVICE, spec);
    if (res?.success) await refresh();
    return { success: !!res?.success, message: String(res?.message ?? ""), path: String(res?.path ?? "") };
  }

  /** @param {string} id @returns {Promise<{ success: boolean, message: string }>} */
  async function deleteAgent(id) {
    const res = await ros.callService(DELETE_AGENT_SERVICE, { id });
    if (res?.success) await refresh();
    return { success: !!res?.success, message: String(res?.message ?? "") };
  }

  /** @param {string} [memoryState] @returns {Promise<any>} */
  function resetBrain(memoryState = "") {
    return ros.callService(RESET_BRAIN_SERVICE, { memory_state: memoryState });
  }

  // Live state pushed by the brain. The heartbeat re-emits an unchanged sample
  // every few seconds, so skip the emit when nothing moved to avoid re-render
  // churn. The roster still comes from refresh(); this only updates the live
  // bits (a status arriving before the first roster is fine — the picker just
  // stays empty until refresh lands).
  ros.subscribe(AGENT_STATUS_TOPIC, (msg) => {
    let payload;
    try {
      payload = JSON.parse(msg?.data ?? "");
    } catch {
      return;
    }
    if (typeof payload?.brain_active !== "boolean") return;
    if (!brainSeen) {
      brainSeen = true;
      resetRetry();
      void refresh();
    }
    const brainActive = payload.brain_active;
    // Idle brain → no current directive (toggles disabled, picker shows None),
    // mirroring refresh().
    const currentDirective = brainActive ? String(payload.current_directive ?? "") : "";
    const activeSkills = new Set((Array.isArray(payload.active_skills) ? payload.active_skills : []).map(String));
    const unchanged =
      state.brainActive === brainActive &&
      state.currentDirective === currentDirective &&
      state.activeSkills.size === activeSkills.size &&
      [...activeSkills].every((id) => state.activeSkills.has(id));
    if (unchanged) return;
    state = { ...state, brainActive, currentDirective, activeSkills };
    emit();
  }, undefined, "std_msgs/msg/String");

  ros.onStateChange((s) => {
    if (s === "connected") {
      resetRetry();
      void refresh();
    }
  });
  void refresh();

  return {
    get: () => state,
    /** @param {(s: AgentSnapshot) => void} cb @returns {() => void} */
    subscribe(cb) {
      listeners.add(cb);
      cb(state);
      return () => {
        listeners.delete(cb);
      };
    },
    refresh,
    setDirective,
    setActiveSkills,
    saveAgent,
    deleteAgent,
    resetBrain,
  };
}
