// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The browser owns first-run participation; the world owns mission success.
import { WEBSOCKET_STATUS_TOPIC } from "../constants.js";
import { FIRST_RUN_REQUEST_EVENT, markOnboardingSeen, publishFirstRunCompletion, readFirstRun, saveFirstRun, shouldAutoStartOnboarding } from "../onboarding.js";

export const INTRO_AGENT_ID = "intro_agent";
export const FIRST_MISSIONS = [
  { id: "put_it_away", environment: "apartment", title: "Put it away", setting: "The apartment", brief: "One LEGO brick. One box. A robot that needs your direction.", prompt: "Pick up the LEGO brick.", icon: "brick" },
  { id: "way_out", environment: "backrooms", title: "Find a way out", setting: "The Backrooms", brief: "Endless yellow rooms. Help MARS find the green exit.", prompt: "Find the exit.", icon: "exit" },
  { id: "other_side", environment: "intersection", title: "The other side", setting: "Crossroads", brief: "Watch the traffic. Guide MARS safely across the street.", prompt: "Help me cross the street.", icon: "crossing" },
];
export const hasIntroAgent = (/** @type {{agents: {id:string}[]}} */ snapshot) => snapshot.agents.some(({id}) => id === INTRO_AGENT_ID);
export function backendReadinessFromMessage(/** @type {any} */ message) {
  try {
    const status = JSON.parse(message.data);
    if (status.connected === true) return true;
    if (["invalid_config", "connection_error", "backend_error", "error", "stopped"].includes(status.state)) return false;
  } catch { /* wait for a valid heartbeat */ }
  return null;
}

/**
 * @param {HTMLElement} root
 * @param {import('../rosClient.js').RosClient} ros
 * @param {ReturnType<typeof import('../teleop/agentState.js').sharedAgentState>} agentState
 * @param {{enabled:boolean, session:any, onNotice?:(text:string)=>void, onStart?:(fresh:boolean, startedAt:number)=>void, onSuggestedPrompt?:(text:string|null)=>void}} options
 */
