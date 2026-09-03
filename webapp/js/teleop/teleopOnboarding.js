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
    title: "Pick something up",
    body: "Open Skills, choose Pick Any Object, and describe what to grab — try “the red LEGO brick”.",
  },
  agent: {
    eyebrow: "Direct control complete",
    title: "Now try Agent",
    body: "Agent lets you give MARS a goal and combine skills through conversation.",
  },
};

export const SPEECH_UNAVAILABLE_STEP = {
  eyebrow: "2 of 3 · Speech",
  title: "Speech is not configured",
  body: "Add an INNATE_SERVICE_KEY and restart Innate OS to enable Cartesia speech. You can skip this step for now.",
};

// Worn by a step's card while its skill is in flight — never a step of its own,
// so it never reaches stored progress. The eyebrow is the step's own, so the
// counter does not move while the robot works.
const RUNNING_BODY = /** @type {const} */ ({
  wave: "Wave is a recorded episode playing back — someone moved the arm once, and MARS repeats it.",
  pick: "MARS is looking for the object, lining the gripper up, and closing on it.",
});

/** @param {keyof typeof RUNNING_BODY} step */
export const runningCopy = (step) => ({
  eyebrow: TELEOP_ONBOARDING_STEPS[step].eyebrow,
  title: "Watch it happen",
  body: RUNNING_BODY[step],
});

const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
const shortcut = isMac ? "⌘K" : "Ctrl+K";
const NEXT_STEP = /** @type {const} */ ({ intro: "wave", wave: "talk", talk: "pick", pick: "agent" });
// The speech step advances on send, but MARS is still mid-sentence then; hold
// the card long enough to hear it before the step changes underneath.
export const SPEAK_ADVANCE_MS = 2000;
const PREV_STEP = /** @type {const} */ ({ wave: "intro", talk: "wave", pick: "talk", agent: "pick" });

/** @param {string} id */
const skillName = (id) => String(id).split("/").at(-1)?.replace(/[-_]+/g, " ").toLowerCase() ?? "";

/** @param {{skillId: string}} run */
export const isWaveCompletion = (run) => skillName(run.skillId) === "wave";

/** @param {{skillId: string}} run */
export const isPickupCompletion = (run) => skillName(run.skillId) === "pick any object";

/** Unavailable capabilities remain visible and explain how to enable them.
 * @param {keyof typeof TELEOP_ONBOARDING_STEPS} step @param {boolean | null} _speechAvailable */
export const resolveAvailableStep = (step, _speechAvailable) => step;

/** @param {keyof typeof PREV_STEP} step @param {boolean | null} _speechAvailable */
export const resolvePreviousStep = (step, _speechAvailable) => PREV_STEP[step];

/** @param {keyof typeof TELEOP_ONBOARDING_STEPS} step @param {boolean | null} speechAvailable */
export const copyForStep = (step, speechAvailable) =>
  step === "talk" && speechAvailable === false ? SPEECH_UNAVAILABLE_STEP : TELEOP_ONBOARDING_STEPS[step];

/**
 * Keep coaching for the bottom controls visually attached to the highlighted
 * control without covering it. The 76px minimum clears the persistent rail.
 * @param {{left: number, right: number, top: number}} targetRect
 * @param {{width: number, height: number}} cardSize
 * @param {{width: number, height: number}} viewport
 */
export function positionAboveTarget(targetRect, cardSize, viewport) {
  const edge = 12;
  const railEdge = 76;
  const gap = 16;
  const maxLeft = Math.max(edge, viewport.width - cardSize.width - edge);
  const minLeft = Math.min(railEdge, maxLeft);
  return {
    left: Math.max(
      minLeft,
      Math.min(maxLeft, targetRect.left + (targetRect.right - targetRect.left - cardSize.width) / 2),
    ),
    top: Math.max(edge, Math.min(viewport.height - cardSize.height - edge, targetRect.top - cardSize.height - gap)),
  };
}

/**
 * Expand and clamp a spotlight hole so the shade never touches its subject.
 * @param {{left: number, right: number, top: number, bottom: number}} rect
 * @param {{width: number, height: number}} viewport
 * @param {number} [padding]
 */
