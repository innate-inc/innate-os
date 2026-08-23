// @ts-check
// Agent chat transcript — thoughts, chat messages, and skill runs in one
// scrollable stream, in Compact or Detailed mode.
//
// Owns all transcript state (scroll pinning, the open thought group, live skill
// runs, and the skill-run streak). That state is mutually entangled — a single
// message can close a thought group, break a streak, pin the prompt and animate
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
 *   addDebugEvent: (event: any) => void,
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
  // Extra scroll range lets a short current turn align to the top.
  const compactSpacer = document.createElement("div");
  compactSpacer.className = "agent-stream-compact-spacer";
  compactSpacer.setAttribute("aria-hidden", "true");
  stream.append(compactSpacer);
  const earlierBtn = document.createElement("button");
  earlierBtn.type = "button";
  earlierBtn.className = "agent-stream-earlier";
  earlierBtn.setAttribute("aria-label", "Earlier messages");
  earlierBtn.setAttribute("aria-hidden", "true");
  earlierBtn.title = "Earlier messages";
  earlierBtn.tabIndex = -1;
  earlierBtn.innerHTML =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6,14 12,8 18,14"/></svg>';
  streamWrap.append(stream, earlierBtn);
  compactBtn.classList.add("active");
  compactBtn.setAttribute("aria-pressed", "true");
  detailedBtn.setAttribute("aria-pressed", "false");
  compactBtn.addEventListener("click", () => setStreamMode("compact"));
  detailedBtn.addEventListener("click", () => setStreamMode("detailed"));
  // ---- stream helpers -----------------------------------------------------

  // Detailed stays sticky-bottom. Compact pins the current user prompt to the
  // top (older turns sit above it) so each send feels like a fresh page.
  function atBottom() {
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
  }
  /** @param {boolean} wasAtBottom */
  function settleStreamAfterAppend(wasAtBottom) {
    if (stream.classList.contains("compact")) {
      sizeCompactSpacer();
      updateScrollHint();
      return;
    }
    if (wasAtBottom) stream.scrollTop = stream.scrollHeight;
    updateScrollHint();
  }

  function updateScrollHint() {
    const show =
      stream.classList.contains("compact") &&
      compactPrompt !== null &&
      hasEarlierMessages(compactPrompt) &&
      stream.scrollTop > 0;
    streamWrap.classList.toggle("can-scroll-up", show);
    earlierBtn.setAttribute("aria-hidden", String(!show));
    earlierBtn.tabIndex = show ? 0 : -1;
  }

  /** @type {HTMLElement | null} */
  let compactPrompt = null;

  /** @param {HTMLElement} el */
  function appendStreamItem(el) {
    stream.insertBefore(el, compactSpacer);
  }

  function sizeCompactSpacer() {
    if (!stream.classList.contains("compact") || !compactPrompt) {
      if (compactSpacer.style.height !== "0px") compactSpacer.style.height = "0px";
      return;
    }
    const styles = getComputedStyle(stream);
    const gap = Number.parseFloat(styles.rowGap) || 0;
    const topInset = Number.parseFloat(styles.paddingTop) || 0;
    let used = compactPrompt.offsetHeight;
    for (
      let node = compactPrompt.nextElementSibling;
      node && node !== compactSpacer;
      node = node.nextElementSibling
    ) {
      if (!(node instanceof HTMLElement)) continue;
      if (getComputedStyle(node).display === "none") continue;
      used += node.offsetHeight + gap;
    }
    const next = `${Math.max(0, stream.clientHeight - used - topInset)}px`;
    if (compactSpacer.style.height !== next) compactSpacer.style.height = next;
  }

  /** @param {HTMLElement} el */
  function hasEarlierMessages(el) {
    for (let node = el.previousElementSibling; node; node = node.previousElementSibling) {
      if (!(node instanceof HTMLElement)) continue;
      if (getComputedStyle(node).display === "none") continue;
      return true;
    }
    return false;
  }

  /** @param {HTMLElement} el */
  function pinCompactPrompt(el) {
    compactPrompt = el;
    if (!stream.classList.contains("compact") || replayingHistory) {
      updateScrollHint();
      return;
    }
    sizeCompactSpacer();
    const topInset = Number.parseFloat(getComputedStyle(stream).paddingTop) || 0;
    const promptTop = el.getBoundingClientRect().top;
    const streamTop = stream.getBoundingClientRect().top;
    stream.scrollTop += promptTop - streamTop - topInset;
    updateScrollHint();
  }

  function pinLatestCompactTurn() {
    const users = stream.querySelectorAll(".chat-msg.user");
    const prompt = compactPrompt ?? users[users.length - 1] ?? null;
    if (!(prompt instanceof HTMLElement)) {
      sizeCompactSpacer();
      updateScrollHint();
      return;
    }
    pinCompactPrompt(prompt);
  }

  /** @type {{ wrap: HTMLElement, status: HTMLElement, list: HTMLElement, lastByKind: Record<string, string>, startTs: number, latestTs: number } | null} */
  let thoughts = null;
  let lastTs = 0;
  let latestSpeechStopAt = 0;
  let latestTranscriptAt = 0;
  let pendingTranscripts = 0;
  let responseTranscriptCount = 0;
  let replyReportedForTranscriptAt = 0;
  let voiceStartedForTranscriptAt = 0;
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
    if (compact) pinLatestCompactTurn();
    else {
      compactSpacer.style.height = "0px";
      stream.scrollTop = stream.scrollHeight;
      updateScrollHint();
    }
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

  earlierBtn.addEventListener("click", () => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    stream.scrollBy({
      top: -Math.max(stream.clientHeight * 0.75, 120),
      behavior: reduced ? "auto" : "smooth",
    });
  });
  stream.addEventListener("scroll", updateScrollHint, { passive: true });
  const streamResize = new ResizeObserver(() => {
    if (stream.classList.contains("compact")) sizeCompactSpacer();
    updateScrollHint();
  });
  streamResize.observe(stream);

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
        const open = wrap.classList.toggle("open");
        arrow.textContent = open ? "▴" : "▾";
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
    settleStreamAfterAppend(wasAtBottom);
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
      pinCompactPrompt(el);
      if (!stream.classList.contains("compact")) stream.scrollTop = stream.scrollHeight;
    } else {
      settleStreamAfterAppend(wasAtBottom);
    }
  }

  /** @param {any} rawEvent */
  function addDebugEvent(rawEvent) {
    const event = enrichDebugTiming(rawEvent);
    if (event.suppressed) return;
    const description = describeDebugEvent(event);
    if (!description) return;
    const wasAtBottom = atBottom();
    skillStreak = null;
    const el = document.createElement("div");
    el.className = `chat-debug ${description.level}`;
    const source = document.createElement("span");
    source.className = "chat-debug-source mono";
    const sourceName = String(event?.source ?? "speech").toUpperCase();
    source.textContent = event?.utterance_id ? `${sourceName} #${event.utterance_id}` : sourceName;
    const copy = document.createElement("span");
    copy.className = "chat-debug-copy";
    const titleRow = document.createElement("span");
    titleRow.className = "chat-debug-title-row";
    const title = document.createElement("span");
    title.className = "chat-debug-title";
    title.textContent = description.title;
    const time = document.createElement("time");
    time.className = "chat-debug-time mono";
    time.dateTime = new Date(timestampMs(event?.timestamp)).toISOString();
    time.textContent = clockTime(event?.timestamp);
    titleRow.append(title, time);
    copy.append(titleRow);
    if (description.detail) {
      const detail = document.createElement("span");
      detail.className = "chat-debug-detail mono";
      detail.textContent = description.detail;
      copy.append(detail);
    }
    el.append(source, copy);
    appendStreamItem(el);
    settleStreamAfterAppend(wasAtBottom);
  }

  /** @param {any} rawEvent */
  function enrichDebugTiming(rawEvent) {
    const event = { ...rawEvent };
    const at = timestampMs(event.timestamp);
    if (event.source === "stt" && event.phase === "speech_started") {
      latestSpeechStopAt = 0;
    } else if (event.source === "stt" && event.phase === "utterance_closed") {
      latestSpeechStopAt = at;
    } else if (event.source === "stt" && event.phase === "transcript_ready") {
      event.stop_to_transcript_ms =
        Number(event.stop_to_transcript_ms) ||
        (latestSpeechStopAt ? Math.max(0, at - latestSpeechStopAt) : null);
      latestTranscriptAt = at;
      pendingTranscripts += 1;
    } else if (event.source === "tts" && event.phase === "audio_started") {
      if (latestTranscriptAt && voiceStartedForTranscriptAt === latestTranscriptAt) {
        event.suppressed = true;
        return event;
      }
      voiceStartedForTranscriptAt = latestTranscriptAt;
      responseTranscriptCount = claimResponseTranscripts(
        pendingTranscripts,
        responseTranscriptCount,
      );
      pendingTranscripts = 0;
      event.transcript_to_voice_ms = latestTranscriptAt
        ? Math.max(0, at - latestTranscriptAt)
        : 0;
      event.stop_to_voice_ms = latestSpeechStopAt
        ? Math.max(0, at - latestSpeechStopAt)
        : 0;
      event.bundled_transcripts = responseTranscriptCount;
    }
    return event;
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
      const open = !group.classList.contains("open");
      setSkillElementOpen(group, head, open);
      head.title = open ? "Hide repeated skill calls" : "Show each skill call";
      sizeCompactSpacer();
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
        setSkillRunOpen(createdRun, !createdRun.wrap.classList.contains("open"));
        sizeCompactSpacer();
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
    settleStreamAfterAppend(wasAtBottom);
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
      if (
        sender === "robot" &&
        !replayingHistory &&
        latestTranscriptAt &&
        replyReportedForTranscriptAt !== latestTranscriptAt
      ) {
        replyReportedForTranscriptAt = latestTranscriptAt;
        responseTranscriptCount = claimResponseTranscripts(
          pendingTranscripts,
          responseTranscriptCount,
        );
        pendingTranscripts = 0;
        addDebugEvent({
          source: "agent",
          phase: "response_ready",
          timestamp: ts,
          transcript_to_response_ms: Math.max(0, ts * 1000 - latestTranscriptAt),
          stop_to_response_ms: latestSpeechStopAt
            ? Math.max(0, ts * 1000 - latestSpeechStopAt)
            : 0,
          bundled_transcripts: responseTranscriptCount,
        });
      }
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
    stream.replaceChildren(compactSpacer);
    compactPrompt = null;
    for (const timer of compactEnterTimers) clearTimeout(timer);
    compactEnterTimers.clear();
    thoughts = null;
    skillRuns.clear();
    skillStreak = null;
    lastTs = 0;
    latestSpeechStopAt = 0;
    latestTranscriptAt = 0;
    pendingTranscripts = 0;
    responseTranscriptCount = 0;
    replyReportedForTranscriptAt = 0;
    voiceStartedForTranscriptAt = 0;
    replayingHistory = true;
    stream.classList.add("replaying");
    try {
      for (const e of entries) replayEntry(e);
    } finally {
      replayingHistory = false;
      stream.classList.remove("replaying");
    }
    if (stream.classList.contains("compact")) pinLatestCompactTurn();
    else {
      stream.scrollTop = stream.scrollHeight;
      updateScrollHint();
    }
  }

  return {
    head: streamHead,
    wrap: streamWrap,
    addThought,
    addMessage,
    addDebugEvent,
    addSkillRun,
    routeChatOut,
    replay,
    setMode: setStreamMode,
    destroy() {
      for (const timer of compactEnterTimers) clearTimeout(timer);
      streamResize.disconnect();
    },
  };
}

