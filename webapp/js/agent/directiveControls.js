// @ts-check
// Directive controls — the agent picker plus Start/Stop and Reset, i.e. the
// row that decides *which* brain runs and whether it is running.
//
// Split from agentPanel because it answers to agentState rather than to any
// chat traffic. It reports the selected agent's name and the run state back to
// the panel, which owns the header and the panel-level "active" styling.

import { copyText } from "../clipboard.js";
import {
  GET_AVAILABLE_DIRECTIVES_SERVICE,
  RESET_BRAIN_SERVICE,
  SET_BRAIN_ACTIVE_SERVICE,
} from "../constants.js";

/**
 * @param {ReturnType<typeof import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {{
 *   listId: string,
 *   onAgentName: (name: string) => void,
 *   onBrainActive: (active: boolean, justStarted: boolean) => void,
 * }} opts
 * @returns {{ el: HTMLElement, toggleEl: HTMLButtonElement, ensureRunning: () => Promise<void>, destroy: () => void }}
 */
export function createDirectiveControls(agentState, opts) {
  const controls = document.createElement("div");
  controls.className = "agent-controls";

  const directivePicker = document.createElement("div");
  directivePicker.className = "agent-directive-picker";
  const directiveButton = document.createElement("button");
  directiveButton.type = "button";
  directiveButton.className = "agent-directive mono";
  directiveButton.setAttribute("role", "combobox");
  directiveButton.setAttribute("aria-label", "Agent");
  directiveButton.setAttribute("aria-haspopup", "listbox");
  directiveButton.setAttribute("aria-expanded", "false");
  directiveButton.title = `Pick the directive to run — ${GET_AVAILABLE_DIRECTIVES_SERVICE}`;
  const directiveValue = document.createElement("span");
  directiveValue.className = "agent-directive-value";
  directiveValue.textContent = "No agents available";
  const directiveChevron = document.createElement("span");
  directiveChevron.className = "agent-directive-chevron";
  directiveChevron.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
  directiveButton.append(directiveValue, directiveChevron);
  const directiveList = document.createElement("div");
  directiveList.className = "agent-directive-list mono";
  directiveList.id = opts.listId;
  directiveList.setAttribute("role", "listbox");
  directiveList.setAttribute("aria-label", "Agents");
  directiveButton.setAttribute("aria-controls", directiveList.id);
  directivePicker.append(directiveButton, directiveList);

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "agent-toggle";

  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "agent-reset";
  resetBtn.title = `Reset the agent's brain / working memory — ${RESET_BRAIN_SERVICE}`;
  resetBtn.setAttribute("aria-label", "Reset agent");
  resetBtn.innerHTML = '<span class="agent-reset-icon" aria-hidden="true"></span>';

  controls.append(directivePicker, toggleBtn, resetBtn);
  // ---- directive roster + start/stop --------------------------------------
  // The dropdown ARMS a directive; Start activates it. While active, switching
  // the dropdown switches the running directive live. brain-active drives the
  // toggle label (Start <-> Stop). We remember the last non-empty directive so
  // Stop -> Start resumes the same one even though the brain reports "" when idle.
  let lastDirective = "";
  let applying = false;
  let wasBrainActive = false;
  let directiveOpen = false;
  let selectedDirective = "";
  /** @type {ReturnType<typeof setTimeout> | null} */
  let flashTimer = null;

  /** @param {boolean} open */
  function setDirectiveOpen(open) {
    directiveOpen = open && !directiveButton.disabled;
    directivePicker.classList.toggle("open", directiveOpen);
    directiveButton.setAttribute("aria-expanded", String(directiveOpen));
    if (!directiveOpen) return;
    const selected = directiveList.querySelector('[aria-selected="true"]');
    const first = directiveList.querySelector('[role="option"]');
    const target = /** @type {HTMLElement | null} */ (selected ?? first);
    requestAnimationFrame(() => target?.focus());
  }

  /** @param {string} id @param {string | undefined} error */
  function chooseDirective(id, error) {
    setDirectiveOpen(false);
    if (error !== undefined) {
      void copyText(error).then(
        () => {
          directiveValue.textContent = "Load error copied";
        },
        () => {
          directiveValue.textContent = "Copy failed";
        },
      );
      flashTimer = setTimeout(() => {
        flashTimer = null;
        renderRoster();
      }, 1200);
      return;
    }
    if (!id) return;
    selectedDirective = id;
    lastDirective = id;
    const agent = agentState.get().agents.find((candidate) => candidate.id === id);
    directiveValue.textContent = agent?.name ?? id;
    opts.onAgentName(directiveValue.textContent ?? "");
    if (agentState.get().brainActive) void withApplying(() => agentState.setDirective(id));
  }

  /** @param {KeyboardEvent} event */
  function onDirectiveKeydown(event) {
    const options = /** @type {HTMLElement[]} */ ([...directiveList.querySelectorAll('[role="option"]')]);
    if (event.currentTarget === directiveButton && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      setDirectiveOpen(true);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setDirectiveOpen(false);
      directiveButton.focus();
      return;
    }
    const current = Math.max(0, options.indexOf(/** @type {HTMLElement} */ (document.activeElement)));
    let next = current;
    if (event.key === "ArrowDown") next = Math.min(current + 1, options.length - 1);
    else if (event.key === "ArrowUp") next = Math.max(current - 1, 0);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    else return;
    event.preventDefault();
    options[next]?.focus();
  }

  /** @param {MouseEvent} event */
  function onDirectiveOutsideClick(event) {
    if (!directivePicker.contains(/** @type {Node} */ (event.target))) setDirectiveOpen(false);
  }

  directiveButton.addEventListener("click", () => setDirectiveOpen(!directiveOpen));
  directiveButton.addEventListener("keydown", onDirectiveKeydown);
  directiveList.addEventListener("keydown", onDirectiveKeydown);
  directivePicker.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", onDirectiveOutsideClick);

  function renderRoster() {
    if (flashTimer) return; // keep the copy-feedback row until it expires
    const { agents, broken, currentDirective, brainActive } = agentState.get();
    if (currentDirective) lastDirective = currentDirective;

    const demo = agents.find((a) => /demo\s*agent/i.test(a.name) || /demo/i.test(a.id));
    const armed = currentDirective || lastDirective || demo?.id || (agents[0]?.id ?? "");
    directiveList.replaceChildren();
    for (const agent of agents) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "agent-directive-option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(agent.id === armed));
      option.innerHTML =
        '<span class="agent-directive-check" aria-hidden="true"><svg viewBox="0 0 14 11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m1.5 5.5 3.3 3.3 7.7-7.3"/></svg></span>';
      const name = document.createElement("span");
      name.className = "agent-directive-option-name";
      name.textContent = agent.name;
      option.append(name);
      option.addEventListener("click", () => chooseDirective(agent.id, undefined));
      directiveList.append(option);
    }
    for (const b of broken ?? []) {
      const preview = b.error.length > 60 ? b.error.slice(0, 59) + "…" : b.error;
      const option = document.createElement("button");
      option.type = "button";
      option.className = "agent-directive-option broken";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.title = `${b.error}\n\nSelect to copy the full error.`;
      const warning = document.createElement("span");
      warning.className = "agent-directive-warning";
      warning.textContent = "!";
      const copy = document.createElement("span");
      copy.className = "agent-directive-option-copy";
      const name = document.createElement("span");
      name.className = "agent-directive-option-name";
      name.textContent = b.name;
      const error = document.createElement("span");
      error.className = "agent-directive-option-error";
      error.textContent = preview;
      copy.append(name, error);
      option.append(warning, copy);
      option.addEventListener("click", () => chooseDirective("", b.error));
      directiveList.append(option);
    }
    if (agents.length === 0 && (broken ?? []).length === 0) {
      const empty = document.createElement("span");
      empty.className = "agent-directive-empty";
      empty.textContent = "No agents available";
      directiveList.append(empty);
    }
    const selectedAgent = agents.find((agent) => agent.id === armed);
    selectedDirective = selectedAgent?.id ?? "";
    directiveValue.textContent = selectedAgent?.name ?? "No agents available";
    opts.onAgentName(selectedAgent?.name ?? "No agent");

    opts.onBrainActive(brainActive, brainActive && !wasBrainActive);
    wasBrainActive = brainActive;

    const action = brainActive ? "Stop" : "Start";
    const agentName = selectedAgent?.name ?? "agent";
    toggleBtn.textContent = action;
    toggleBtn.title = `${action} ${agentName} — ${SET_BRAIN_ACTIVE_SERVICE}`;
    toggleBtn.setAttribute("aria-label", `${action} ${agentName}`);
    toggleBtn.setAttribute("aria-pressed", String(brainActive));
    toggleBtn.setAttribute("aria-busy", String(applying));
    toggleBtn.classList.toggle("stop", brainActive);
    toggleBtn.disabled = applying || (!brainActive && agents.length === 0);
    // Keep the picker openable when only broken agents exist, so their rows
    // stay reachable; the change handler ignores non-agent values.
    directiveButton.disabled = applying || (agents.length === 0 && (broken ?? []).length === 0);
    if (directiveButton.disabled) setDirectiveOpen(false);
    resetBtn.disabled = applying;
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

  // No local "started./stopped." echo: the brain announces both on
  // /brain/chat_out (and into history), so every client — not just the one
  // whose button was pressed — renders the same message via the normal
  // chat_out path.
  toggleBtn.addEventListener("click", () => {
    const { brainActive } = agentState.get();
    if (brainActive) {
      void withApplying(() => agentState.setDirective(""));
    } else {
      const id = selectedDirective || lastDirective;
      if (id) void withApplying(() => agentState.setDirective(id));
    }
  });

  resetBtn.addEventListener("click", () => {
    if (!window.confirm("Reset the agent's brain? This clears its working memory.")) return;
    agentState.resetBrain().catch(() => {});
  });

  const unsubAgents = agentState.subscribe(renderRoster);

  // Talking to an idle agent means "start it" — an inactive brain drops
  // chat_in, and the mic device is only open while a directive runs.
  async function ensureRunning() {
    if (agentState.get().brainActive) return;
    const id = selectedDirective || lastDirective;
    if (id) await withApplying(() => agentState.setDirective(id));
  }

  return {
    el: controls,
    // The compact sheet parks this in its header; moved, not duplicated.
    toggleEl: toggleBtn,
    ensureRunning,
    destroy() {
      document.removeEventListener("click", onDirectiveOutsideClick);
      if (flashTimer) clearTimeout(flashTimer);
      unsubAgents();
    },
  };
}
