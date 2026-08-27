// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import { ONBOARDING_REQUEST_EVENT, markOnboardingSeen } from "../onboarding.js";

export const FIRST_NUDGE = {
  eyebrow: "Demo Agent is ready",
  title: "Say hello to MARS",
  body: "Hold the mic or type a message. Use the Agent menu above to switch personalities or task-specific agents.",
  examples: ["What can you see?", "Wave hello"],
};

export const REPLY_NUDGE = {
  eyebrow: "MARS answered",
  title: "Now make it move",
  body: "Keep talking naturally: ask it to navigate somewhere, wave, or pick up an object.",
};

/**
 * A non-modal, conversation-aware first-run coach. The first beat clears away
 * secondary controls and points at chat; the second appears only after the
 * robot has actually replied.
 * @param {HTMLElement} root
 * @returns {{ onUserMessage: () => void, onRobotMessage: (message?: HTMLElement | null) => void, destroy: () => void }}
 */
export function createAgentOnboarding(root) {
  /** @type {HTMLElement | null} */
  let card = null;
  /** @type {HTMLElement | null} */
  let target = null;
  let awaitingReply = false;

  function start() {
    close(false);
    awaitingReply = false;
    root.classList.add("agent-onboarding-focused");
    document.body.classList.add("agent-onboarding-active");
    show(FIRST_NUDGE, root.querySelector(".agent-compose"), "first");
  }

  /** @param {boolean} remember */
  function close(remember) {
    card?.remove();
    card = null;
    target?.classList.remove("agent-onboarding-target");
    target = null;
    root.classList.remove("agent-onboarding-focused");
    document.body.classList.remove("agent-onboarding-active");
    if (remember) markOnboardingSeen();
  }

  /** @param {typeof FIRST_NUDGE | typeof REPLY_NUDGE} copy @param {Element | null} anchor @param {"first" | "reply"} phase */
  function show(copy, anchor, phase) {
    close(false);
    if (phase === "first") {
      root.classList.add("agent-onboarding-focused");
      document.body.classList.add("agent-onboarding-active");
    }
    target = anchor instanceof HTMLElement ? anchor : root;
    target.classList.add("agent-onboarding-target");

    card = document.createElement("aside");
    card.className = `agent-onboarding-card is-${phase}`;
    card.setAttribute("role", "status");
    card.innerHTML =
      `<span class="microlabel agent-onboarding-eyebrow">${copy.eyebrow}</span>` +
      `<h2>${copy.title}</h2>` +
      `<p>${copy.body}</p>`;

    if (phase === "first" && "examples" in copy) {
      const examples = document.createElement("div");
      examples.className = "agent-onboarding-examples";
      for (const example of copy.examples) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.textContent = example;
        chip.addEventListener("click", () => {
          const input = root.querySelector(".agent-compose-input");
          if (!(input instanceof HTMLInputElement) && !(input instanceof HTMLTextAreaElement)) return;
          input.value = example;
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.focus();
        });
        examples.appendChild(chip);
      }
      card.appendChild(examples);
    }

    const actions = document.createElement("div");
    actions.className = "agent-onboarding-actions";
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "agent-onboarding-dismiss";
    dismiss.textContent = phase === "first" ? "I'll explore" : "Keep talking";
    dismiss.addEventListener("click", () => close(true));
    actions.appendChild(dismiss);
    if (phase === "reply") {
      const nav = document.createElement("a");
      nav.href = "/nav";
      nav.className = "agent-onboarding-primary";
      nav.textContent = "Open Navigation";
      nav.addEventListener("click", () => markOnboardingSeen());
      actions.appendChild(nav);
    }
    card.appendChild(actions);
    root.appendChild(card);
    requestAnimationFrame(position);
  }

  function position() {
    if (!card || !target) return;
    const rect = target.getBoundingClientRect();
    const width = card.offsetWidth || 340;
    const height = card.offsetHeight || 190;
    let left = rect.left - width - 16;
    if (left < 76) left = Math.min(window.innerWidth - width - 12, rect.right + 16);
    const top = Math.max(12, Math.min(window.innerHeight - height - 12, rect.top + rect.height / 2 - height / 2));
    card.style.left = `${Math.max(76, left)}px`;
    card.style.top = `${top}px`;
  }

  function onUserMessage() {
    if (!card?.classList.contains("is-first")) return;
    awaitingReply = true;
    close(false);
  }

  /** @param {HTMLElement | null} [message] */
  function onRobotMessage(message = null) {
    if (!awaitingReply) return;
    awaitingReply = false;
    show(REPLY_NUDGE, message || root.querySelector(".agent-compose"), "reply");
    markOnboardingSeen();
  }

  function onRequest() {
    start();
  }

  window.addEventListener(ONBOARDING_REQUEST_EVENT, onRequest);
  window.addEventListener("resize", position);

  return {
    onUserMessage,
    onRobotMessage,
    destroy() {
      close(false);
      window.removeEventListener(ONBOARDING_REQUEST_EVENT, onRequest);
      window.removeEventListener("resize", position);
    },
  };
}
