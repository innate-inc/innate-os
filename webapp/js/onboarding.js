// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// App-wide first-run tour. localStorage is scoped to the current origin, so a
// browser sees it once for each simulator/robot it opens. Bump the version only
// when the tour changes enough that returning operators should see it again.

export const ONBOARDING_VERSION = 1;
export const ONBOARDING_SEEN_KEY = `innate.onboardingSeen.v${ONBOARDING_VERSION}`;

/** @typedef {{ eyebrow: string, title: string, body: string, target: string | null }} TourStep */

/** @type {TourStep[]} */
export const ONBOARDING_STEPS = [
  {
    eyebrow: "Welcome to Innate OS",
    title: "Meet your robot's control room",
    body: "Use the same interface to work with MARS in simulation and on the physical robot. Here's the quick tour.",
    target: null,
  },
  {
    eyebrow: "Agent",
    title: "Tell MARS what to do",
    body: "Chat or speak with the robot, watch its reasoning, and start or stop autonomous work.",
    target: '.rail-link[data-section="agent"]',
  },
  {
    eyebrow: "Teleop",
    title: "Drive and look around",
    body: "Take direct control of the base, arm, head, cameras, and voice when you need it.",
    target: '.rail-link[data-section="teleop"]',
  },
  {
    eyebrow: "Navigation",
    title: "Map the space",
    body: "Build and inspect maps, save places, and send MARS to a destination.",
    target: '.rail-link[data-section="nav"]',
  },
  {
    eyebrow: "Settings",
    title: "Tune the system",
    body: "Adjust robot, agent, audio, camera, navigation, and developer settings here.",
    target: '.rail-link[data-section="settings"]',
  },
  {
    eyebrow: "Help",
    title: "Come back any time",
    body: "Press the question mark above Settings whenever you want to replay this tour.",
    target: ".rail-help",
  },
];

/**
 * Build the persistent onboarding controller. It owns one tour at a time and
 * exposes maybeStart() separately so the router can wait for the first page to
 * finish mounting before putting guidance over it.
 * @returns {{ maybeStart: () => void, start: () => void }}
 */
export function createOnboarding() {
  /** @type {{ close: (remember?: boolean) => void } | null} */
  let openTour = null;

  function start() {
    openTour?.close(false);
    openTour = showTour(() => {
      openTour = null;
    });
  }

  function maybeStart() {
    if (!storageGet(ONBOARDING_SEEN_KEY)) start();
  }

  return { maybeStart, start };
}

/**
 * @param {() => void} onClose
 * @returns {{ close: (remember?: boolean) => void }}
 */