/** @param {any} event @returns {{ title: string, detail: string, level: string } | null} */
function describeDebugEvent(event) {
  const source = String(event?.source ?? "");
  const phase = String(event?.phase ?? "");
  const backend = [event?.backend, event?.engine].filter(Boolean).join(" / ");
  if (source === "stt" && phase === "speech_started") {
    return {
      title: "You are talking",
      detail: joinDetails("speech detected", vadDetail(event), endpointDetail(event), event?.capture, backend),
      level: "active",
    };
  }
  if (source === "stt" && phase === "ducking_started") {
    return {
      title: "Microphone muted while MARS speaks",
      detail: "Speech during this window is discarded",
      level: "warning",
    };
  }
  if (source === "stt" && phase === "ducking_ended") {
    return {
      title: `Microphone resumed after ${duration(event?.duration_ms)}`,
      detail: backend,
      level: "active",
    };
  }
  if (source === "stt" && phase === "utterance_closed") {
    return {
      title: `You stopped talking · ${seconds(event?.audio_seconds)} captured`,
      detail: joinDetails(
        closeReason(event),
        vadDetail(event),
        event?.pending ? `${event.pending} transcripts waiting` : "no STT backlog",
        event?.capture,
        backend,
      ),
      level: "active",
    };
  }
  if (source === "stt" && phase === "transcript_ready") {
    const stopped = event?.stop_to_transcript_ms != null;
    return {
      title: stopped
        ? `Transcript ready ${duration(event.stop_to_transcript_ms)} after you stopped`
        : `Transcript ready ${duration(event?.total_ms)} after speech detection`,
      detail: joinDetails(
        event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : "",
        event?.queue_ms != null ? `${duration(event.queue_ms)} queued` : "",
        event?.transcribe_ms != null ? `${duration(event.transcribe_ms)} API` : "",
        event?.characters != null ? `${event.characters} characters` : "",
        vadDetail(event),
        event?.audio_queue_chunks ? `${event.audio_queue_chunks} audio chunks buffered` : "",
        event?.dropped_audio_chunks ? `${event.dropped_audio_chunks} audio chunks dropped` : "",
        event?.capture,
        backend,
      ),
      level: "success",
    };
  }
  if (source === "stt" && phase === "no_speech") {
    return {
      title: `No speech recognized after ${duration(event?.total_ms)}`,
      detail: joinDetails(
        event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : "",
        vadDetail(event),
        audioDiagnosticDetail(event),
        captureDetail(event),
        backend,
      ),
      level: "warning",
    };
  }
  if (source === "stt" && phase === "utterance_rejected") {
    return {
      title: "Not enough voiced audio to transcribe",
      detail: joinDetails(
        event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : "",
        closeReason(event),
        vadDetail(event),
        audioDiagnosticDetail(event),
        captureDetail(event),
        backend,
      ),
      level: "warning",
    };
  }
  if (source === "stt" && phase === "utterance_dropped") {
    return {
      title: "Dropped an utterance because transcription was backlogged",
      detail: event?.audio_seconds != null ? `${seconds(event.audio_seconds)} audio` : backend,
      level: "error",
    };
  }
  if (source === "stt" && phase === "transcription_failed") {
    return {
      title: `Transcription failed after ${duration(event?.transcribe_ms)}`,
      detail: joinDetails(String(event?.error ?? ""), backend),
      level: "error",
    };
  }
  if (source === "stt" && phase === "audio_stalled") {
    return {
      title: `Microphone capture produced no audio for ${seconds(event?.empty_seconds)}`,
      detail: joinDetails(event?.capture, backend),
      level: "error",
    };
  }
  if (source === "stt" && phase === "audio_resumed") {
    return {
      title: `Microphone audio resumed after ${duration(event?.stalled_ms)}`,
      detail: joinDetails(event?.capture, backend),
      level: "success",
    };
  }
  if (source === "stt" && phase === "connection_lost") {
    return {
      title: "Realtime transcription connection lost",
      detail: joinDetails("reconnecting automatically", backend),
      level: "error",
    };
  }
  if (source === "stt" && phase === "connection_restored") {
    return {
      title: "Realtime transcription connection restored",
      detail: backend,
      level: "success",
    };
  }
  if (source === "tts" && phase === "speech_started") {
    return {
      title: "MARS speech requested",
      detail: joinDetails(
        event?.characters != null ? `${event.characters} characters` : "",
        event?.queue_ms ? `${duration(event.queue_ms)} queued` : "",
      ),
      level: "active",
    };
  }
  if (source === "tts" && phase === "audio_started") {
    return {
      title: `MARS started talking ${duration(event?.transcript_to_voice_ms)} after transcript`,
      detail: joinDetails(
        event?.stop_to_voice_ms ? `${duration(event.stop_to_voice_ms)} stop → voice` : "",
        event?.ttfb_ms != null ? `${duration(event.ttfb_ms)} TTS first byte` : "",
        event?.bundled_transcripts > 1 ? `${event.bundled_transcripts} transcripts bundled` : "",
        String(event?.output ?? ""),
      ),
      level: "success",
    };
  }
  if (source === "tts" && phase === "speech_completed") {
    return {
      title: `MARS finished ${seconds(event?.playback_seconds)} of speech`,
      detail: joinDetails(
        event?.ttfb_ms != null ? `${duration(event.ttfb_ms)} to first audio` : "",
        event?.stream_ms != null ? `${duration(event.stream_ms)} generation` : "",
        event?.total_ms != null ? `${duration(event.total_ms)} total` : "",
        String(event?.output ?? ""),
      ),
      level: "success",
    };
  }
  if (source === "tts" && phase === "speech_failed") {
    return {
      title: `MARS speech failed after ${duration(event?.total_ms)}`,
      detail: event?.characters != null ? `${event.characters} characters` : "",
      level: "error",
    };
  }
  if (source === "agent" && phase === "response_ready") {
    return {
      title: `MARS response ready ${duration(event?.transcript_to_response_ms)} after transcript`,
      detail: joinDetails(
        event?.stop_to_response_ms ? `${duration(event.stop_to_response_ms)} stop → response` : "",
        event?.bundled_transcripts > 1 ? `${event.bundled_transcripts} transcripts bundled` : "",
      ),
      level: "success",
    };
  }
  return null;
}

