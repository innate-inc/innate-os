// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import {
  ONBOARDING_REQUEST_EVENT,
  ONBOARDING_VERSION,
  markOnboardingSeen,
  requestAgentOnboarding,
} from "../onboarding.js";

export const TELEOP_ONBOARDING_PROGRESS_KEY = `innate.teleopOnboardingProgress.v${ONBOARDING_VERSION}`;

export const TELEOP_ONBOARDING_STEPS = {
  intro: {
    eyebrow: "Welcome to Innate OS",
    title: "This is your robot",
    body: "You can move it, turn on its microphone to hear what it hears, make it talk, and trigger skills.",
  },
  wave: {
    eyebrow: "1 of 3 · Skills",
    title: "Make MARS wave",
    body: "Press {shortcut}, choose Wave, and run it.",
  },
  talk: {
    eyebrow: "2 of 3 · Speech",
    title: "Make MARS talk",
    body: "Type something in the speech bar and press Enter.",
  },
  pick: {
    eyebrow: "3 of 3 · Manipulation",
    title: "Pick up the cylinder",
    body: "Open Skills, choose Pick Any Object, and enter “The Cylinder” for prompt.",
  },
  agent: {
    eyebrow: "Direct control complete",
    title: "Now try Agent",
    body: "Agent lets you give MARS a goal and combine skills through conversation.",
  },
};

const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
const shortcut = isMac ? "⌘K" : "Ctrl+K";
const NEXT_STEP = /** @type {const} */ ({ intro: "wave", wave: "talk", talk: "pick", pick: "agent" });

/** @param {string} id */
const skillName = (id) => String(id).split("/").at(-1)?.replace(/[-_]+/g, " ").toLowerCase() ?? "";

/** @param {{skillId: string}} run */
export const isWaveCompletion = (run) => skillName(run.skillId) === "wave";

/** @param {{skillId: string, inputs: Record<string, any>}} run */
export const isCylinderPickupCompletion = (run) =>
  skillName(run.skillId) === "pick any object" &&
  String(run.inputs.prompt ?? "").trim().toLowerCase() === "the cylinder";

/** @param {keyof typeof TELEOP_ONBOARDING_STEPS} step @param {boolean | null} speechAvailable */
export const resolveAvailableStep = (step, speechAvailable) =>
  step === "talk" && speechAvailable === false ? "pick" : step;

/** @param {boolean} simControls */
export const teleopIntroBody = (simControls) => simControls
  ? "You can move it, make it talk, and trigger skills. On a physical MARS, you can also turn on its microphone to hear what it hears."
  : TELEOP_ONBOARDING_STEPS.intro.body;

/**
 * Guided first-run mission for direct control. Steps advance only when the
 * corresponding command is actually sent and, for skills, succeeds.
 * @param {HTMLElement} root
 * @param {{ simControls?: boolean }} [opts]
 */
