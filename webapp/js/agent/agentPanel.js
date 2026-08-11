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

import { copyText } from "../clipboard.js";
import {
  AGENT_STATUS_TOPIC,
  CHAT_IN_TOPIC,
  CHAT_OUT_TOPIC,
  GET_AVAILABLE_DIRECTIVES_SERVICE,
  GET_CHAT_HISTORY_SERVICE,
  RESET_BRAIN_SERVICE,
  SET_BRAIN_ACTIVE_SERVICE,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";

/**
 * @param {HTMLElement} root cockpit root — the panel mounts as a right-edge overlay.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<typeof import("../teleop/agentState.js").createAgentState>} agentState
 * @param {{ onView: (view: "live" | "brain") => void }} opts stage-view switch,
 *   owned by the page; setView reflects a flip the page made itself (chip, Esc).
 * @returns {{ destroy: () => void, setView: (view: "live" | "brain") => void }}
 */
export function createAgentPanel(root, rosClient, agentState, opts) {
  const selfOrigin = crypto.randomUUID?.() ?? `web-${Date.now()}-${Math.random()}`;

  const panel = document.createElement("section");
  panel.className = "overlay agent-panel";

  // ---- header -------------------------------------------------------------
  const head = document.createElement("div");
  head.className = "agent-head";
  const titleEl = document.createElement("span");
  titleEl.className = "agent-title";
  titleEl.textContent = "Agent";
  const stateDot = document.createElement("span");
  stateDot.className = "agent-state-dot";
  stateDot.title = `green while the brain is active — ${AGENT_STATUS_TOPIC}`;
  // Live/Brain switch: the panel is the agent; these pick which stage shows
  // behind it — the robot's eyes, or its loop instrumented turn by turn.
  const viewSwitch = document.createElement("div");
  viewSwitch.className = "agent-views";
  viewSwitch.setAttribute("role", "group");
  viewSwitch.setAttribute("aria-label", "Stage view");
  const liveBtn = viewButton("Live", "The robot's live camera view");
  const brainBtn = viewButton("Brain", "Brain monitor — the agent loop, turn by turn (Esc returns)");
  viewSwitch.append(liveBtn, brainBtn);
  liveBtn.addEventListener("click", () => opts.onView("live"));
  brainBtn.addEventListener("click", () => opts.onView("brain"));
  /** @param {"live" | "brain"} view */
  function setView(view) {
    liveBtn.classList.toggle("active", view === "live");
    brainBtn.classList.toggle("active", view === "brain");
    liveBtn.setAttribute("aria-pressed", String(view === "live"));
    brainBtn.setAttribute("aria-pressed", String(view === "brain"));
  }
  setView("live");
  // Phones dock the panel as a bottom sheet (CSS); this expands it upward.
  const expandBtn = document.createElement("button");
  expandBtn.type = "button";
  expandBtn.className = "agent-expand";
  expandBtn.setAttribute("aria-label", "Expand panel");
  expandBtn.textContent = "\u25b4";
  expandBtn.onclick = () => {
    const expanded = panel.classList.toggle("expanded");
    expandBtn.textContent = expanded ? "\u25be" : "\u25b4";
    expandBtn.setAttribute("aria-label", expanded ? "Collapse panel" : "Expand panel");
  };
  head.append(titleEl, stateDot, viewSwitch, expandBtn);

  // ---- directive + start/stop --------------------------------------------
  const controls = document.createElement("div");
  controls.className = "agent-controls";

  const directiveSelect = document.createElement("select");
  directiveSelect.className = "agent-directive mono";
  directiveSelect.setAttribute("aria-label", "Directive");
  directiveSelect.title = `Pick the directive to run — ${GET_AVAILABLE_DIRECTIVES_SERVICE}`;

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "agent-toggle";
  toggleBtn.title = SET_BRAIN_ACTIVE_SERVICE;

  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "agent-reset";
  resetBtn.title = `Reset the agent's brain / working memory — ${RESET_BRAIN_SERVICE}`;
  resetBtn.textContent = "Reset";

  controls.append(directiveSelect, toggleBtn, resetBtn);

  // ---- active skill -------------------------------------------------------
  const activeSkill = document.createElement("div");
  activeSkill.className = "agent-activeskill";
  const activeSkillLabel = document.createElement("span");
  activeSkillLabel.className = "microlabel";
  activeSkillLabel.textContent = "active skill";
  const activeSkillName = document.createElement("span");
  activeSkillName.className = "agent-activeskill-name mono";
  activeSkillName.textContent = "—";
  activeSkill.title = `the skill the brain is executing right now — ${SKILL_STATUS_UPDATE_TOPIC}`;
  activeSkill.append(activeSkillLabel, activeSkillName);

  // ---- live stream (thoughts + chat + skill runs) -------------------------
  const streamLabel = document.createElement("p");
  streamLabel.className = "microlabel agent-stream-label";
  streamLabel.textContent = "AI thoughts";
  streamLabel.title = `the brain's thoughts, chat, and skill runs — ${CHAT_OUT_TOPIC}`;

  const stream = document.createElement("div");
  stream.className = "agent-stream";

  // ---- composer -----------------------------------------------------------
  const form = document.createElement("form");
  form.className = "agent-compose";
  const input = document.createElement("textarea");
  input.className = "agent-compose-input";
  input.rows = 1;
  input.placeholder = "Message the agent…";
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "agent-compose-send";
  send.textContent = "Send";
  send.title = CHAT_IN_TOPIC;
  form.append(input, send);

  panel.append(head, controls, activeSkill, streamLabel, stream, form);
  root.append(panel);

  // ---- directive roster + start/stop --------------------------------------
  // The dropdown ARMS a directive; Start activates it. While active, switching
  // the dropdown switches the running directive live. brain-active drives the
  // toggle label (Start <-> Stop). We remember the last non-empty directive so
  // Stop -> Start resumes the same one even though the brain reports "" when idle.
  let lastDirective = "";
  let applying = false;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let flashTimer = null;

  function renderRoster() {
    if (flashTimer) return; // keep the copy-feedback row until it expires
    const { agents, broken, currentDirective, brainActive } = agentState.get();
    if (currentDirective) lastDirective = currentDirective;

    directiveSelect.replaceChildren();
    if (agents.length === 0) {
      directiveSelect.appendChild(new Option("No agents available", ""));
    } else {
      for (const a of agents) directiveSelect.appendChild(new Option(a.name, a.id));
    }
    // Agents that failed to load stay visible instead of silently vanishing:
    // a picker row with the start of the load error, the full error in the
    // tooltip, and selecting the row copies it to the clipboard.
    for (const b of broken ?? []) {
      const preview = b.error.length > 60 ? b.error.slice(0, 59) + "…" : b.error;
      const opt = new Option(`⚠ ${b.name} — ${preview}`, `__broken__:${b.id}`);
      opt.title = `${b.error}\n\nSelect to copy the full error.`;
      opt.dataset.error = b.error;
      directiveSelect.appendChild(opt);
    }
    // Show the running directive when active, else the armed/last one. With no
    // prior choice, default to "Demo Agent" if the roster has it.
    const demo = agents.find((a) => /demo\s*agent/i.test(a.name) || /demo/i.test(a.id));
    const armed = currentDirective || lastDirective || demo?.id || (agents[0]?.id ?? "");
    if (armed) directiveSelect.value = armed;

    panel.classList.toggle("active", brainActive);
    stateDot.classList.toggle("on", brainActive);
    // A running loop makes the Brain segment glow — an invitation to look inside.
    brainBtn.classList.toggle("pulse", brainActive);

    toggleBtn.textContent = applying ? "…" : brainActive ? "Stop" : "Start";
    toggleBtn.classList.toggle("stop", brainActive);
    toggleBtn.disabled = applying || (!brainActive && agents.length === 0);
    // Keep the picker openable when only broken agents exist, so their rows
    // stay reachable; the change handler ignores non-agent values.
    directiveSelect.disabled = applying || (agents.length === 0 && (broken ?? []).length === 0);
    resetBtn.disabled = applying;
    input.placeholder = brainActive ? "Message the agent…" : "Message the agent… (sending starts it)";
  }

  /** @param {() => Promise<any>} fn */
  async function withApplying(fn) {
    // A real action supersedes the copy-feedback flash: without this, the
    // flash guard in renderRoster would hide the applying state for up to 1.2s.
    if (flashTimer) {
      clearTimeout(flashTimer);
      flashTimer = null;
    }
    applying = true;
    renderRoster();
    try {
      await fn();
    } finally {
      applying = false;
      renderRoster();
    }
  }

  directiveSelect.addEventListener("change", () => {
    const id = directiveSelect.value;
    if (id.startsWith("__broken__:")) {
      // Copy the full load error, flash the result in the closed box, then
      // re-render (which re-arms the previous choice).
      const opt = directiveSelect.selectedOptions[0];
      void copyText(opt?.dataset.error ?? "").then(
        () => {
          if (opt) opt.text = "✓ load error copied";
        },
        () => {
          if (opt) opt.text = "✗ copy failed — see tooltip";
        },
      );
      flashTimer = setTimeout(() => {
        flashTimer = null;
        renderRoster();
      }, 1200);
      return;
    }
    if (!id) return;
    lastDirective = id;
    // Switch live only when already running; otherwise just arm for Start.
    if (agentState.get().brainActive) void withApplying(() => agentState.setDirective(id));
  });

  // No local "started./stopped." echo: the brain announces both on
  // /brain/chat_out (and into history), so every client — not just the one
  // whose button was pressed — renders the same message via the normal
  // chat_out path.
  toggleBtn.addEventListener("click", () => {
    const { brainActive } = agentState.get();
    if (brainActive) {
      void withApplying(() => agentState.setDirective(""));
    } else {
      const selected = directiveSelect.value.startsWith("__broken__:") ? "" : directiveSelect.value;
      const id = selected || lastDirective;
      if (id) void withApplying(() => agentState.setDirective(id));
    }
  });

  resetBtn.addEventListener("click", () => {
    if (!window.confirm("Reset the agent's brain? This clears its working memory.")) return;
    agentState.resetBrain().catch(() => {});
  });

  const unsubAgents = agentState.subscribe(renderRoster);

  // ---- stream helpers -----------------------------------------------------

  // Sticky-bottom scrolling. Capture whether we're pinned to the bottom BEFORE
  // mutating the stream, then snap down afterwards only if we were. Measuring
  // *after* appending is the bug that left tall robot replies off-screen: the
  // freshly added height makes the distance-from-bottom exceed any threshold, so
  // it wrongly concludes the user had scrolled up.
  function atBottom() {
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
  }
  /** @param {boolean} wasAtBottom */
  function snapIfAtBottom(wasAtBottom) {
    if (wasAtBottom) stream.scrollTop = stream.scrollHeight;
  }

  /** @type {{ wrap: HTMLElement, status: HTMLElement, list: HTMLElement, lastByKind: Record<string, string>, startTs: number, latestTs: number } | null} */
  let thoughts = null;
  let lastTs = 0;

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
      stream.appendChild(wrap);
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
    snapIfAtBottom(wasAtBottom);
  }

  /** @param {string} kind @param {string} text @param {number} ts @param {string} [label] */
  function addMessage(kind, text, ts, label) {
    const wasAtBottom = atBottom();
    finalizeThoughts();
    const el = document.createElement("div");
    el.className = `chat-msg ${kind}`;
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
    stream.appendChild(el);
    lastTs = ts;
    snapIfAtBottom(wasAtBottom);
  }

  /** @type {Map<string, { wrap: HTMLElement, head: HTMLElement, status: HTMLElement, hasDetail: boolean }>} */
  const skillRuns = new Map();

  /** @param {string} key @param {string} name @param {string} status @param {number} ts @param {string} [reason] */
  function addSkillRun(key, name, status, ts, reason) {
    const wasAtBottom = atBottom();
    const cls = ["running", "completed", "failed", "interrupted"].includes(status) ? status : "running";
    // Reflect the running primitive in the Active Skill readout.
    if (cls === "running") {
      activeSkillName.textContent = name.replace(/_/g, " ");
      activeSkill.classList.add("on");
    } else if (activeSkillName.textContent === name.replace(/_/g, " ")) {
      activeSkillName.textContent = "—";
      activeSkill.classList.remove("on");
    }

    let run = skillRuns.get(key);
    if (!run) {
      finalizeThoughts();
      const wrap = document.createElement("div");
      wrap.className = "chat-skill";
      const head = document.createElement("div");
      head.className = "chat-skill-head";
      const tag = document.createElement("span");
      tag.className = "chat-skill-tag mono";
      tag.textContent = "skill";
      const nameEl = document.createElement("span");
      nameEl.className = "chat-skill-name";
      nameEl.textContent = name.replace(/_/g, " ");
      const statusEl = document.createElement("span");
      statusEl.className = "chat-skill-status mono";
      statusEl.title = SKILL_STATUS_UPDATE_TOPIC;
      head.append(tag, nameEl, statusEl);
      wrap.append(head);
      stream.appendChild(wrap);
      run = { wrap, head, status: statusEl, hasDetail: false };
      skillRuns.set(key, run);
    }
    run.wrap.className = `chat-skill ${cls}`;
    if (run.hasDetail) run.wrap.classList.add("has-detail");
    run.status.textContent = cls;

    // A failed run carries the failure reason / error — show it expanded
    // in-place (collapsible so a long trace can be tucked away).
    if (cls === "failed" && reason && !run.hasDetail) {
      run.hasDetail = true;
      run.wrap.classList.add("has-detail", "open");
      run.head.title = "Click to show/hide the failure reason";
      const chevron = document.createElement("span");
      chevron.className = "chat-skill-chevron mono";
      chevron.textContent = "▴";
      run.head.appendChild(chevron);
      const detail = document.createElement("div");
      detail.className = "chat-skill-detail";
      detail.textContent = reason;
      run.wrap.appendChild(detail);
      run.head.addEventListener("click", () => {
        const open = run.wrap.classList.toggle("open");
        chevron.textContent = open ? "▴" : "▾";
      });
    }

    lastTs = ts;
    if (cls !== "running") skillRuns.delete(key);
    snapIfAtBottom(wasAtBottom);
  }

  // ---- composer -----------------------------------------------------------
  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    addMessage("user", text, Date.now() / 1000);
    input.value = "";
    input.style.height = "auto";
    // Always jump to our own message, even if we'd scrolled up reading earlier.
    stream.scrollTop = stream.scrollHeight;
    // Messaging an idle agent means "start it" — an inactive brain drops chat_in.
    if (!agentState.get().brainActive) {
      const id = directiveSelect.value || lastDirective;
      if (id) await withApplying(() => agentState.setDirective(id));
    }
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
  });

  // ---- history backfill ---------------------------------------------------
  // Live topics only carry messages from after we subscribe, so a fresh page
  // load starts blank. The brain keeps the full conversation; pull it once on
  // connect and replay it through the same renderers, then keep appending live.
  let historyLoaded = false;

  async function loadHistory() {
    if (historyLoaded) return;
    let res;
    try {
      res = await rosClient.callService(GET_CHAT_HISTORY_SERVICE, {});
    } catch {
      return; // best-effort — the live stream still works without it
    }
    /** @type {any[]} */
    let entries;
    try {
      entries = JSON.parse(res?.history ?? "[]");
    } catch {
      return;
    }
    if (!Array.isArray(entries) || entries.length === 0) {
      historyLoaded = true;
      return;
    }
    // The snapshot already includes anything the live stream just showed, so
    // reset and replay it wholesale rather than trying to merge.
    stream.replaceChildren();
    thoughts = null;
    skillRuns.clear();
    lastTs = 0;
    for (const e of entries) replayEntry(e);
    historyLoaded = true;
    stream.scrollTop = stream.scrollHeight;
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
      addSkillRun(key, name, status, ts, typeof e?.failureReason === "string" ? e.failureReason : "");
      return;
    }
    const text = String(e?.text ?? "");
    if (!text) return;
    if (sender === "robot_thoughts" || sender === "robot_anticipation") {
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      return; // raw vision dumps — noisy, drop (matches live)
    } else if (sender === "user" || sender === "robot") {
      addMessage(sender, text, ts);
    } else {
      addMessage("system", text, ts, sender || undefined);
    }
  }

  const unsubConn = rosClient.onStateChange((s) => {
    if (s === "connected") void loadHistory();
  });

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
    addMessage("user", text, Number(payload?.timestamp) || Date.now() / 1000);
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
    if (sender === "robot_thoughts" || sender === "robot_anticipation") {
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      return; // raw vision dumps — noisy, drop
    } else if (sender === "user" || sender === "robot") {
      addMessage(sender, text, ts);
    } else {
      addMessage("system", text, ts, sender);
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
    addSkillRun(key, name, status, Number(payload?.timestamp) || Date.now() / 1000, reason);
  }, undefined, "std_msgs/msg/String");

  return {
    setView,
    destroy() {
      if (flashTimer) clearTimeout(flashTimer);
      unsubAgents();
      unsubConn();
      unsubIn();
      unsubOut();
      unsubSkill();
      panel.remove();
    },
  };
}

/**
 * A segment of the panel's Live/Brain stage switch.
 * @param {string} label
 * @param {string} title
 */
function viewButton(label, title) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "agent-view-btn";
  btn.textContent = label;
  btn.title = title;
  return btn;
}

/**
 * Shorten long decimals in a display string to 2 places. Cosmetic only.
 * @param {string} text
 */
function roundNums(text) {
  return text.replace(/-?\d+\.\d+(?:[eE][-+]?\d+)?/g, (n) => {
    const v = Number(n);
    return Number.isFinite(v) ? String(Math.round(v * 100) / 100) : n;
  });
}
