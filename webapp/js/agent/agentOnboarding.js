// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

// First-run Agent onboarding is a real conversation with Intro Agent. The
// browser owns the session and reveals the interface from the local turn and
// the exact skill runs that follow it; robot-wide events are never sufficient.

import {
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
export const CAPABILITY_RESPONSE =
  "I am your physical agent, can evolve in the world, do whatever you want, and ask me anything.";
export const GUIDED_PROMPTS = {
  capabilities: "What can you do?",
  pickup: "Pick up the Lego in front of you.",
  deliver: "Go and give it to the person in the corner.",
};
const ONBOARDING_SESSION_MS = 30 * 60 * 1000;

/** @param {{agents: Array<{id: string}>}} snapshot */
export function hasIntroAgent(snapshot) {
  return snapshot.agents.some((agent) => agent.id === INTRO_AGENT_ID);
}

/** @param {unknown} value @returns {"capabilities" | "pickup" | "deliver" | "done"} */
export function parsePromptStage(value) {
  if (!value || typeof value !== "object") return "capabilities";
  const stage = /** @type {{promptStage?: unknown}} */ (value).promptStage;
  return stage === "pickup" || stage === "deliver" || stage === "done" ? stage : "capabilities";
}

/** @param {string} text */
const normalizedPrompt = (text) => text.trim().toLowerCase().replace(/[.!?]+$/, "");

/** Match either a display name ("Pick Any Object") or a namespaced skill id
 * ("innate-os/pick_any_object") without coupling onboarding to catalog copy.
 * @param {string} value @param {string} expected */
export function matchesSkill(value, expected) {
  const name = value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return name === expected || name.endsWith(`_${expected}`);
}

/** Reduce the robot-wide skill bus to one run this browser can own.
 * @param {string} expectedSkill @param {string} ownedRunId
 * @param {{skill: string, runId: string, status: string, timestamp: number}} event
 * @param {number} afterTimestamp
 * @returns {{kind: "claim" | "completed" | "failed" | "interrupted", runId: string} | null} */
export function ownedSkillEvent(expectedSkill, ownedRunId, event, afterTimestamp) {
  if (
    !event.runId ||
    event.timestamp < afterTimestamp ||
    !matchesSkill(event.skill, expectedSkill)
  ) return null;
  if (event.status === "running") {
    return ownedRunId ? null : { kind: "claim", runId: event.runId };
  }
  if (!ownedRunId || event.runId !== ownedRunId) return null;
  if (["completed", "failed", "interrupted"].includes(event.status)) {
    return /** @type {{kind: "completed" | "failed" | "interrupted", runId: string}} */ ({
      kind: event.status,
      runId: event.runId,
    });
  }
  return null;
}

/** @param {unknown} value @returns {string[]} */
export function parseRevealSections(value) {
  if (!value || typeof value !== "object") return [];
  const raw = /** @type {{revealed?: unknown}} */ (value).revealed;
  if (!Array.isArray(raw)) return [];
  return REVEAL_SECTIONS.filter((section) => raw.includes(section));
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
 *   prepareEnvironment?: () => Promise<void>,
 *   prepareLego?: () => void,
 * }} options
 */
