// @ts-check
// Agent chat transcript — thoughts, chat messages, and skill runs in one
// scrollable stream, in Compact or Detailed mode.
//
// Owns all transcript state (scroll position, the open thought group, live skill
// runs, and the skill-run streak). That state is mutually entangled — a single
// message can close a thought group, break a streak, move the scroll and animate
// an entry — so it lives in one module rather than being threaded between
// several. Nothing here talks to ROS: the panel feeds it messages.

import { CHAT_OUT_TOPIC, SKILL_STATUS_UPDATE_TOPIC } from "../constants.js";
import {
  formatSkillArgs,
  modeButton,
  renderSkillFailure,
  renderSkillParameters,
  roundNums,
  setSkillElementOpen,
  setSkillRunOpen,
  skillDisplayName,
  skillGroupStatus,
  skillNameKey,
  skillStatusLabel,
} from "./skillFormat.js";

// Runs of the same skill collapse into one group at this many in a row.
const SKILL_GROUP_MIN = 3;

/**
 * @param {{ onActiveSkill?: (name: string | null) => void }} [opts]
 *   onActiveSkill reports the skill currently running so the panel can update
 *   its "active skill" chip — that chip lives in the control panel, not here.
 * @returns {{
 *   head: HTMLElement,
 *   wrap: HTMLElement,
 *   addThought: (kind: string, text: string, ts: number) => void,
 *   addMessage: (kind: string, text: string, ts: number, label?: string) => void,
 *   addSkillRun: (key: string, name: string, status: string, ts: number, reason: string, args: any) => void,
 *   routeChatOut: (sender: string, text: string, ts: number) => void,
 *   replay: (entries: any[]) => void,
 *   setMode: (mode: "compact" | "detailed") => void,
 *   destroy: () => void,
 * }}
 */
