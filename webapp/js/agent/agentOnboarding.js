// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

// First-run Agent onboarding is a real conversation with Intro Agent. The
// browser owns only presentation state; the agent decides when to reveal each
// part of the simulator by calling RevealOnboarding.

import {
  ONBOARDING_UI_TOPIC,
  TTS_TOPIC,
  WEBSOCKET_STATUS_TOPIC,
} from "../constants.js";
import {
  markOnboardingSeen,
  ONBOARDING_REQUEST_EVENT,
  ONBOARDING_VERSION,
} from "../onboarding.js";

export const AGENT_ONBOARDING_PROGRESS_KEY = `innate.agentOnboarding.v${ONBOARDING_VERSION}`;
export const INTRO_AGENT_ID = "intro_agent";
export const REVEAL_SECTIONS = ["cameras", "controls", "complete"];
export const ONBOARDING_GREETING =
  "Hi, I’m MARS — your friendly, AI-native personal robot.";
export const GUIDED_PROMPTS = {
  capabilities: "What can you do?",
  pickup: "Pick up this Lego piece in front of you.",
};
const ONBOARDING_SESSION_MS = 30 * 60 * 1000;

/** @param {unknown} value @returns {"capabilities" | "pickup" | "done"} */
export function parsePromptStage(value) {
  if (!value || typeof value !== "object") return "capabilities";
  const stage = /** @type {{promptStage?: unknown}} */ (value).promptStage;
  return stage === "pickup" || stage === "done" ? stage : "capabilities";
}

/** @param {string} text */
const normalizedPrompt = (text) => text.trim().toLowerCase().replace(/[.!?]+$/, "");

/** @param {unknown} value @returns {string[]} */
export function parseRevealSections(value) {
  if (!value || typeof value !== "object") return [];
  const raw = /** @type {{revealed?: unknown}} */ (value).revealed;
  if (!Array.isArray(raw)) return [];
  return REVEAL_SECTIONS.filter((section) => raw.includes(section));
}

/** @param {unknown} message @returns {string | null} */
export function revealSectionFromMessage(message) {
  if (typeof /** @type {any} */ (message)?.data !== "string") return null;
  try {
    const section = String(JSON.parse(/** @type {any} */ (message).data)?.section ?? "");
    return REVEAL_SECTIONS.includes(section) ? section : null;
  } catch {
    return null;
  }
}

/** Ready, definitively unavailable, or still starting/unknown.
 * @param {unknown} message @returns {boolean | null} */
export function backendReadinessFromMessage(message) {
  if (typeof /** @type {any} */ (message)?.data !== "string") return null;
  try {
    const status = JSON.parse(/** @type {any} */ (message).data);
    if (status?.connected === true) return true;
    if (["invalid_config", "connection_error", "backend_error", "error", "stopped"].includes(status?.state)) {
      return false;
    }
  } catch {
    // Unknown status is allowed time to settle below.
  }
  return null;
}

/**
 * @param {HTMLElement} root
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<typeof import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {{
 *   enabled: boolean,
 *   onNotice?: (text: string) => void,
 *   onStart?: (fresh: boolean, startedAt: number) => void,
 *   onSuggestedPrompt?: (text: string | null) => void,
 *   prepareLego?: () => void,
 * }} options
 */