export function createAgentOnboarding(root, rosClient, agentState, options) {
  let active = false;
  let startedAt = loadStartedAt();
  /** @type {Promise<void> | null} */
  let starting = null;
  let destroyed = false;
  /** @type {"capabilities" | "pickup" | "deliver" | "done"} */
  let promptStage = loadPromptStage();
  /** @type {"capabilities" | "pickup" | "deliver" | null} */
  let awaitingReply = null;
  let awaitingSince = 0;
  let ownedSkillRunId = "";
  let deliverySkill = "navigate_to_position";
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
      hasIntroAgent,
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

  /** Start synthesizing the opening line as soon as the brain exists. Full
   * agent activation can finish in parallel; speech does not depend on it.
   * @param {number} sessionStartedAt */
  async function greetWhenBrainIsPresent(sessionStartedAt) {
    const roster = await waitForState(hasIntroAgent, 10_000);
    if (!roster || destroyed || !active || startedAt !== sessionStartedAt) return;
    rosClient.publish(TTS_TOPIC, { data: ONBOARDING_GREETING });
  }

  async function start(restart = false) {
    if (!options.enabled) return;
    const now = Date.now();
    const fresh = restart || startedAt === 0 || now - startedAt > ONBOARDING_SESSION_MS;
    if (fresh) {
      revealed.clear();
      promptStage = "capabilities";
      awaitingReply = null;
      awaitingSince = 0;
      ownedSkillRunId = "";
      deliverySkill = "navigate_to_position";
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
    try {
      // A fresh tour owns its opening scene. Wait for the environment reset so
      // the greeting and first suggested action cannot race the robot spawn.
      // A reload of an unfinished tour resumes the existing world untouched.
      if (fresh) await options.prepareEnvironment?.();
      if (destroyed || !active) return;
      options.onStart?.(fresh, startedAt);
      syncSuggestedPrompt();
      // A resumed session means the page was reloaded before onboarding finished.
      // The robot process may also have restarted, so replay the opening line to
      // make that restored session visibly and audibly begin again.
      const greeting = greetWhenBrainIsPresent(startedAt);
      await Promise.all([ensureRunning(), greeting]);
      if (destroyed || !active) return;
    } catch (error) {
      if (destroyed) return;
      const detail = error instanceof Error ? error.message : "The Intro Agent could not start.";
      options.onNotice?.(`${detail} The full simulator interface has been restored.`);
      for (const section of REVEAL_SECTIONS) revealed.add(section);
      active = false;
      render();
      syncSuggestedPrompt();
    }
  }

  /** The panel invokes this only after this browser successfully publishes its
   * own turn. Remote chat echoes are transcript-only.
   * @param {string} text @param {number} timestamp */
  function onUserMessage(text, timestamp) {
    if (!active || awaitingReply !== null || promptStage === "done") return;
    if (normalizedPrompt(text) !== normalizedPrompt(GUIDED_PROMPTS[promptStage])) return;
    awaitingReply = promptStage;
    awaitingSince = timestamp;
    ownedSkillRunId = "";
    deliverySkill = "navigate_to_position";
    syncSuggestedPrompt();
  }

  /** @param {string} text @param {number} timestamp */
  function onRobotMessage(text, timestamp) {
    if (
      !active ||
      awaitingReply !== "capabilities" ||
      timestamp < awaitingSince ||
      normalizedPrompt(text) !== normalizedPrompt(CAPABILITY_RESPONSE)
    ) return;
    reveal("cameras");
    promptStage = "pickup";
    awaitingReply = null;
    awaitingSince = 0;
    persist();
    syncSuggestedPrompt();
  }

  /** @param {{skill: string, runId: string, status: string, timestamp: number}} event */
  function onSkillStatus(event) {
    if (!active || !awaitingReply || awaitingReply === "capabilities") return;
    const expectedSkill = awaitingReply === "pickup" ? "pick_any_object" : deliverySkill;
    const ownedEvent = ownedSkillEvent(expectedSkill, ownedSkillRunId, event, awaitingSince);
    if (ownedEvent?.kind === "claim") {
      // The first expected run begun after this browser's accepted prompt owns
      // the step. A terminal event with any other run id is robot-wide noise.
      ownedSkillRunId = ownedEvent.runId;
      return;
    }
    if (!ownedEvent) return;
    if (ownedEvent.kind === "failed" || ownedEvent.kind === "interrupted") {
      awaitingReply = null;
      awaitingSince = 0;
      ownedSkillRunId = "";
      deliverySkill = "navigate_to_position";
      syncSuggestedPrompt();
      return;
    }
    if (awaitingReply === "pickup") {
      reveal("controls");
      promptStage = "deliver";
      awaitingReply = null;
      awaitingSince = 0;
      ownedSkillRunId = "";
      persist();
      syncSuggestedPrompt();
      return;
    }
    if (deliverySkill === "navigate_to_position") {
      // Delivery is one browser-owned chain: keep the prompt closed while the
      // agent moves from the owned navigation run to its OpenGripper run.
      deliverySkill = "open_gripper";
      ownedSkillRunId = "";
      return;
    }
    reveal("complete");
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
    onSkillStatus,
    destroy() {
      destroyed = true;
      active = false;
      render();
      syncSuggestedPrompt();
      unadvertiseGreeting();
      unsubBackend();
      window.removeEventListener(ONBOARDING_REQUEST_EVENT, /** @type {EventListener} */ (onRequest));
    },
  };
}
