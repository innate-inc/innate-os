// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Help is an interface tour. It never starts a skill, changes agents, or resets a scene.
import { ONBOARDING_REQUEST_EVENT } from "./onboarding.js";

const TOURS = {
  agent: [
    [".agent-compose, .agent-sheet-header", "Talk to MARS", "Give the robot a goal in your own words. Type here, or use the microphone.", "Tap this bar to open chat. Give MARS a goal by typing or using the microphone."],
    [".agent-control-panel, .agent-sheet-header", "Choose how MARS thinks", "Choose an agent and start or stop it. Each agent has its own instructions and skills.", "Start or stop MARS from this bar. Open chat to choose an agent and see its controls."],
    [".overlay-stack-top-left, .cam-strip-toggle", "See what MARS sees", "Switch between the robot’s cameras and the wider view while it works.", "Tap the eye to open the camera views, then choose the view you want to follow."],
    [".rail-help, .rail-ribbon", "Make yourself at home", "Use the sidebar to try direct control, navigation, and settings. Come back to Help whenever you need a reminder."],
  ],
  teleop: [
    [".video-stage", "Take the controls", "Drive MARS directly and watch its cameras. Release the controls to stop moving."],
    [".skills-menu-btn", "Run a skill", "Open Skills to choose an action and its settings, then run it when you’re ready."],
    [".tts-bar", "Give MARS a voice", "Type what you want the robot to say. The sound control lets you listen or mute playback."],
    [".rail-help, .rail-ribbon", "Keep exploring", "Agent combines skills through conversation. Navigation gives you the map. Help brings these tips back."],
  ],
};

/** @param {HTMLElement} root @param {"agent"|"teleop"} page */
export function createInterfaceTour(root, page) {
  let index = 0;
  let card = /** @type {HTMLElement|null} */ (null);
  let descriptionEl = /** @type {HTMLElement|null} */ (null);
  let target = /** @type {HTMLElement|null} */ (null);
  let previousFocus = /** @type {Element|null} */ (null);
  let steps = /** @type {string[][]} */ ([]);
  const observer = new ResizeObserver(() => position());
  function close() {
    observer.disconnect();
    target?.classList.remove("ui-tour-target"); target = null;
    card?.remove(); card = null; descriptionEl = null;
    window.removeEventListener("resize", position);
    document.removeEventListener("scroll", position, true);
    window.visualViewport?.removeEventListener("resize", position);
    window.visualViewport?.removeEventListener("scroll", position);
    document.removeEventListener("keydown", onKey, true);
    if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
  }
  function visibleTarget(/** @type {string} */ selector) {
    return /** @type {HTMLElement|undefined} */ (selector.split(",").flatMap(part => [...document.querySelectorAll(part.trim())]).find(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return style.visibility !== "hidden" && style.display !== "none"
        && rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0
        && rect.left < innerWidth && rect.top < innerHeight;
    }));
  }
  function position() {
    if (!card || !target) return;
    const current = visibleTarget(steps[index][0]) ?? root;
    if (current !== target) {
      observer.unobserve(target);
      target.classList.remove("ui-tour-target");
      target = current;
      target.classList.add("ui-tour-target");
      observer.observe(target);
    }
    const [, , description, compactDescription] = steps[index];
    const compact = root.classList.contains("agent-compact")
      && !["agent-compose", "agent-control-panel"].some(name => current.classList.contains(name));
    if (descriptionEl) descriptionEl.textContent = compact && compactDescription ? compactDescription : description;
    const rect = target.getBoundingClientRect();
    const viewport = window.visualViewport;
    const x = viewport?.offsetLeft ?? 0, y = viewport?.offsetTop ?? 0;
    const width = viewport?.width ?? document.documentElement.clientWidth;
    const height = viewport?.height ?? innerHeight;
    card.style.maxWidth = `${Math.max(0, width - 32)}px`;
    card.style.maxHeight = `${Math.max(0, height - 32)}px`;
    const size = card.getBoundingClientRect();
    const clampX = (/** @type {number} */ left) => Math.max(x + 16, Math.min(x + width - size.width - 16, left));
    const clampY = (/** @type {number} */ top) => Math.max(y + 16, Math.min(y + height - size.height - 16, top));
    // Prefer beside docked controls. Above/below are fallbacks for narrow screens.
    const placements = [
      {side:"left", left:rect.left - size.width - 16, top:clampY(rect.top)},
      {side:"right", left:rect.right + 16, top:clampY(rect.top)},
      {side:"top", left:clampX(rect.left), top:rect.top - size.height - 16},
      {side:"bottom", left:clampX(rect.left), top:rect.bottom + 16},
    ];
    const fits = (/** @type {typeof placements[number]} */ p) => p.left >= x + 16 && p.top >= y + 16
      && p.left + size.width <= x + width - 16 && p.top + size.height <= y + height - 16;
    const placed = placements.find(fits) ?? {side:"floating", left:clampX(rect.left), top:clampY(rect.top - size.height - 16)};
    card.dataset.placement = placed.side;
    card.style.left = `${placed.left}px`; card.style.top = `${placed.top}px`;
  }
  function show() {
    observer.disconnect();
    target?.classList.remove("ui-tour-target");
    const [selector, title] = steps[index];
    target = visibleTarget(selector) ?? root;
    target.classList.add("ui-tour-target");
    card?.remove();
    card = document.createElement("section"); card.className = "ui-tour-card";
    card.setAttribute("role", "dialog"); card.setAttribute("aria-label", title); card.tabIndex = -1;
    const top = document.createElement("div"); top.className = "ui-tour-top";
    const count = document.createElement("span"); count.className = "microlabel"; count.textContent = `Quick tour · ${index + 1} / ${steps.length}`;
    const dismiss = document.createElement("button"); dismiss.type = "button"; dismiss.textContent = "×"; dismiss.setAttribute("aria-label", "Close tour"); dismiss.addEventListener("click", close);
    top.append(count, dismiss);
    const heading = document.createElement("h2"); heading.textContent = title;
    const body = document.createElement("p");
    descriptionEl = body;
    const actions = document.createElement("div"); actions.className = "ui-tour-actions";
    const back = document.createElement("button"); back.type = "button"; back.textContent = "Back"; back.disabled = index === 0; back.addEventListener("click", () => {index--; show();});
    const next = document.createElement("button"); next.type = "button"; next.className = "ui-tour-next"; next.textContent = index === steps.length - 1 ? "Done" : "Next";
    next.addEventListener("click", () => {if (index === steps.length - 1) close(); else {index++; show();}});
    actions.append(back, next); card.append(top, heading, body, actions); document.body.append(card); position(); card.focus({preventScroll:true});
    observer.observe(root); observer.observe(target); observer.observe(card);
  }
  function onKey(/** @type {KeyboardEvent} */ event) {
    if (event.key === "Escape") {event.preventDefault(); event.stopPropagation(); close();}
  }
  function start() {
    if (root.classList.contains("agent-conversation-onboarding")) return;
    close(); previousFocus = document.activeElement;
    steps = TOURS[page].filter(([selector]) => visibleTarget(selector));
    if (!steps.length) return;
    index = 0; show();
    window.addEventListener("resize", position);
    window.visualViewport?.addEventListener("resize", position);
    window.visualViewport?.addEventListener("scroll", position);
    document.addEventListener("scroll", position, true);
    document.addEventListener("keydown", onKey, true);
  }
  window.addEventListener(ONBOARDING_REQUEST_EVENT, start);
  return {destroy() {close(); window.removeEventListener(ONBOARDING_REQUEST_EVENT, start);}};
}