export function createTeleopOnboarding(root, opts = {}) {
  /** @type {HTMLElement | null} */
  let card = null;
  /** @type {HTMLElement | null} */
  let target = null;
  /** @type {HTMLElement | null} */
  let mask = null;
  /** @type {keyof typeof TELEOP_ONBOARDING_STEPS | null} */
  let step = null;
  let skillsMenuOpen = false;
  /** @type {boolean | null} */
  let speechAvailable = null;

  /** @param {CustomEvent<{restart?: boolean}>} event */
  function onRequest(event) {
    if (event.detail?.restart) storageRemove(TELEOP_ONBOARDING_PROGRESS_KEY);
    const saved = storageGet(TELEOP_ONBOARDING_PROGRESS_KEY);
    show(isStep(saved) ? saved : "intro");
  }

  /** @param {keyof typeof TELEOP_ONBOARDING_STEPS} next */
  function show(next) {
    next = resolveAvailableStep(next, speechAvailable);
    close(false);
    step = next;
    storageSet(TELEOP_ONBOARDING_PROGRESS_KEY, next);
    const copy = TELEOP_ONBOARDING_STEPS[next];
    const centered = next === "intro";
    target = centered ? null : targetFor(next);
    target?.classList.add("agent-onboarding-target");

    if (centered) {
      root.classList.add("agent-onboarding-focused");
      document.body.classList.add("agent-onboarding-active");
      mask = document.createElement("div");
      mask.className = "agent-onboarding-mask";
      mask.setAttribute("aria-hidden", "true");
      const segment = document.createElement("div");
      segment.className = "agent-onboarding-mask-segment";
      mask.appendChild(segment);
      root.appendChild(mask);
    }

    card = document.createElement("aside");
    card.className = `agent-onboarding-card teleop-onboarding-card is-${next}`;
    card.setAttribute("role", "status");
    card.innerHTML =
      `<span class="microlabel agent-onboarding-eyebrow">${copy.eyebrow}</span>` +
      `<h2>${copy.title}</h2>` +
      `<p>${(next === "intro" ? teleopIntroBody(!!opts.simControls) : copy.body).replace("{shortcut}", shortcut)}</p>`;

    const actions = document.createElement("div");
    actions.className = "agent-onboarding-actions";
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "agent-onboarding-dismiss";
    skip.textContent = "Skip";
    skip.addEventListener("click", () => {
      const nextStep = next === "agent" ? null : NEXT_STEP[next];
      if (nextStep) show(nextStep);
      else close(true);
    });
    actions.appendChild(skip);

    if (next === "intro") {
      const begin = document.createElement("button");
      begin.type = "button";
      begin.className = "agent-onboarding-primary";
      begin.textContent = "Show me";
      begin.addEventListener("click", () => show("wave"));
      actions.appendChild(begin);
    } else if (next === "agent") {
      const agent = document.createElement("a");
      agent.href = "/";
      agent.className = "agent-onboarding-primary";
      agent.textContent = "Open Agent";
      agent.addEventListener("click", () => {
        storageRemove(TELEOP_ONBOARDING_PROGRESS_KEY);
        requestAgentOnboarding();
      });
      actions.appendChild(agent);
    }
    card.appendChild(actions);
    root.appendChild(card);
    requestAnimationFrame(position);
  }

  /** @param {keyof typeof TELEOP_ONBOARDING_STEPS} name */
  function targetFor(name) {
    if (name === "wave" || name === "pick") return document.querySelector(".skills-menu-btn");
    if (name === "talk") return document.querySelector(".tts-bar");
    if (name === "agent") return document.querySelector('.rail-link[data-section="agent"]');
    return null;
  }

  function position() {
    if (!card) return;
    if (step === "intro") {
      const segment = /** @type {HTMLElement | null} */ (mask?.firstElementChild);
      if (segment) setRect(segment, 0, 0, window.innerWidth, window.innerHeight);
      return;
    }
    if (!target) return;
    const skillsPop = skillsMenuOpen && (step === "wave" || step === "pick")
      ? document.querySelector(".skills-menu.open .skills-pop")
      : null;
    if (skillsPop instanceof HTMLElement) {
      const popRect = skillsPop.getBoundingClientRect();
      const availableWidth = popRect.left - 76 - 16;
      card.style.width = `${Math.max(220, Math.min(280, availableWidth))}px`;
      const width = card.offsetWidth;
      const height = card.offsetHeight || 190;
      card.style.left = `${Math.max(76, popRect.left - width - 16)}px`;
      card.style.top = `${Math.max(12, Math.min(window.innerHeight - height - 12, popRect.top))}px`;
      return;
    }
    card.style.width = "";
    const rect = target.getBoundingClientRect();
    const width = card.offsetWidth || 350;
    const height = card.offsetHeight || 190;
    let left = rect.left - width - 16;
    if (left < 76) left = Math.min(window.innerWidth - width - 12, rect.right + 16);
    const top = Math.max(12, Math.min(window.innerHeight - height - 12, rect.top + rect.height / 2 - height / 2));
    card.style.left = `${Math.max(76, left)}px`;
    card.style.top = `${top}px`;
  }

  /** @param {HTMLElement} element @param {number} left @param {number} top @param {number} width @param {number} height */
  function setRect(element, left, top, width, height) {
    element.style.left = `${left}px`;
    element.style.top = `${top}px`;
    element.style.width = `${width}px`;
    element.style.height = `${height}px`;
  }

  /** @param {boolean} remember */
  function close(remember) {
    card?.remove();
    card = null;
    mask?.remove();
    mask = null;
    target?.classList.remove("agent-onboarding-target");
    target = null;
    root.classList.remove("agent-onboarding-focused");
    document.body.classList.remove("agent-onboarding-active");
    if (remember) {
      step = null;
      storageRemove(TELEOP_ONBOARDING_PROGRESS_KEY);
      markOnboardingSeen();
    }
  }

  /** @param {{skillId: string, inputs: Record<string, any>}} run */
  function onSkillCompleted(run) {
    if (step === "wave" && isWaveCompletion(run)) show("talk");
    if (step === "pick" && isCylinderPickupCompletion(run)) show("agent");
  }

  function onSpeak() {
    if (step === "talk") show("pick");
  }

  /** @param {boolean} available */
  function onSpeechAvailabilityChange(available) {
    speechAvailable = available;
    if (!available && step === "talk") show("pick");
  }

  function onSkillsMenuOpenChange(open) {
    skillsMenuOpen = open;
    requestAnimationFrame(position);
  }

  window.addEventListener(ONBOARDING_REQUEST_EVENT, /** @type {EventListener} */ (onRequest));
  window.addEventListener("resize", position);

  return {
    onSkillCompleted,
    onSpeak,
    onSpeechAvailabilityChange,
    onSkillsMenuOpenChange,
    destroy() {
      close(false);
      window.removeEventListener(ONBOARDING_REQUEST_EVENT, /** @type {EventListener} */ (onRequest));
      window.removeEventListener("resize", position);
    },
  };
}

/** @param {string | null} value @returns {value is keyof typeof TELEOP_ONBOARDING_STEPS} */
function isStep(value) {
  return value !== null && value in TELEOP_ONBOARDING_STEPS;
}

/** @param {string} key */
function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Progress is best effort in locked-down browsers.
  }
}

/** @param {string} key */
function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Progress is best effort in locked-down browsers.
  }
}
