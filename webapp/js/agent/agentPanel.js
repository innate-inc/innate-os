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

import { createMicStream } from "./micStream.js";
import {
  CHAT_IN_TOPIC,
  CHAT_OUT_TOPIC,
  GET_CHAT_HISTORY_SERVICE,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";
import { createChatStream } from "./chatStream.js";
import { createDirectiveControls } from "./directiveControls.js";
import { createAgentSheet } from "./agentSheet.js";

const HISTORY_RECONCILE_MS = 30_000;

const CHAT_EXAMPLES = [
  "What can you see?",
  "What do you remember here?",
  "Move forward 1ft and wave",
  "Wave hello",
  "Move across the room",
];

/**
 * @param {HTMLElement} root cockpit root — the panel mounts as a right-edge overlay.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<typeof import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {{
 *   enableMic?: boolean,
 *   onMicState?: (state: {on: boolean, busy: boolean, level: number, waveform: number[], error: string | null}) => void,
 *   onUserMessage?: () => void,
 *   onRobotMessage?: (message: HTMLElement | null) => void,
 *   onAgentName?: (name: string) => void
 * }} opts
 *   enableMic connects the browser microphone in sim, where the robot has no
 *   physical microphone (see micStream.js).
 * @returns {{
 *   destroy: () => void,
 *   startMic: () => Promise<void>,
 *   stopMic: () => void,
 *   micMount: HTMLElement,
 *   setCompact: (on: boolean) => void
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
  const directives = createDirectiveControls(agentState, {
    listId: `agent-directive-list-${selfOrigin}`,
    onAgentName(name) {
      headAgentName.textContent = name;
      sheet?.setName(name);
      updateHeadLabel();
      opts.onAgentName?.(name);
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
  const form = document.createElement("form");
  form.className = "agent-compose";
  const input = document.createElement("textarea");
  input.className = "agent-compose-input";
  input.rows = 1;
  input.setAttribute("aria-label", "Message MARS");
  input.setAttribute("aria-keyshortcuts", "Enter");
  const placeholder = document.createElement("span");
  placeholder.className = "agent-compose-placeholder";
  placeholder.textContent = CHAT_EXAMPLES[0];
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
  let placeholderIndex = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let placeholderSwapTimer = null;
  const placeholderInterval = setInterval(() => {
    placeholderIndex = (placeholderIndex + 1) % CHAT_EXAMPLES.length;
    if (input.value.trim()) {
      placeholder.textContent = CHAT_EXAMPLES[placeholderIndex];
      return;
    }
    placeholder.classList.add("exiting");
    placeholderSwapTimer = setTimeout(() => {
      placeholder.textContent = CHAT_EXAMPLES[placeholderIndex];
      placeholder.classList.remove("exiting");
      placeholder.classList.add("entering");
      void placeholder.offsetWidth;
      placeholder.classList.remove("entering");
      placeholderSwapTimer = null;
    }, 500);
  }, 3500);

  controlPanel.append(head, directives.el);
  thoughtsPanel.append(chat.head, chat.wrap, form);
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
    await directives.ensureRunning();
    await mic?.start();
  }

  function stopMic() {
    mic?.stop();
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    chat.addMessage("user", text, Date.now() / 1000);
    input.value = "";
    input.style.height = "auto";
    syncComposerAction();
    opts.onUserMessage?.();
    await directives.ensureRunning();
    rosClient.publish(CHAT_IN_TOPIC, {
      data: JSON.stringify({ text, sender: "user", timestamp: Date.now() / 1000, origin: selfOrigin }),
    });
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

  async function loadHistory() {
    if (loadingHistory) return;
    loadingHistory = true;
    try {
      const res = await rosClient.callService(GET_CHAT_HISTORY_SERVICE, {});
      const raw = String(res?.history ?? "");
      if (raw === lastSnapshot) return;
      const entries = JSON.parse(raw || "[]");
      if (!Array.isArray(entries) || !entries.length) return;
      lastSnapshot = raw;
      chat.replay(entries);
    } catch (err) {
      console.warn("[chat] reconcile failed:", err);
    } finally {
      loadingHistory = false;
    }
  }

  const unsubConn = rosClient.onStateChange((s) => {
    if (s === "connected") void loadHistory();
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
    chat.addMessage("user", text, Number(payload?.timestamp) || Date.now() / 1000);
    opts.onUserMessage?.();
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
    chat.routeChatOut(sender, text, ts);
    if (sender === "robot") {
      const messages = chat.wrap.querySelectorAll(".chat-msg.robot");
      opts.onRobotMessage?.(/** @type {HTMLElement | null} */ (messages[messages.length - 1] ?? null));
    }
  }, undefined, "std_msgs/msg/String");

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
    if (!name || !status) return;
    const key = String(payload?.primitive_id ?? payload?.skill_id ?? name);
    const reason = typeof payload?.reason === "string" ? payload.reason : "";
    const ts = Number(payload?.timestamp) || Date.now() / 1000;
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
    destroy() {
      sheet.destroy();
      mic?.destroy();
      directives.destroy();
      clearInterval(placeholderInterval);
      if (placeholderSwapTimer) clearTimeout(placeholderSwapTimer);
      chat.destroy();
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(historyPoll);
      window.removeEventListener("keydown", focusComposerOnEnter);
      unsubConn();
      unsubIn();
      unsubOut();
      unsubSkill();
      panel.remove();
    },
  };
}