export function createAgentOnboarding(root, rosClient, agentState, options) {
  let active = false;
  let startedAt = loadStartedAt();
  /** @type {Promise<void> | null} */
  let starting = null;
  let destroyed = false;
  /** @type {"capabilities" | "pickup" | "done"} */
  let promptStage = loadPromptStage();
  /** @type {"capabilities" | "pickup" | null} */
  let awaitingReply = null;
  /** @type {Set<string>} */
  const revealed = new Set(loadProgress());
  /** @type {boolean | null} */
  let backendReady = null;
  /** @type {Set<(ready: boolean) => void>} */
  const backendListeners = new Set();
  const unadvertiseGreeting = rosClient.advertise(TTS_TOPIC, "std_msgs/msg/String");

  function loadProgress() {
    try {
      return parseRevealSections(JSON.parse(localStorage.getItem(AGENT_ONBOARDING_PROGRESS_KEY) || "{}"));
    } catch {
      return [];
    }
  }

  function loadStartedAt() {
    try {
      const value = Number(JSON.parse(localStorage.getItem(AGENT_ONBOARDING_PROGRESS_KEY) || "{}")?.startedAt);
      return Number.isFinite(value) && value > 0 ? value : 0;
    } catch {
      return 0;
    }
  }

  function loadPromptStage() {
    try {
      return parsePromptStage(JSON.parse(localStorage.getItem(AGENT_ONBOARDING_PROGRESS_KEY) || "{}"));
    } catch {
      return "capabilities";
    }
  }

  function persist() {
    try {
      localStorage.setItem(
        AGENT_ONBOARDING_PROGRESS_KEY,
        JSON.stringify({ version: ONBOARDING_VERSION, startedAt, revealed: [...revealed], promptStage }),
      );
    } catch {
      // Storage can be unavailable in locked-down browsers; the live tour works.
    }
  }

  function render() {
    root.classList.toggle("agent-conversation-onboarding", active);
    document.body.classList.toggle("agent-conversation-onboarding-active", active);
    for (const section of REVEAL_SECTIONS) {
      root.classList.toggle(`agent-onboarding-show-${section}`, !active || revealed.has(section));
    }
  }

  function syncSuggestedPrompt() {
    const text = active && awaitingReply === null && promptStage !== "done"
      ? GUIDED_PROMPTS[promptStage]
      : null;
    options.onSuggestedPrompt?.(text);
    if (text === GUIDED_PROMPTS.pickup) options.prepareLego?.();
  }

  /** @param {string} section */
  function reveal(section) {
    if (!active || !REVEAL_SECTIONS.includes(section)) return;
    if (section === "complete") {
      for (const name of REVEAL_SECTIONS) revealed.add(name);
      active = false;
      persist();
      markOnboardingSeen();
      render();
      syncSuggestedPrompt();
      return;
    }
    revealed.add(section);
    persist();
    render();
  }

  const unsubUi = rosClient.subscribe(
    ONBOARDING_UI_TOPIC,
    (message) => {
      const section = revealSectionFromMessage(message);
      if (section) reveal(section);
    },
    undefined,
    "std_msgs/msg/String",
  );
  const unsubBackend = rosClient.subscribe(
    WEBSOCKET_STATUS_TOPIC,
    (message) => {
      const readiness = backendReadinessFromMessage(message);
      if (readiness === null) return;
      backendReady = readiness;
      for (const listener of backendListeners) listener(readiness);
    },
    undefined,
    "std_msgs/msg/String",
  );

  function waitForBackend() {
    if (backendReady !== null) return Promise.resolve(backendReady);
    return new Promise((resolve) => {
      let done = false;
      /** @param {boolean} ready */
      const finish = (ready) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        backendListeners.delete(finish);
        resolve(ready);
      };
      const timer = setTimeout(() => finish(false), 10_000);
      backendListeners.add(finish);
    });
  }

  /**
   * @param {(snapshot: ReturnType<typeof agentState.get>) => boolean} predicate
   * @param {number} timeoutMs
   * @returns {Promise<ReturnType<typeof agentState.get> | null>}
   */
  function waitForState(predicate, timeoutMs) {
    return new Promise((resolve) => {
      const initial = agentState.get();
      if (predicate(initial)) {
        resolve(initial);
        return;
      }
      let done = false;
      /** @type {() => void} */
      let unsubscribe = () => {};
      /** @type {ReturnType<typeof setTimeout>} */
      let timer;
      /** @param {ReturnType<typeof agentState.get> | null} value */
      const finish = (value) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        unsubscribe();
        resolve(value);
      };
      unsubscribe = agentState.subscribe((snapshot) => {
        if (predicate(snapshot)) finish(snapshot);
      });
      timer = setTimeout(() => finish(null), timeoutMs);
    });
  }

  async function activateIntroAgent() {
    if (!(await waitForBackend())) {
      throw new Error("Agent onboarding is unavailable because the AI service is offline. Check INNATE_SERVICE_KEY.");
    }
    const roster = await waitForState(
      (snapshot) => snapshot.agents.some((agent) => agent.id === INTRO_AGENT_ID),
      10_000,
    );
    if (!roster || destroyed) throw new Error("Intro Agent is unavailable.");
    const state = agentState.get();
    if (!state.brainActive || state.currentDirective !== INTRO_AGENT_ID) {
      await agentState.setDirective(INTRO_AGENT_ID);
    }
    const running = await waitForState(
      (snapshot) => snapshot.brainActive && snapshot.currentDirective === INTRO_AGENT_ID,
      8_000,
    );
    if (!running || destroyed) throw new Error("Intro Agent did not start.");
  }

  async function ensureRunning() {
    if (!active) return false;
    if (!starting) {
      starting = activateIntroAgent().finally(() => {
        starting = null;
      });
    }
    await starting;
    return true;
  }

  async function start(restart = false) {
    if (!options.enabled) return;
    const now = Date.now();
    const fresh = restart || startedAt === 0 || now - startedAt > ONBOARDING_SESSION_MS;
    if (fresh) {
      revealed.clear();
      promptStage = "capabilities";
      awaitingReply = null;
      startedAt = now;
      try {
        localStorage.removeItem(AGENT_ONBOARDING_PROGRESS_KEY);
      } catch {
        // See persist().
      }
    }
    active = true;
    persist();
    render();
    options.onStart?.(fresh, startedAt);
    syncSuggestedPrompt();
    try {
      await ensureRunning();
      if (destroyed || !active) return;
      if (fresh) {
        // The opening copy is product copy, not a model turn: keep it exact and
        // let the live Intro Agent own every response and reveal after this.
        rosClient.publish(TTS_TOPIC, { data: ONBOARDING_GREETING });
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : "The Intro Agent could not start.";
      options.onNotice?.(`${detail} The full simulator interface has been restored.`);
      for (const section of REVEAL_SECTIONS) revealed.add(section);
      active = false;
      render();
      syncSuggestedPrompt();
    }
  }

  /** @param {string} text */
  function onUserMessage(text) {
    if (!active || awaitingReply !== null || promptStage === "done") return;
    if (normalizedPrompt(text) !== normalizedPrompt(GUIDED_PROMPTS[promptStage])) return;
    awaitingReply = promptStage;
    syncSuggestedPrompt();
  }

  /** @param {string} text */
  function onRobotMessage(text) {
    if (!active || awaitingReply === null || text.trim() === ONBOARDING_GREETING) return;
    promptStage = awaitingReply === "capabilities" ? "pickup" : "done";
    awaitingReply = null;
    persist();
    syncSuggestedPrompt();
  }

  /** @param {CustomEvent<{restart?: boolean}>} event */
  function onRequest(event) {
    void start(Boolean(event.detail?.restart));
  }
  window.addEventListener(ONBOARDING_REQUEST_EVENT, /** @type {EventListener} */ (onRequest));
  render();

  return {
    isActive: () => active,
    ensureRunning,
    onUserMessage,
    onRobotMessage,
    destroy() {
      destroyed = true;
      active = false;
      render();
      syncSuggestedPrompt();
      unadvertiseGreeting();
      unsubUi();
      unsubBackend();
      window.removeEventListener(ONBOARDING_REQUEST_EVENT, /** @type {EventListener} */ (onRequest));
    },
  };
}