/** @param {unknown} value */
function duration(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms)) return "unknown time";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`;
}

/** @param {unknown} value */
function seconds(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "an unknown duration";
  return `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`;
}

/** @param {...string} values */
function joinDetails(...values) {
  return values.filter(Boolean).join(" · ");
}

/** @param {unknown} value */
function timestampMs(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : Date.now();
}

/** @param {number} pending @param {number} current */
function claimResponseTranscripts(pending, current) {
  return pending > 0 ? pending : current;
}

/** @param {any} event */
function vadDetail(event) {
  const peak = Number(event?.peak_level);
  const current = Number(event?.vad_level);
  const threshold = Number(event?.vad_threshold);
  const level = Number.isFinite(peak) ? peak : current;
  if (!Number.isFinite(level) || !Number.isFinite(threshold)) return "";
  const label = Number.isFinite(peak) ? "VAD peak" : "VAD";
  return `${label} ${level.toFixed(3)} / ${threshold.toFixed(3)} trigger`;
}

/** @param {any} event */
function audioDiagnosticDetail(event) {
  const rms = Number(event?.rms);
  const silero = Number(event?.silero_score);
  const values = [];
  if (event?.rms != null && Number.isFinite(rms)) values.push(`rolling RMS ${rms.toFixed(4)}`);
  if (event?.silero_score != null && Number.isFinite(silero)) {
    values.push(`Silero ${silero.toFixed(3)}${event?.silero_paused ? " (paused)" : ""}`);
  }
  values.push(event?.ducking ? "ducking active; audio discarded" : "ducking off");
  return values.join(" · ");
}

/** @param {any} event */
function captureDetail(event) {
  const device = String(event?.audio_device_id ?? "");
  const name = String(event?.audio_device_name ?? "");
  return joinDetails(event?.capture, name, device);
}

/** @param {any} event */
function endpointDetail(event) {
  const silence = Number(event?.silence_seconds);
  return Number.isFinite(silence) ? `${silence.toFixed(2)}s silence endpoint` : "";
}

/** @param {any} event */
function closeReason(event) {
  if (event?.close_reason === "max_duration") return "ended by 30s safety limit";
  if (event?.close_reason === "silence") return joinDetails("ended by silence", endpointDetail(event));
  return endpointDetail(event);
}

/** @param {unknown} value */
function clockTime(value) {
  const date = new Date(timestampMs(value));
  const millis = String(date.getMilliseconds()).padStart(3, "0");
  const time = date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return `${time}.${millis}`;
}