export function createChatStream(opts = {}) {
  // ---- live stream (thoughts + chat + skill runs) -------------------------
  const streamLabel = document.createElement("p");
  streamLabel.className = "microlabel agent-stream-label";
  streamLabel.textContent = "Chat with MARS";
  streamLabel.title = `MARS chat, thoughts, and skill runs — ${CHAT_OUT_TOPIC}`;
  const streamHead = document.createElement("div");
  streamHead.className = "agent-stream-head";
  const streamMode = document.createElement("div");
  streamMode.className = "agent-stream-mode";
  streamMode.setAttribute("role", "group");
  streamMode.setAttribute("aria-label", "Chat detail");
  const compactBtn = modeButton("Compact");
  const detailedBtn = modeButton("Detailed");
  streamMode.append(compactBtn, detailedBtn);
  streamHead.append(streamLabel, streamMode);

  const streamWrap = document.createElement("div");
  streamWrap.className = "agent-stream-wrap";
  const stream = document.createElement("div");
  stream.className = "agent-stream compact";
  streamWrap.append(stream);
  compactBtn.classList.add("active");
  compactBtn.setAttribute("aria-pressed", "true");
  detailedBtn.setAttribute("aria-pressed", "false");
  compactBtn.addEventListener("click", () => setStreamMode("compact"));
  detailedBtn.addEventListener("click", () => setStreamMode("detailed"));
  // ---- stream helpers -----------------------------------------------------

  // Follow new output while the reader is already at the bottom. Sending a new
  // prompt explicitly returns there; incoming output never yanks scrollback
  // away from someone reading an earlier turn.
  function atBottom() {
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
  }
  /** @param {boolean} wasAtBottom */
  function settleStreamAfterMutation(wasAtBottom) {
    if (wasAtBottom) stream.scrollTop = stream.scrollHeight;
  }

  /** @param {HTMLElement} el */
  function appendStreamItem(el) {
    stream.append(el);
  }

  /** @type {{ wrap: HTMLElement, status: HTMLElement, list: HTMLElement, lastByKind: Record<string, string>, startTs: number, latestTs: number } | null} */
  let thoughts = null;
  let lastTs = 0;
  let replayingHistory = false;
  /** @type {Set<ReturnType<typeof setTimeout>>} */
  const compactEnterTimers = new Set();
  /** @type {Map<string, {
   *  wrap: HTMLElement,
   *  head: HTMLButtonElement,
   *  summary: HTMLElement,
   *  status: HTMLElement,
   *  parameters: HTMLElement,
   *  failure: HTMLElement,
   *  hasDetail: boolean
   * }>} */
  const skillRuns = new Map();
  /** @type {{
   *  name: string,
   *  wraps: HTMLElement[],
   *  group: HTMLElement | null,
   *  list: HTMLElement | null,
   * } | null} */
  let skillStreak = null;
  // Name shown in the panel's active-skill chip, so a finishing run only
  // clears the chip when it is the run that set it.
  let runningSkill = "";

  /** @param {"compact" | "detailed"} mode */
  function setStreamMode(mode) {
    const compact = mode === "compact";
    stream.classList.toggle("compact", compact);
    streamMode.classList.toggle("detailed-selected", !compact);
    compactBtn.classList.toggle("active", compact);
    detailedBtn.classList.toggle("active", !compact);
    compactBtn.setAttribute("aria-pressed", String(compact));
    detailedBtn.setAttribute("aria-pressed", String(!compact));
    for (const card of stream.querySelectorAll(".chat-skill.failed.has-detail")) {
      const head = card.querySelector(".chat-skill-head");
      if (card instanceof HTMLElement && head instanceof HTMLButtonElement) {
        setSkillElementOpen(card, head, !compact);
      }
    }
    stream.scrollTop = stream.scrollHeight;
  }

  /** @param {HTMLElement} el */
  function animateCompactEnter(el) {
    if (!stream.classList.contains("compact") || replayingHistory) return;
    el.classList.add("compact-entering");
    const timer = setTimeout(() => {
      el.classList.remove("compact-entering");
      compactEnterTimers.delete(timer);
    }, 180);
    compactEnterTimers.add(timer);
  }

  /** @param {boolean} active */
  function setThoughtsStatus(active) {
    if (!thoughts) return;
    const d = Math.max(0, Math.ceil(thoughts.latestTs - (lastTs || thoughts.startTs)));
    const word = active ? "Processing" : "Processed";
    thoughts.status.textContent = d > 0 ? `${word} for ${d} second${d === 1 ? "" : "s"}` : `${word}…`;
    thoughts.wrap.classList.toggle("active", active);
  }

  function finalizeThoughts() {
    if (thoughts) setThoughtsStatus(false);
    thoughts = null;
  }

  /** @param {string} kind @param {string} text @param {number} ts */
  function addThought(kind, text, ts) {
    const wasAtBottom = atBottom();
    if (!thoughts) {
      // A thought block lands between the runs, so they are no longer
      // contiguous — promoting the streak would hoist later runs above it.
      skillStreak = null;
      const wrap = document.createElement("div");
      // Collapsed by default — only the latest thought (preview) shows until expanded.
      wrap.className = "chat-thoughts";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "chat-thoughts-toggle";
      toggle.title = "The model's internal reasoning for this turn — click to expand";
      const label = document.createElement("span");
      label.className = "chat-thoughts-label";
      label.textContent = "Thoughts";
      const arrow = document.createElement("span");
      arrow.className = "chat-thoughts-arrow";
      arrow.textContent = "▾";
      const status = document.createElement("span");
      status.className = "chat-thoughts-status";
      toggle.append(label, status, arrow);
      const list = document.createElement("div");
      list.className = "chat-thoughts-list";
      toggle.addEventListener("click", () => {
        const wasAtBottom = atBottom();
        const open = wrap.classList.toggle("open");
        arrow.textContent = open ? "▴" : "▾";
        settleStreamAfterMutation(wasAtBottom);
      });
      wrap.append(toggle, list);
      appendStreamItem(wrap);
      thoughts = { wrap, status, list, lastByKind: {}, startTs: ts, latestTs: ts };
    }
    thoughts.latestTs = ts;
    if (thoughts.lastByKind[kind] !== text) {
      thoughts.lastByKind[kind] = text;
      const item = document.createElement("div");
      item.className = "chat-thought-item";
      item.textContent = roundNums(text);
      thoughts.list.appendChild(item);
    }
    setThoughtsStatus(true);
    settleStreamAfterMutation(wasAtBottom);
  }

  /** @param {string} kind @param {string} text @param {number} ts @param {string} [label] */
  function addMessage(kind, text, ts, label) {
    const wasAtBottom = atBottom();
    finalizeThoughts();
    if (kind === "user" || kind === "robot") skillStreak = null;
    const el = document.createElement("div");
    el.className = `chat-msg ${kind}`;
    el.classList.toggle("skill-output", label === "skill_output");
    if (kind === "system") {
      const tag = document.createElement("span");
      tag.className = "chat-sender mono";
      tag.textContent = (label || "system").replace(/_/g, " ");
      el.appendChild(tag);
    }
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = kind === "user" ? text : roundNums(text);
    el.appendChild(bubble);
    appendStreamItem(el);
    if (label !== "skill_output") animateCompactEnter(el);
    lastTs = ts;
    if (kind === "user") {
      stream.scrollTop = stream.scrollHeight;
    } else {
      settleStreamAfterMutation(wasAtBottom);
    }
  }

  /** @param {string} name */
  function startSkillStreak(name) {
    const streak = {
      name,
      wraps: [],
      group: null,
      list: null,
    };
    skillStreak = streak;
    return streak;
  }

  /** @param {string} name @param {HTMLElement} wrap @returns {boolean} */
  function attachSkillToStreak(name, wrap) {
    const key = skillNameKey(name);
    const streak = !skillStreak || skillStreak.name !== key ? startSkillStreak(key) : skillStreak;
    streak.wraps.push(wrap);
    if (streak.wraps.length < SKILL_GROUP_MIN) return false;
    if (!streak.group) promoteSkillStreak(streak);
    else streak.list?.append(wrap);
    if (streak.group) refreshSkillGroupEl(streak.group);
    return true;
  }

  /** @param {NonNullable<typeof skillStreak>} streak */
  function promoteSkillStreak(streak) {
    const group = document.createElement("div");
    group.className = "chat-skill-group";
    const head = document.createElement("button");
    head.type = "button";
    head.className = "chat-skill-head";
    head.setAttribute("aria-expanded", "false");
    const icon = document.createElement("span");
    icon.className = "chat-skill-icon";
    icon.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "chat-skill-copy";
    const title = document.createElement("span");
    title.className = "chat-skill-name-row";
    const nameEl = document.createElement("span");
    nameEl.className = "chat-skill-name";
    const firstName = streak.wraps[0]?.querySelector(".chat-skill-name");
    nameEl.textContent = firstName instanceof HTMLElement ? firstName.textContent : streak.name;
    const count = document.createElement("span");
    count.className = "chat-skill-count";
    title.append(nameEl, count);
    copy.append(title);
    const statusEl = document.createElement("span");
    statusEl.className = "chat-skill-status";
    const chevron = document.createElement("span");
    chevron.className = "chat-skill-chevron";
    chevron.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9,6 15,12 9,18"/></svg>';
    chevron.setAttribute("aria-hidden", "true");
    head.append(icon, copy, statusEl, chevron);
    const list = document.createElement("div");
    list.className = "chat-skill-group-list";
    group.append(head, list);
    const first = streak.wraps[0];
    first?.before(group);
    list.append(...streak.wraps);
    head.addEventListener("click", () => {
      const wasAtBottom = atBottom();
      const open = !group.classList.contains("open");
      setSkillElementOpen(group, head, open);
      head.title = open ? "Hide repeated skill calls" : "Show each skill call";
      settleStreamAfterMutation(wasAtBottom);
    });
    head.title = "Show each skill call";
    streak.group = group;
    streak.list = list;
    animateCompactEnter(group);
  }

  /** @param {HTMLElement} group */
  function refreshSkillGroupEl(group) {
    const wraps = [...group.querySelectorAll(":scope > .chat-skill-group-list > .chat-skill")];
    const head = group.querySelector(":scope > .chat-skill-head");
    const count = head?.querySelector(".chat-skill-count");
    const statusEl = head?.querySelector(".chat-skill-status");
    const nameEl = head?.querySelector(".chat-skill-name");
    if (!(head instanceof HTMLButtonElement) || !count || !statusEl) return;
    const n = wraps.length;
    count.textContent = `${n} runs`;
    const cls = skillGroupStatus(wraps);
    group.classList.remove("running", "completed", "failed", "interrupted");
    group.classList.add(cls);
    statusEl.textContent = skillStatusLabel(cls);
    const skill = nameEl instanceof HTMLElement ? nameEl.textContent : "skill";
    head.setAttribute("aria-label", `${skill}, ${n} calls, ${statusEl.textContent}`);
  }

  /** @param {HTMLElement} wrap */
  function refreshStreakContaining(wrap) {
    const group = wrap.closest(".chat-skill-group");
    if (group instanceof HTMLElement) refreshSkillGroupEl(group);
  }

  /** @param {string} key @param {string} name @param {string} status @param {number} ts @param {string} [reason]
   *  @param {any} [args] */
  function addSkillRun(key, name, status, ts, reason, args) {
    const wasAtBottom = atBottom();
    const cls = ["running", "completed", "failed", "interrupted"].includes(status) ? status : "running";
    const displayName = skillDisplayName(name);
    if (cls === "running") {
      runningSkill = displayName;
      opts.onActiveSkill?.(displayName);
    } else if (runningSkill === displayName) {
      runningSkill = "";
      opts.onActiveSkill?.(null);
    }

    let run = skillRuns.get(key);
    if (!run) {
      finalizeThoughts();
      const wrap = document.createElement("div");
      wrap.className = "chat-skill";
      const head = document.createElement("button");
      head.type = "button";
      head.className = "chat-skill-head";
      head.setAttribute("aria-expanded", "false");
      const icon = document.createElement("span");
      icon.className = "chat-skill-icon";
      icon.setAttribute("aria-hidden", "true");
      const copy = document.createElement("span");
      copy.className = "chat-skill-copy";
      const nameEl = document.createElement("span");
      nameEl.className = "chat-skill-name";
      nameEl.textContent = displayName;
      const summary = document.createElement("span");
      summary.className = "chat-skill-summary";
      copy.append(nameEl, summary);
      const statusEl = document.createElement("span");
      statusEl.className = "chat-skill-status";
      statusEl.title = SKILL_STATUS_UPDATE_TOPIC;
      const chevron = document.createElement("span");
      chevron.className = "chat-skill-chevron";
      chevron.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9,6 15,12 9,18"/></svg>';
      chevron.setAttribute("aria-hidden", "true");
      head.append(icon, copy, statusEl, chevron);
      const detail = document.createElement("div");
      detail.className = "chat-skill-detail";
      const parameters = document.createElement("div");
      parameters.className = "chat-skill-parameters";
      const failure = document.createElement("div");
      failure.className = "chat-skill-failure";
      detail.append(parameters, failure);
      wrap.append(head, detail);
      const createdRun = { wrap, head, summary, status: statusEl, parameters, failure, hasDetail: false };
      run = createdRun;
      head.addEventListener("click", () => {
        if (!createdRun.hasDetail) return;
        const wasAtBottom = atBottom();
        setSkillRunOpen(createdRun, !createdRun.wrap.classList.contains("open"));
        settleStreamAfterMutation(wasAtBottom);
      });
      skillRuns.set(key, createdRun);
      if (!attachSkillToStreak(name, wrap)) {
        appendStreamItem(wrap);
        animateCompactEnter(wrap);
      }
    }
    run.wrap.classList.remove("running", "completed", "failed", "interrupted");
    run.wrap.classList.add(cls);
    const inputs = formatSkillArgs(name, args);
    if (inputs.rows.length) {
      run.summary.textContent = inputs.summary;
      renderSkillParameters(run.parameters, inputs.rows);
      run.hasDetail = true;
    }
    run.status.textContent = skillStatusLabel(cls);
    run.head.setAttribute("aria-label", `${displayName}: ${run.status.textContent}`);

    if (cls === "failed" && reason) {
      renderSkillFailure(run.failure, reason);
      run.hasDetail = true;
    }
    run.wrap.classList.toggle("has-detail", run.hasDetail);
    run.head.title = run.hasDetail ? "Show skill details" : "";
    if (cls === "failed") setSkillRunOpen(run, !stream.classList.contains("compact"));
    refreshStreakContaining(run.wrap);

    lastTs = ts;
    if (cls !== "running") skillRuns.delete(key);
    settleStreamAfterMutation(wasAtBottom);
  }

  // ---- history replay -----------------------------------------------------

  /** Route one chat_out-shaped message to the right renderer. Live messages and
   *  replayed history entries share this so the two paths cannot drift.
   *  @param {string} sender @param {string} text @param {number} ts */
  function routeChatOut(sender, text, ts) {
    if (sender === "robot_thoughts" || sender === "robot_anticipation") {
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      return; // raw vision dumps — noisy, drop
    } else if (sender === "user" || sender === "robot") {
      addMessage(sender, text, ts);
    } else {
      addMessage("system", text, ts, sender || undefined);
    }
  }

  /** Render one stored history entry, mirroring the live chat_out routing.
   *  @param {any} e */
  function replayEntry(e) {
    const ts = Number(e?.timestamp) || Date.now() / 1000;
    const sender = String(e?.sender ?? "");
    if (sender === "task_activated") {
      const name = String(e?.text ?? e?.skill_name ?? e?.skillId ?? "");
      const status = String(e?.taskStatus ?? "");
      if (!name || !status) return;
      const key = String(e?.primitiveId ?? e?.skillId ?? name);
      addSkillRun(key, name, status, ts, typeof e?.failureReason === "string" ? e.failureReason : "", e?.args);
      return;
    }
    const text = String(e?.text ?? "");
    if (!text) return;
    routeChatOut(sender, text, ts);
  }

  /** Replace the transcript with a history snapshot. The snapshot already
   *  includes anything the live stream just showed, so reset and replay it
   *  wholesale rather than trying to merge.
   *  @param {any[]} entries */
  function replay(entries) {
    stream.replaceChildren();
    for (const timer of compactEnterTimers) clearTimeout(timer);
    compactEnterTimers.clear();
    thoughts = null;
    skillRuns.clear();
    skillStreak = null;
    lastTs = 0;
    replayingHistory = true;
    stream.classList.add("replaying");
    try {
      for (const e of entries) replayEntry(e);
    } finally {
      replayingHistory = false;
      stream.classList.remove("replaying");
    }
    stream.scrollTop = stream.scrollHeight;
  }

  return {
    head: streamHead,
    wrap: streamWrap,
    addThought,
    addMessage,
    addSkillRun,
    routeChatOut,
    replay,
    setMode: setStreamMode,
    destroy() {
      for (const timer of compactEnterTimers) clearTimeout(timer);
    },
  };
}