function showTour(onClose) {
  const previousFocus = document.activeElement;
  let stepIndex = 0;
  let closed = false;

  const root = document.createElement("div");
  root.className = "onboarding";

  const blocker = document.createElement("div");
  blocker.className = "onboarding-blocker";

  const spotlight = document.createElement("div");
  spotlight.className = "onboarding-spotlight";

  const card = document.createElement("section");
  card.className = "onboarding-card";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  card.setAttribute("aria-labelledby", "onboarding-title");
  card.tabIndex = -1;

  const top = document.createElement("div");
  top.className = "onboarding-top";
  const eyebrow = document.createElement("span");
  eyebrow.className = "microlabel onboarding-eyebrow";
  const progress = document.createElement("span");
  progress.className = "onboarding-progress";
  top.append(eyebrow, progress);

  const title = document.createElement("h2");
  title.id = "onboarding-title";
  title.className = "onboarding-title";
  const body = document.createElement("p");
  body.className = "onboarding-body";

  const actions = document.createElement("div");
  actions.className = "onboarding-actions";
  const skip = document.createElement("button");
  skip.type = "button";
  skip.className = "onboarding-skip";
  skip.textContent = "Skip tour";
  skip.addEventListener("click", () => close(true));
  const buttons = document.createElement("div");
  buttons.className = "onboarding-buttons";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "onboarding-button onboarding-back";
  back.textContent = "Back";
  back.addEventListener("click", () => setStep(stepIndex - 1));
  const next = document.createElement("button");
  next.type = "button";
  next.className = "onboarding-button onboarding-next";
  next.addEventListener("click", () => {
    if (stepIndex === ONBOARDING_STEPS.length - 1) close(true);
    else setStep(stepIndex + 1);
  });
  buttons.append(back, next);
  actions.append(skip, buttons);
  card.append(top, title, body, actions);
  root.append(blocker, spotlight, card);
  document.body.appendChild(root);

  window.addEventListener("resize", position);
  document.addEventListener("keydown", onKey);
  setStep(0);
  card.focus();

  /** @param {KeyboardEvent} event */
  function onKey(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
      return;
    }
    if (event.key === "ArrowRight" && stepIndex < ONBOARDING_STEPS.length - 1) setStep(stepIndex + 1);
    if (event.key === "ArrowLeft" && stepIndex > 0) setStep(stepIndex - 1);
    if (event.key !== "Tab") return;
    const focusable = [...card.querySelectorAll("button:not([disabled])")];
    if (!focusable.length) return;
    const first = /** @type {HTMLElement} */ (focusable[0]);
    const last = /** @type {HTMLElement} */ (focusable[focusable.length - 1]);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /** @param {number} index */
  function setStep(index) {
    stepIndex = Math.max(0, Math.min(index, ONBOARDING_STEPS.length - 1));
    const step = ONBOARDING_STEPS[stepIndex];
    eyebrow.textContent = step.eyebrow;
    progress.textContent = `${stepIndex + 1} / ${ONBOARDING_STEPS.length}`;
    title.textContent = step.title;
    body.textContent = step.body;
    back.hidden = stepIndex === 0;
    next.textContent = stepIndex === ONBOARDING_STEPS.length - 1 ? "Done" : stepIndex === 0 ? "Show me" : "Next";
    root.classList.toggle("is-centered", !step.target);
    position();
  }

  function position() {
    const step = ONBOARDING_STEPS[stepIndex];
    const target = step.target ? document.querySelector(step.target) : null;
    if (!(target instanceof HTMLElement)) {
      spotlight.hidden = true;
      card.style.removeProperty("left");
      card.style.removeProperty("top");
      return;
    }

    spotlight.hidden = false;
    const rect = target.getBoundingClientRect();
    const pad = 5;
    spotlight.style.left = `${Math.max(4, rect.left - pad)}px`;
    spotlight.style.top = `${Math.max(4, rect.top - pad)}px`;
    spotlight.style.width = `${rect.width + pad * 2}px`;
    spotlight.style.height = `${rect.height + pad * 2}px`;

    // Rail targets have room on their right on desktop and mobile. Clamp both
    // axes so the card still fits on compact phone screens and landscape iPads.
    const gap = 18;
    const cardWidth = card.offsetWidth || Math.min(360, window.innerWidth - 24);
    const measuredHeight = card.offsetHeight || 240;
    const left = Math.min(window.innerWidth - cardWidth - 12, rect.right + gap);
    const topPos = Math.max(12, Math.min(window.innerHeight - measuredHeight - 12, rect.top + rect.height / 2 - measuredHeight / 2));
    card.style.left = `${Math.max(12, left)}px`;
    card.style.top = `${topPos}px`;
  }

  /** @param {boolean} [remember] */
  function close(remember = false) {
    if (closed) return;
    closed = true;
    if (remember) storageSet(ONBOARDING_SEEN_KEY, "1");
    window.removeEventListener("resize", position);
    document.removeEventListener("keydown", onKey);
    root.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
    onClose();
  }

  return { close };
}

/** localStorage can throw in private/locked-down browser modes. */
/** @param {string} key @returns {string | null} */
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
    // The tour still closes; it will simply return next time on this browser.
  }
}