export function paddedSpotlightRect(rect, viewport, padding = 14) {
  const left = Math.max(0, rect.left - padding);
  const top = Math.max(0, rect.top - padding);
  const right = Math.min(viewport.width, rect.right + padding);
  const bottom = Math.min(viewport.height, rect.bottom + padding);
  return { left, top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
}

/**
 * Join adjacent interface pieces before rounding the spotlight. This avoids an
 * inward scallop where, for example, the Skills menu meets its trigger button.
 * @param {{left: number, right: number, top: number, bottom: number}[]} rects
 */
export function boundingSpotlightRect(rects) {
  return {
    left: Math.min(...rects.map((rect) => rect.left)),
    right: Math.max(...rects.map((rect) => rect.right)),
    top: Math.min(...rects.map((rect) => rect.top)),
    bottom: Math.max(...rects.map((rect) => rect.bottom)),
  };
}

let maskId = 0;

/**
 * Guided first-run mission for direct control. Steps advance only when the
 * corresponding command is actually sent and, for skills, succeeds.
 * @param {HTMLElement} root
 * @param {{ prepareLego?: () => void }} [options] Sets the manipulation
 * target down as the pick step opens; a no-op on hardware, where the user puts
 * a real object in front of the robot instead.
 */
export function createTeleopOnboarding(root, { prepareLego } = {}) {
  /** @type {HTMLElement | null} */
  let card = null;
  /** @type {HTMLElement | null} */
  let target = null;
  /** @type {SVGSVGElement | null} */
  let mask = null;
  /** @type {SVGRectElement[]} */
  let spotlightHoles = [];
  /** @type {keyof typeof TELEOP_ONBOARDING_STEPS | null} */
  let step = null;
  let skillsMenuOpen = false;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let speakAdvance = null;
  let running = false;
  /** @type {boolean | null} */
  let speechAvailable = null;

  /** @param {CustomEvent<{restart?: boolean}>} event */
  function onRequest(event) {
    if (event.detail?.restart) storageRemove(TELEOP_ONBOARDING_PROGRESS_KEY);
    const saved = storageGet(TELEOP_ONBOARDING_PROGRESS_KEY);
    show(isStep(saved) ? saved : "intro");
  }

  /** Enter a step: commit the progress, set the world up, paint the card.
   * @param {keyof typeof TELEOP_ONBOARDING_STEPS} next */
  function show(next) {
    next = resolveAvailableStep(next, speechAvailable);
    step = next;
    running = false;
    storageSet(TELEOP_ONBOARDING_PROGRESS_KEY, next);
    if (next === "pick") prepareLego?.();
    render(copyForStep(next, speechAvailable));
  }

  /** Repaint the current step with different copy — no progress write, and no
   * second brick, so a running skill is never interrupted by its own card.
   * @param {{eyebrow: string, title: string, body: string}} copy */
  function render(copy) {
    const next = step;
    if (!next) return;
    close(false);
    const centered = next === "intro";
    target = centered || running ? null : targetFor(next);
    target?.classList.add("agent-onboarding-target");

    if (centered) {
      root.classList.add("agent-onboarding-focused");
      document.body.classList.add("agent-onboarding-active");
    }
    // A running skill gets no shade at all: the card has stepped aside to let
    // the robot be watched, and spotlighting a control it no longer points at
    // would darken the scene instead.
    if (!running) {
      mask = createSpotlightMask(centered ? 0 : 3);
      root.appendChild(mask);
    }

    card = document.createElement("aside");
    card.className = `agent-onboarding-card teleop-onboarding-card is-${next}${running ? " is-running" : ""}`;
    card.setAttribute("role", "status");
    card.innerHTML =
      `<span class="microlabel agent-onboarding-eyebrow">${copy.eyebrow}</span>` +
      `<h2>${copy.title}</h2>` +
      `<p>${copy.body.replace("{shortcut}", shortcut)}</p>`;

    const actions = document.createElement("div");
    actions.className = "agent-onboarding-actions";
    actions.appendChild(
      actionButton("Skip tour", "agent-onboarding-dismiss agent-onboarding-quit", () => close(true)),
    );

    // Mid-run the card offers only the way out; the skill's own Stop is in the menu.
    if (!running) appendNavigation(actions, next);
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

  /** @param {HTMLElement} actions @param {keyof typeof TELEOP_ONBOARDING_STEPS} next */
  function appendNavigation(actions, next) {
    if (next === "intro") {
      actions.appendChild(actionButton("Show me", "agent-onboarding-primary", () => show("wave")));
      return;
    }
    const previous = resolvePreviousStep(next, speechAvailable);
    actions.appendChild(actionButton("Back", "agent-onboarding-dismiss", () => show(previous)));
    if (next !== "agent") {
      const nextStep = NEXT_STEP[next];
      actions.appendChild(actionButton("Skip step", "agent-onboarding-dismiss", () => show(nextStep)));
      return;
    }
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

  function position() {
    if (!card) return;
    if (step === "intro") {
      sizeSpotlightMask();
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
      updateSpotlightRects([
        boundingSpotlightRect([target.getBoundingClientRect(), popRect]),
        card.getBoundingClientRect(),
      ]);
      return;
    }
    card.style.width = "";
    const rect = target.getBoundingClientRect();
    const width = card.offsetWidth || 350;
    const height = card.offsetHeight || 190;
    if (step === "wave" || step === "talk" || step === "pick") {
      const point = positionAboveTarget(
        rect,
        { width, height },
        { width: window.innerWidth, height: window.innerHeight },
      );
      card.style.left = `${point.left}px`;
      card.style.top = `${point.top}px`;
      updateSpotlight([target, card]);
      return;
    }
    let left = rect.left - width - 16;
    if (left < 76) left = Math.min(window.innerWidth - width - 12, rect.right + 16);
    const top = Math.max(12, Math.min(window.innerHeight - height - 12, rect.top + rect.height / 2 - height / 2));
    card.style.left = `${Math.max(76, left)}px`;
    card.style.top = `${top}px`;
    updateSpotlight([target, card]);
  }

  /** @param {number} holeCount */
  function createSpotlightMask(holeCount) {
    const svgNs = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNs, "svg");
    svg.classList.add("agent-onboarding-mask");
    svg.setAttribute("aria-hidden", "true");
    const defs = document.createElementNS(svgNs, "defs");
    const definition = document.createElementNS(svgNs, "mask");
    const id = `teleop-onboarding-mask-${maskId += 1}`;
    definition.id = id;
    definition.setAttribute("maskUnits", "userSpaceOnUse");
    const field = document.createElementNS(svgNs, "rect");
    field.classList.add("agent-onboarding-mask-field");
    definition.appendChild(field);
    spotlightHoles = [];
    for (let index = 0; index < holeCount; index += 1) {
      const hole = document.createElementNS(svgNs, "rect");
      hole.classList.add("agent-onboarding-mask-hole");
      hole.setAttribute("rx", "18");
      definition.appendChild(hole);
      spotlightHoles.push(hole);
    }
    defs.appendChild(definition);
    const shade = document.createElementNS(svgNs, "rect");
    shade.classList.add("agent-onboarding-mask-shade");
    shade.setAttribute("mask", `url(#${id})`);
    svg.append(defs, shade);
    return svg;
  }

  function sizeSpotlightMask() {
    if (!mask) return;
    const width = window.innerWidth;
    const height = window.innerHeight;
    mask.setAttribute("viewBox", `0 0 ${width} ${height}`);
    for (const rect of mask.querySelectorAll(".agent-onboarding-mask-field, .agent-onboarding-mask-shade")) {
      rect.setAttribute("x", "0");
      rect.setAttribute("y", "0");
      rect.setAttribute("width", String(width));
      rect.setAttribute("height", String(height));
    }
  }

  /** @param {HTMLElement[]} subjects */
  function updateSpotlight(subjects) {
    updateSpotlightRects(subjects.map((subject) => subject.getBoundingClientRect()));
  }

  /** @param {{left: number, right: number, top: number, bottom: number}[]} subjects */
  function updateSpotlightRects(subjects) {
    sizeSpotlightMask();
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    spotlightHoles.forEach((hole, index) => {
      const subject = subjects[index];
      if (!subject) {
        hole.setAttribute("width", "0");
        hole.setAttribute("height", "0");
        return;
      }
      const rect = paddedSpotlightRect(subject, viewport);
      hole.setAttribute("x", String(rect.left));
      hole.setAttribute("y", String(rect.top));
      hole.setAttribute("width", String(rect.width));
      hole.setAttribute("height", String(rect.height));
    });
  }

  /** @param {boolean} remember */
  function close(remember) {
    if (speakAdvance) clearTimeout(speakAdvance);
    speakAdvance = null;
    card?.remove();
    card = null;
    mask?.remove();
    mask = null;
    spotlightHoles = [];
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

  /** @param {{skillId: string}} run */
  function onSkillStarted(run) {
    if (step === "wave" && isWaveCompletion(run)) startRunning("wave");
    if (step === "pick" && isPickupCompletion(run)) startRunning("pick");
  }

  /** @param {keyof typeof RUNNING_BODY} which */
  function startRunning(which) {
    running = true;
    render(runningCopy(which));
  }

  /** @param {{skillId: string}} run */
  function onSkillCompleted(run) {
    if (step === "wave" && isWaveCompletion(run)) show("talk");
    if (step === "pick" && isPickupCompletion(run)) show("agent");
  }

  /** A failed or cancelled run leaves the card claiming MARS is still working.
   * @param {{skillId: string, ok: boolean}} run */
  function onSkillEnded(run) {
    if (run.ok || !running || !step) return;
    running = false;
    render(TELEOP_ONBOARDING_STEPS[step]);
  }

  function onSpeak() {
    if (step !== "talk" || speakAdvance) return;
    speakAdvance = setTimeout(() => {
      speakAdvance = null;
      show("pick");
    }, SPEAK_ADVANCE_MS);
  }

  /** @param {boolean} available */
  function onSpeechAvailabilityChange(available) {
    const changed = speechAvailable !== available;
    speechAvailable = available;
    if (changed && step === "talk") render(copyForStep("talk", speechAvailable));
  }

  function onSkillsMenuOpenChange(open) {
    skillsMenuOpen = open;
    requestAnimationFrame(position);
  }

  window.addEventListener(ONBOARDING_REQUEST_EVENT, /** @type {EventListener} */ (onRequest));
  window.addEventListener("resize", position);

  return {
    onSkillStarted,
    onSkillCompleted,
    onSkillEnded,
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

/** @param {string} label @param {string} className @param {() => void} onClick */
function actionButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
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
