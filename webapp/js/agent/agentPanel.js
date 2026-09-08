// @ts-check
// Agent panel — the liquid-glass control column on the Agent page. One place to
// run the autonomous brain: pick a directive, Start/Stop it, watch its live
// thinking traces + active skill + chat, and message it a goal.
//
// Data sources (all real ROS, same as the teleop dock panes this replaces):
//   - directive roster / current / brain-active: agentState (get_available_directives)
//   - start/stop:   agentState.setDirective(id) / setDirective("")  (set_brain_active)
//   - thoughts/chat: /brain/chat_out (robot, robot_thoughts, robot_anticipation, system, …)
//   - user input:    /brain/chat_in
//   - active skill:  /brain/skill_status_update
//
// The thought-grouping + skill-run rendering here is the canonical chat stream
// (it originated in the old teleop chat pane, since removed).

import { createPromptSuggestions } from "./promptSuggestions.js";
import { createMicStream } from "./micStream.js";
import {
  AGENT_STATUS_TOPIC,
  CHAT_IN_TOPIC,
  CHAT_OUT_TOPIC,
  GET_CHAT_HISTORY_SERVICE,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";
import { createChatStream, isInternalOnboardingSkill } from "./chatStream.js";
import { createDirectiveControls } from "./directiveControls.js";
import { createAgentSheet } from "./agentSheet.js";

const HISTORY_RECONCILE_MS = 30_000;
// agent_status heartbeats every 3s; don't leave a stale thinking notice up
// if the brain disappears while rosbridge itself remains connected.
const THINKING_STALE_MS = 10_000;

/**
 * @param {HTMLElement} root cockpit root — the panel mounts as a right-edge overlay.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<typeof import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {{
 *   enableMic?: boolean,
 *   onMicState?: (state: {on: boolean, busy: boolean, level: number, waveform: number[], error: string | null}) => void,
 *   ensureRunning?: (fallback: () => Promise<void>) => Promise<void>,
 *   onUserMessage?: (text: string, timestamp: number) => void,
 *   onRobotMessage?: (text: string, timestamp: number) => void,
 *   onSkillStatus?: (event: {skill: string, runId: string, status: string, timestamp: number}) => void,
 * }} opts
 *   enableMic connects the browser microphone in sim, where the robot has no
 *   physical microphone (see micStream.js).
 * @returns {{
 *   destroy: () => void,
 *   startMic: () => Promise<void>,
 *   stopMic: () => void,
 *   micMount: HTMLElement,
 *   setCompact: (on: boolean) => void,
 *   addNotice: (text: string) => void,
 *   beginOnboarding: (fresh: boolean, startedAt: number) => void,
 *   clearSuggestedPrompts: () => void,
 *   setOffers: (offers: Array<{text: string, kind: string, onSelect: (text: string) => void}>) => void,
 *   submitText: (text: string) => Promise<boolean>,
 *   narrate: (text: string) => Promise<boolean>,
 *   setDisplayName: (name: string | null) => void,
 *   isBusy: () => boolean
 * }}
 *   setCompact swaps the right-edge dock for the bottom sheet (agentSheet.js).
 */
export function createAgentPanel(root, rosClient, agentState, opts) {
  const selfOrigin = crypto.randomUUID?.() ?? `web-${Date.now()}-${Math.random()}`;
  const mic = opts.enableMic
    ? createMicStream(rosClient, (state) => opts.onMicState?.(state))
    : null;

  const panel = document.createElement("section");
  panel.className = "overlay agent-panel";
  const controlPanel = document.createElement("section");
  controlPanel.className = "agent-control-panel";
  const thoughtsPanel = document.createElement("section");
  thoughtsPanel.className = "agent-thoughts-panel";

  // ---- header -------------------------------------------------------------
  const head = document.createElement("button");
  head.type = "button";
  head.className = "agent-head";
  head.setAttribute("aria-label", "Collapse agent controls");
  head.setAttribute("aria-expanded", "true");
  const titleEl = document.createElement("span");
  titleEl.className = "agent-title";
  titleEl.innerHTML =
    '<svg class="agent-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/></svg>';
  const headCopy = document.createElement("span");
  headCopy.className = "agent-head-copy";
  const headLabel = document.createElement("span");
  headLabel.className = "agent-head-label";
  headLabel.textContent = "Agent";
  const headAgentName = document.createElement("span");
  headAgentName.className = "agent-head-agent-name";
  headAgentName.textContent = "—";
  headCopy.append(headLabel, headAgentName);
  const headChevron = document.createElement("span");
  headChevron.className = "agent-head-chev";
  headChevron.innerHTML =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
  /** @param {boolean} collapsed */
  function setControlsCollapsed(collapsed) {
    controlPanel.classList.toggle("collapsed", collapsed);
    head.setAttribute("aria-expanded", String(!collapsed));
    updateHeadLabel();
  }
  function updateHeadLabel() {
    const action = controlPanel.classList.contains("collapsed") ? "Expand" : "Collapse";
    head.setAttribute("aria-label", `${action} controls for ${headAgentName.textContent}`);
  }
  head.addEventListener("click", () => setControlsCollapsed(!controlPanel.classList.contains("collapsed")));
  head.append(titleEl, headCopy, headChevron);

  // ---- directive + start/stop --------------------------------------------
  /** @type {ReturnType<typeof createAgentSheet> | undefined} */
  let sheet; // built below, but onAgentName can fire before that
  // The story can name the robot; the header then shows that name, not the directive's.
  /** @type {string | null} */
  let displayNameOverride = null;
  let directiveName = "—";
  function applyHeadName() {
    const name = displayNameOverride ?? directiveName;
    headAgentName.textContent = name;
    sheet?.setName(name);
    updateHeadLabel();
  }
  const directives = createDirectiveControls(agentState, {
    listId: `agent-directive-list-${selfOrigin}`,
    onAgentName(name) {
      directiveName = name;
      applyHeadName();
    },
    onBrainActive(active, justStarted) {
      panel.classList.toggle("active", active);
      if (!justStarted) return;
      if (controlPanel.classList.contains("collapsed")) setControlsCollapsed(false);
      sheet?.open();
    },
  });

  // ---- live stream (thoughts + chat + skill runs) -------------------------
  const chat = createChatStream();

  // ---- composer -----------------------------------------------------------
  const composeArea = document.createElement("div");
  composeArea.className = "agent-compose-area";
  const thinkingNotice = document.createElement("div");
  thinkingNotice.className = "agent-thinking";
  thinkingNotice.setAttribute("role", "status");
  thinkingNotice.setAttribute("aria-live", "polite");
  /** @type {ReturnType<typeof setTimeout> | null} */
  let thinkingTimer = null;
  function clearThinking() {
    if (thinkingTimer !== null) clearTimeout(thinkingTimer);
    thinkingTimer = null;
    thinkingNotice.classList.remove("active");
    thinkingNotice.textContent = "";
  }
  const unsubThinking = rosClient.subscribe(AGENT_STATUS_TOPIC, (msg) => {
    let status;
    try {
      status = JSON.parse(msg?.data ?? "");
    } catch {
      return;
    }
    if (status?.brain_active !== true || status?.brain_thinking !== true) {
      clearThinking();
      return;
    }
    if (thinkingTimer !== null) clearTimeout(thinkingTimer);
    // Heartbeats refresh the timeout without re-announcing the same text.
    if (!thinkingNotice.classList.contains("active")) {
      thinkingNotice.textContent = "Thinking…";
      thinkingNotice.classList.add("active");
    }
    thinkingTimer = setTimeout(clearThinking, THINKING_STALE_MS);
  }, undefined, "std_msgs/msg/String");
  const form = document.createElement("form");
  form.className = "agent-compose";
  const input = document.createElement("textarea");
  input.className = "agent-compose-input";
  input.rows = 1;
  input.setAttribute("aria-label", "Message MARS");
  input.setAttribute("aria-keyshortcuts", "Enter");
  const placeholder = document.createElement("span");
  placeholder.className = "agent-compose-placeholder";
  placeholder.textContent = "Message MARS";
  const micMount = document.createElement("div");
  micMount.className = "agent-compose-mic";
  const focusHint = document.createElement("button");
  focusHint.type = "button";
  focusHint.className = "tts-key tts-focus-key agent-compose-focus-key";
  focusHint.textContent = "↵";
  focusHint.setAttribute("aria-label", "Focus agent message input");
  focusHint.title = "Focus message input (Enter)";
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "agent-compose-send";
  send.innerHTML = '<span class="agent-compose-send-icon" aria-hidden="true"></span>';
  send.setAttribute("aria-label", "Send message");
  send.title = "Send message";
  form.append(input, placeholder, focusHint);
  if (opts.enableMic) form.append(micMount);
  form.append(send);
  function syncComposerAction() {
    const empty = input.value.trim().length === 0;
    send.disabled = empty;
    send.hidden = empty;
    micMount.hidden = !opts.enableMic || !empty;
    focusHint.hidden = !empty;
    placeholder.classList.toggle("hidden", !empty);
  }
  syncComposerAction();

  controlPanel.append(head, directives.el);
  composeArea.append(thinkingNotice, form);
  thoughtsPanel.append(chat.head, chat.wrap, composeArea);
  panel.append(controlPanel, thoughtsPanel);
  root.append(panel);

  const stream = chat.wrap.querySelector(".agent-stream");
  // Where start/stop lives on the dock, so the sheet can hand it back.
  const toggleHome = directives.toggleEl.nextElementSibling;
  sheet = createAgentSheet(panel, {
    // Never scrolled while closed, so it would open on the oldest turn.
    onOpen: () => {
      if (stream instanceof HTMLElement) stream.scrollTop = stream.scrollHeight;
    },
  });

  function focusComposer() {
    sheet?.open();
    input.focus();
  }
  focusHint.addEventListener("click", focusComposer);
  /** @param {KeyboardEvent} e */
  function focusComposerOnEnter(e) {
    if (
      e.defaultPrevented ||
      root.classList.contains("first-mission-choosing") ||
      e.key !== "Enter" ||
      e.repeat ||
      e.altKey ||
      e.ctrlKey ||
      e.metaKey ||
      e.target !== document.body
    ) return;
    e.preventDefault();
    focusComposer();
  }
  window.addEventListener("keydown", focusComposerOnEnter);

  // ---- composer -----------------------------------------------------------
  async function startMic() {
    await (opts.ensureRunning?.(directives.ensureRunning) ?? directives.ensureRunning());
    await mic?.start();
  }

  function stopMic() {
    mic?.stop();
  }

  // Two chip sources share the row under the latest message: the model's
  // suggested replies, and offers the interface makes (grant a skill, pick a
  // persona). Offers come first and survive the model's clears.
  /** @type {string[]} */
  let modelPrompts = [];
  /** @type {Array<{text: string, kind: string, onSelect: (text: string) => void}>} */
  let offers = [];
  let offersExclusive = false;
  function renderChips() {
    chat.setSuggestion([...offers, ...(offersExclusive ? [] : modelPrompts)], (selected) => void submitText(selected));
  }
  const suggestions = createPromptSuggestions((prompts) => {
    modelPrompts = prompts ?? [];
    renderChips();
  });
  let sending = false;
  /** @param {string} text */
  /** Texts this page sent lately: the brain echoes user lines on chat_out, and one bubble is enough. */
  const sentTexts = new Map();
  /** Lines the world spoke through this page; a history replay must not turn them into visitor bubbles. */
  const narratorTexts = new Set();
  /** @param {string} text @param {{ narrator?: boolean, quiet?: boolean }} [how] a narrator line is the world speaking, not the visitor; quiet keeps a failed send off the screen */
  async function submitText(text, how = {}) {
    if (!text || sending) return false;
    sending = true;
    // The bubble lands the moment the person acts, not after the round trip.
    const timestamp = Date.now() / 1000;
    sentTexts.set(text.trim(), Date.now());
    if (how.narrator) narratorTexts.add(text.trim());
    if (how.narrator) chat.addMessage("system", text, timestamp, "narrator");
    else chat.addMessage("user", text, timestamp);
    try {
      await (opts.ensureRunning?.(directives.ensureRunning) ?? directives.ensureRunning());
      const frame = { data: JSON.stringify({ text, sender: "user", timestamp, origin: selfOrigin }) };
      let sent = rosClient.publish(CHAT_IN_TOPIC, frame);
      if (!sent) {
        // A reconnecting socket is the common case; one quiet retry covers it.
        await new Promise((resolve) => setTimeout(resolve, 700));
        sent = rosClient.publish(CHAT_IN_TOPIC, frame);
      }
      if (!sent) throw new Error("The robot connection was lost before the message could be sent.");
      suggestions.clear();
      opts.onUserMessage?.(text, timestamp);
      return true;
    } catch (error) {
      const detail = error instanceof Error ? error.message : "The message could not be sent.";
      if (!root.classList.contains("story-active") && !how.narrator && !how.quiet) chat.addMessage("system", detail, Date.now() / 1000);
      return false;
    } finally {
      sending = false;
    }
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    if (await submitText(text)) {
      if (input.value.trim() === text) input.value = "";
      input.style.height = "auto";
      syncComposerAction();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    void submit();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
    syncComposerAction();
  });

  // ---- history backfill ---------------------------------------------------
  // A topic delivers only what arrives after we subscribe, so a gap in delivery
  // is permanent — the brain's record is the only thing that can close it.
  let loadingHistory = false;
  let lastSnapshot = "";
  let historyFloor = 0;

  /** @param {boolean} [duringOnboarding] */
  async function loadHistory(duringOnboarding = false) {
    if (!duringOnboarding && root.classList.contains("agent-conversation-onboarding")) return;
    if (loadingHistory) return;
    loadingHistory = true;
    try {
      const res = await rosClient.callService(GET_CHAT_HISTORY_SERVICE, {});
      const raw = String(res?.history ?? "");
      if (raw === lastSnapshot) return;
      const entries = JSON.parse(raw || "[]");
      if (!Array.isArray(entries) || !entries.length) return;
      lastSnapshot = raw;
      chat.replay(
        entries
          .filter((entry) => (Number(entry?.timestamp) || 0) >= historyFloor)
          .map((entry) =>
            String(entry?.sender ?? "") === "user" && narratorTexts.has(String(entry?.text ?? "").trim())
              ? { ...entry, narrator: true }
              : entry,
          ),
      );
    } catch (err) {
      console.warn("[chat] reconcile failed:", err);
    } finally {
      loadingHistory = false;
    }
  }

  const unsubConn = rosClient.onStateChange((s) => {
    if (s === "connected") void loadHistory();
    else clearThinking();
  });

  const onVisible = () => {
    if (document.visibilityState === "visible") void loadHistory();
  };
  document.addEventListener("visibilitychange", onVisible);
  const historyPoll = setInterval(onVisible, HISTORY_RECONCILE_MS);

  // ---- live subscriptions -------------------------------------------------
  const unsubIn = rosClient.subscribe(CHAT_IN_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    if (payload?.origin === selfOrigin) return;
    if (String(payload?.sender ?? "") !== "user") return;
    const text = String(payload?.text ?? "");
    if (!text) return;
    suggestions.clear();
    chat.addMessage("user", text, Number(payload?.timestamp) || Date.now() / 1000);
  }, undefined, "std_msgs/msg/String");

  const unsubOut = rosClient.subscribe(CHAT_OUT_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    const sender = String(payload?.sender ?? "");
    const text = String(payload?.text ?? "");
    if (!sender || !text) return;
    const ts = Number(payload?.timestamp) || Date.now() / 1000;
    if (sender === "user") {
      suggestions.clear();
      const sentAt = sentTexts.get(text.trim());
      if (sentAt !== undefined && Date.now() - sentAt < 60_000) return; // already on screen
    }
    chat.routeChatOut(sender, text, ts);
    if (sender === "robot") {
      clearThinking(); // the reply is here; a lit "Thinking…" beside fresh chips reads as frozen
      opts.onRobotMessage?.(text, ts);
    }
  }, undefined, "std_msgs/msg/String");

  /** Skills the brain has started and not yet finished, by run id. */
  const runningSkills = new Set();
  const unsubSkill = rosClient.subscribe(SKILL_STATUS_UPDATE_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    const name = String(payload?.primitive_name ?? payload?.skill_name ?? payload?.skill_id ?? "");
    const status = String(payload?.status ?? "");
    if (suggestions.consume(payload)) return;
    if (!name || !status || isInternalOnboardingSkill(name)) return;
    const key = String(payload?.primitive_id ?? payload?.skill_id ?? name);
    const reason = typeof payload?.reason === "string" ? payload.reason : "";
    const ts = Number(payload?.timestamp) || Date.now() / 1000;
    if (status === "running") runningSkills.add(key);
    else runningSkills.delete(key);
    opts.onSkillStatus?.({ skill: String(payload?.skill_id ?? name), runId: key, status, timestamp: ts });
    chat.addSkillRun(key, name, status, ts, reason, payload?.args);
  }, undefined, "std_msgs/msg/String");

  return {
    startMic,
    stopMic,
    micMount,
    setCompact(on) {
      // Compact drops the control panel's header for the sheet's.
      if (on) sheet.actionSlot.append(directives.toggleEl);
      else directives.el.insertBefore(directives.toggleEl, toggleHome);
      // Its switch is hidden here, so a wider visit's choice must not stick.
      if (on) chat.setMode("compact");
      sheet.setEnabled(on);
    },
    /** @param {string} text */
    addNotice(text) {
      chat.addMessage("system", text, Date.now() / 1000);
    },
    beginOnboarding(fresh, startedAt) {
      historyFloor = startedAt / 1000;
      lastSnapshot = "";
      suggestions.clear();
      chat.clear();
      sheet.open();
      if (!fresh) void loadHistory(true);
    },
    clearSuggestedPrompts: () => suggestions.clear(),
    /** @param {Array<{text: string, kind: string, onSelect: (text: string) => void}>} next */
    setOffers(next, exclusive = false) {
      // Called on every world frame; only a changed set may touch the DOM.
      const key = `${exclusive}|${next.map((o) => `${o.kind}:${o.text}`).join("\n")}`;
      if (key === `${offersExclusive}|${offers.map((o) => `${o.kind}:${o.text}`).join("\n")}`) return;
      offers = next;
      offersExclusive = exclusive;
      renderChips();
    },
    submitText,
    /** @param {string} text */
    narrate: (text) => submitText(text, { narrator: true }),
    /** @param {string | null} name */
    setDisplayName(name) {
      displayNameOverride = name;
      applyHeadName();
    },
    isBusy: () => runningSkills.size > 0,
    destroy() {
      sheet.destroy();
      mic?.destroy();
      directives.destroy();
      chat.destroy();
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(historyPoll);
      window.removeEventListener("keydown", focusComposerOnEnter);
      unsubConn();
      unsubIn();
      unsubOut();
      unsubSkill();
      unsubThinking();
      clearThinking();
      panel.remove();
    },
  };
}