export function createAgentOnboarding(root, ros, agentState, options) {
  const session = options.session;
  let saved = readFirstRun();
  let active = options.enabled && shouldAutoStartOnboarding();
  let destroyed = false;
  let backendReady = /** @type {boolean|null} */ (null);
  let challenge = /** @type {any} */ (null);
  let environment = /** @type {any} */ (null);
  let operation = /** @type {Promise<void>|null} */ (null);
  let activation = /** @type {Promise<boolean>|null} */ (null);
  let reconnectTimer = /** @type {ReturnType<typeof setTimeout>|undefined} */ (undefined);
  let began = false;
  let statusMessage = "";
  const views = new Set();
  let abort = new AbortController();
  let restarting = /** @type {Promise<void>|null} */ (null);
  const listeners = new Set();
  const overlay = document.createElement("section");
  overlay.className = "first-mission";
  overlay.setAttribute("aria-label", "Your first mission");
  root.append(overlay);

  function persist() {
    saveFirstRun(saved);
  }
  const mission = () => FIRST_MISSIONS.find(({id}) => id === saved?.id);
  function paintVisibility() {
    root.classList.toggle("agent-conversation-onboarding", active);
    root.classList.toggle("first-mission-choosing", active && !mission());
    document.body.classList.toggle("agent-conversation-onboarding-active", active);
    overlay.hidden = !active || !!mission();
    document.dispatchEvent(new CustomEvent("innate:first-run-visibility", {detail:{active}}));
  }
  function button(/** @type {string} */ text, /** @type {()=>void} */ click, className = "") {
    const el = document.createElement("button");
    el.type = "button"; el.textContent = text; el.className = className;
    el.addEventListener("click", click); return el;
  }
  function render(/** @type {string} */ status = "") {
    statusMessage = status;
    paintVisibility();
    overlay.replaceChildren();
    for (const view of views) view(snapshot());
    if (!active || mission()) return;
    const header = document.createElement("div"); header.className = "first-mission-heading";
    const eyebrow = document.createElement("span"); eyebrow.className = "microlabel"; eyebrow.textContent = "Meet MARS";
    const title = document.createElement("h1"); title.textContent = "What shall we do first?";
    const body = document.createElement("p"); body.textContent = "Pick a mission. Give MARS instructions in your own words.";
    header.append(eyebrow, title, body); overlay.append(header);
    const choices = document.createElement("div"); choices.className = "first-mission-choices";
    for (const item of FIRST_MISSIONS) {
      const choice = button("", () => void choose(item), "first-mission-choice");
      choice.dataset.mission = item.id;
      const art = document.createElement("span"); art.className = `first-mission-art ${item.icon}`; art.setAttribute("aria-hidden", "true");
      const setting = document.createElement("span"); setting.className = "microlabel"; setting.textContent = item.setting;
      const name = document.createElement("strong"); name.textContent = item.title;
      const brief = document.createElement("span"); brief.className = "first-mission-brief"; brief.textContent = item.brief;
      choice.append(art, setting, name, brief); choices.append(choice);
    }
    overlay.append(choices);
    overlay.append(button("Explore on my own", () => void finish("skipped"), "first-mission-skip"));
  }
  function snapshot() {
    return {active, mission:mission(), attemptId:saved?.attemptId, status:statusMessage};
  }
  function notify() { for (const listener of listeners) listener(); }
  function waitFor(/** @type {()=>boolean} */ predicate, /** @type {string} */ failure, timeout = 20000) {
    return new Promise((resolve, reject) => {
      /** @type {ReturnType<typeof setTimeout>} */
      let timer;
      const cleanup = () => { clearTimeout(timer); listeners.delete(check); abort.signal.removeEventListener("abort", cancel); };
      const cancel = () => { cleanup(); reject(new Error("Mission closed")); };
      const check = () => { if (predicate()) { cleanup(); resolve(undefined); } };
      if (abort.signal.aborted || !active) { cancel(); return; }
      listeners.add(check); abort.signal.addEventListener("abort", cancel, {once:true});
      timer = setTimeout(() => {cleanup(); reject(new Error(failure));}, timeout);
      check();
    });
  }
  async function ensureRunning() {
    if (!active) return false;
    if (saved?.phase !== "playing") throw new Error("Your mission is still loading.");
    if (!activation) activation = (async () => {
      await waitFor(() => backendReady === true && hasIntroAgent(agentState.get()), "MARS is still connecting. You can wait here or skip the mission.");
      if (!active || destroyed) return false;
      const state = agentState.get();
      if (!state.brainActive || state.currentDirective !== INTRO_AGENT_ID) {
        await agentState.setDirective(INTRO_AGENT_ID);
        if (!active || destroyed) {
          // Closing/reopening the page does not stop the robot. An explicit
          // Skip does, including when its activation acknowledgement was late.
          const current = agentState.get().currentDirective;
          if (saved?.phase === "skipped" && (!current || current === INTRO_AGENT_ID)) await agentState.setDirective("");
          return false;
        }
      }
      await waitFor(() => agentState.get().brainActive && agentState.get().currentDirective === INTRO_AGENT_ID, "MARS could not start. You can wait here or skip the mission.");
      return active && !destroyed;
    })().finally(() => {activation = null;});
    return activation;
  }
  async function connectMission(/** @type {boolean} */ fresh) {
    const selected = mission();
    if (!selected) return;
    if (!began) { began = true; options.onStart?.(fresh, saved.startedAt); }
    render(fresh ? "Preparing your mission…" : "Reconnecting to your mission…");
    if (saved.phase === "starting") {
      // Only an explicit choice initializes a scene. A retry after a dropped
      // acknowledgement uses the same UUID, which the server makes idempotent.
      await waitFor(() => !!environment, "The simulator is still connecting. You can wait here or skip.");
      if (challenge?.active?.attempt_id !== saved.attemptId && agentState.get().brainActive) {
        await agentState.setDirective("");
        await waitFor(() => !agentState.get().brainActive, "Waiting for MARS to stop before preparing the mission.");
      }
      if (!active || destroyed) return;
      if (environment.environment?.id !== selected.environment) session.switchEnvironment(selected.environment);
      await waitFor(() => environment.environment?.id === selected.environment && !environment.switch,
        "This environment could not load. You can wait here or skip.", 45000);
      await waitFor(() => challenge?.list?.some((/** @type {any} */ c) => c.id === selected.id), "This mission is unavailable in this simulator.");
      session.startChallenge(selected.id, saved.attemptId);
    }
    await waitFor(() => challenge?.active?.attempt_id === saved.attemptId, "Your mission is not available in this simulator session. Skip to continue exploring.", 30000);
    if (!active || destroyed) return;
    saved.phase = "playing"; persist();
    if (challenge.active.state === "passed") { await finish("done"); return; }
    options.onSuggestedPrompt?.(fresh ? selected.prompt : null);
    render();
    await ensureRunning();
  }
  function runConnect(/** @type {boolean} */ fresh) {
    if (operation) return operation;
    operation = connectMission(fresh).catch(error => {
      if (!active || destroyed) return;
      render(error.message);
      options.onNotice?.(error.message);
      // Reconnect in place after transient startup/disconnection failures. A
      // saved playing attempt is never restarted; starting retries keep its UUID.
      reconnectTimer = setTimeout(() => { if (active && !destroyed) void runConnect(fresh); }, 2500);
    }).finally(() => {operation = null;});
    return operation;
  }
  async function choose(/** @type {typeof FIRST_MISSIONS[number]} */ selected) {
    if (!active || mission()) return;
    saved = {id:selected.id, attemptId:crypto.randomUUID(), phase:"starting", startedAt:Date.now()};
    persist();
    await runConnect(true);
  }
  async function finish(/** @type {"done"|"skipped"} */ phase) {
    if (!active) return;
    const owned = !!saved?.attemptId && challenge?.active?.attempt_id === saved.attemptId;
    active = false;
    saved = {...saved, phase}; persist(); markOnboardingSeen();
    publishFirstRunCompletion(phase);
    clearTimeout(reconnectTimer); abort.abort(); render(); options.onSuggestedPrompt?.(null);
    if (phase === "skipped" && owned) {
      session.abortChallenge(saved.attemptId);
      if (agentState.get().currentDirective === INTRO_AGENT_ID) await agentState.setDirective("");
    }
    if (phase === "done") options.onNotice?.("Mission complete. The full interface is ready to explore.");
  }
  const unsubBackend = ros.subscribe(WEBSOCKET_STATUS_TOPIC, message => {
    const ready = backendReadinessFromMessage(message);
    if (ready !== null) backendReady = ready;
    notify();
  }, undefined, "std_msgs/msg/String");
  const unsubState = agentState.subscribe(notify);
  const unsubEnvironment = session.onEnvironment?.((/** @type {any} */ value) => {environment = value; notify();});
  const unsubChallenge = session.onChallenge?.((/** @type {any} */ value) => {
    challenge = value; notify();
    if (active && saved?.attemptId && value.active?.attempt_id === saved.attemptId && value.active.state === "passed") void finish("done");
  });
  function restart() {
    if (!options.enabled || destroyed) return Promise.resolve();
    if (restarting) return restarting;
    restarting = (async () => {
      // Drain the old attempt before replacing its cancellation signal. Late
      // activation acknowledgements must not start MARS behind the chooser.
      if (active) await finish("skipped");
      else if (saved?.attemptId && challenge?.active?.attempt_id === saved.attemptId
        && agentState.get().currentDirective === INTRO_AGENT_ID) await agentState.setDirective("");
      await Promise.allSettled([operation, activation].filter(Boolean));
      if (destroyed) return;
      abort = new AbortController();
      saved = {phase:"choosing"};
      active = true; began = false;
      persist(); render();
    })().catch(error => {
      options.onNotice?.(`Could not open challenges: ${error.message}. Try picking another challenge again.`);
    }).finally(() => {restarting = null;});
    return restarting;
  }
  function start(/** @type {Event} */ event) {
    if (/** @type {CustomEvent} */ (event).detail?.restart) { void restart(); return; }
    if (active) { render(); if (mission()) void runConnect(false); }
  }
  window.addEventListener(FIRST_RUN_REQUEST_EVENT, start);
  render();
  // Route remounts reconnect too; they must not rely on the shell's one-time
  // first-page event, nor reset the in-flight skill or world.
  if (active && mission()) void runConnect(false);
  return {
    isActive: () => active,
    subscribe(/** @type {(state:ReturnType<typeof snapshot>)=>void} */ view) {
      views.add(view); view(snapshot()); return () => views.delete(view);
    },
    skip: () => finish("skipped"),
    ensureRunning,
    onUserMessage() { options.onSuggestedPrompt?.(null); },
    destroy() {
      destroyed = true; active = false; clearTimeout(reconnectTimer); abort.abort();
      unsubBackend(); unsubState(); unsubEnvironment?.(); unsubChallenge?.();
      window.removeEventListener(FIRST_RUN_REQUEST_EVENT, start);
      views.clear();
      paintVisibility(); overlay.remove();
    },
  };
}
