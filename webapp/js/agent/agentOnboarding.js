// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The browser owns first-run participation; the world owns mission success.
import { TTS_TOPIC, WEBSOCKET_STATUS_TOPIC } from "../constants.js";
import { FIRST_MISSIONS, FIRST_RUN_REQUEST_EVENT, publishFirstRunCompletion, readFirstRun, saveFirstRun, shouldAutoStartOnboarding } from "../onboarding.js";

export const INTRO_AGENT_ID = "intro_agent";
export const VIEW_GUIDANCE = "I’m starting now. Switch to my Main view at the top to see what I see. You can also use Arm view for a closer look at what my gripper is doing.";
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
 * @param {{enabled:boolean, session:any, onNotice?:(text:string)=>void, onStart?:(fresh:boolean, startedAt:number)=>void, onClearSuggestions?:()=>void, onViewAccess?:(access:"hidden"|"cameras"|"all")=>void}} options
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
  let taskStarted = saved?.taskStarted === true;
  const unadvertise = options.enabled ? ros.advertise(TTS_TOPIC, "std_msgs/msg/String") : () => {};
  const views = new Set();
  let abort = new AbortController();
  let restarting = /** @type {Promise<boolean>|null} */ (null);
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
    root.classList.toggle("first-mission-view-step", active && saved?.viewsRevealed === true && !saved?.viewChanged);
    options.onViewAccess?.(!active ? "all" : saved?.viewsRevealed === true ? "cameras" : "hidden");
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
  function revealTaskViews() {
    if (!active || destroyed || !taskStarted || saved?.viewsRevealed || !backendReady
      || saved?.phase !== "playing" || challenge?.active?.attempt_id !== saved.attemptId || challenge.active.state === "passed") return;
    // Queue the invitation before exposing the controls. Reconnect retries a
    // failed publish; a saved reveal never repeats it on a route remount.
    if (!ros.publish(TTS_TOPIC, {data:VIEW_GUIDANCE})) return;
    saved.viewsRevealed = true; persist(); render();
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
      if (timeout) timer = setTimeout(() => {cleanup(); reject(new Error(failure));}, timeout);
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
        // Skip and page close only leave the guided UI. An activation already
        // requested may finish; replay explicitly drains and stops it below.
        if (!active || destroyed) return false;
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
      if (!active || destroyed) return;
      session.startChallenge(selected.id, saved.attemptId);
    }
    await waitFor(() => challenge?.active?.attempt_id === saved.attemptId, "Your mission is not available in this simulator session. Skip to continue exploring.", 30000);
    if (!active || destroyed) return;
    saved.phase = "playing"; persist();
    if (challenge.active.state === "passed") { await finish("done"); return; }
    options.onClearSuggestions?.();
    render();
    await ensureRunning();
  }
  function runConnect(/** @type {boolean} */ fresh) {
    if (operation) return operation;
    operation = connectMission(fresh).catch(error => {
      if (!active || destroyed || abort.signal.aborted) return; // a reopen cancelled it on purpose
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
    saved = {id:selected.id, attemptId:crypto.randomUUID(), phase:"starting", startedAt:Date.now(), viewsRevealed:false};
    persist();
    await runConnect(true);
  }
  function close() {
    active = false;
    clearTimeout(reconnectTimer); abort.abort(); render();
  }
  async function finish(/** @type {"done"|"skipped"} */ phase) {
    if (!active) return;
    saved = {...saved, phase}; persist();
    publishFirstRunCompletion(phase);
    close();
    if (phase === "done") {
      options.onClearSuggestions?.();
      options.onNotice?.("Mission complete. The full interface is ready to explore.");
    }
  }
  const unsubBackend = ros.subscribe(WEBSOCKET_STATUS_TOPIC, message => {
    const ready = backendReadinessFromMessage(message);
    if (ready !== null) backendReady = ready;
    revealTaskViews();
    notify();
  }, undefined, "std_msgs/msg/String");
  const unsubState = agentState.subscribe(notify);
  const unsubEnvironment = session.onEnvironment?.((/** @type {any} */ value) => {environment = value; notify();});
  const unsubChallenge = session.onChallenge?.((/** @type {any} */ value) => {
    challenge = value; notify();
    revealTaskViews();
    if (active && saved?.attemptId && value.active?.attempt_id === saved.attemptId && value.active.state === "passed") void finish("done");
  });
  function restart() {
    if (!options.enabled || destroyed || restarting) return Promise.resolve(false);
    if (active && !mission()) return Promise.resolve(true);
    const interrupted = !!(operation || activation);
    restarting = (async () => {
      // Cancel the attempt's pending work under its own signal and drain it, so
      // a late activation cannot start MARS behind the chooser. The guided view
      // stays gated meanwhile: this is not Skip, and nothing terminal is
      // recorded or published for the broker.
      clearTimeout(reconnectTimer); abort.abort();
      await Promise.allSettled([operation, activation].filter(Boolean));
      if (destroyed) return false;
      // Picking another mission is distinct from Skip: stop this attempt's agent
      // and close the attempt. The abort is attempt-scoped, so one the world never
      // acknowledged, or already replaced, is a server-side no-op.
      const owned = saved?.attemptId
        && (challenge?.active?.attempt_id === saved.attemptId || ["starting", "playing"].includes(saved.phase));
      if (owned) {
        // setDirective refreshes state but absorbs service errors. Keep the
        // attempt owned and retryable unless that refresh confirms the stop.
        if (agentState.get().currentDirective === INTRO_AGENT_ID) {
          await agentState.setDirective("");
          if (agentState.get().brainActive) throw new Error("MARS has not stopped yet");
        }
        if (destroyed) return false;
        session.abortChallenge(saved.attemptId);
      }
      saved = {phase:"choosing"}; persist();
      if (destroyed) return false;
      abort = new AbortController();
      active = true; began = false; taskStarted = false;
      options.onClearSuggestions?.();
      render();
      return true;
    })().catch(error => {
      options.onNotice?.(`Could not change mission: ${error.message}. Try again.`);
      // Still owned and still gated: give the attempt a live signal back and
      // resume whatever connection work the cancel interrupted.
      abort = new AbortController();
      if (interrupted && active && mission()) void runConnect(false);
      return false;
    }).finally(() => {restarting = null;});
    return restarting;
  }
  function reopen(/** @type {Event} */ event) {
    const detail = /** @type {CustomEvent} */ (event).detail;
    event.preventDefault();
    void restart().then(success => detail?.complete?.(success));
  }
  window.addEventListener(FIRST_RUN_REQUEST_EVENT, reopen);
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
    onSkillStatus(/** @type {{skill:string,status:string,timestamp:number}} */ event) {
      if (!active || destroyed || saved?.phase !== "playing" || saved.viewsRevealed
        || challenge?.active?.attempt_id !== saved.attemptId
        || event.timestamp * 1000 < saved.startedAt || event.status !== "running") return;
      const name = event.skill.split("/").at(-1) ?? "";
      // Thinking, greeting, memory lookup and suggestions are not task motion.
      if (!name.startsWith("pick_") && !["navigate_to_position", "drop_in_box", "throw_object"].includes(name)) return;
      taskStarted = true; saved.taskStarted = true; persist();
      revealTaskViews();
    },
    onViewChange() {
      if (!active || !saved?.viewsRevealed) return;
      saved.viewChanged = true; persist(); paintVisibility();
    },
    destroy() {
      destroyed = true; active = false; clearTimeout(reconnectTimer); abort.abort();
      unsubBackend(); unsubState(); unsubEnvironment?.(); unsubChallenge?.();
      unadvertise();
      window.removeEventListener(FIRST_RUN_REQUEST_EVENT, reopen);
      views.clear();
      paintVisibility(); overlay.remove();
    },
  };
}
