// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Shell — the 64px icon rail rendered into every page, and the placeholder
// renderer for not-yet-built sections.

import { ros } from "./rosClient.js";
import { initTtsAudio } from "./ttsAudio.js";
import { getConfig } from "./config.js";
import { createAgentState } from "./teleop/agentState.js";
import { createAgentIndicator } from "./agentIndicator.js";
import { createArmAlert } from "./armAlert.js";
import { maybeShowAppPromo } from "./appPromo.js";
import { installPressActivate } from "./pressActivate.js";
import { FOOTER_SECTIONS, GROUPS, SECTIONS, SIM_SECTIONS, railRows } from "./railLayout.js";

/** @typedef {import("./railLayout.js").Section} Section */

/** Path for a section key: teleop is the site root, the rest are /<key>. @param {string} key */
function pathForKey(key) {
  return key === "teleop" ? "/" : `/${key}`;
}

/** True when focus is in a text field, so global key shortcuts must stand down. */
export function isTypingContext() {
  const el = document.activeElement;
  return (
    el instanceof HTMLElement &&
    (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)
  );
}

/**
 * Build the persistent app chrome once — the icon rail, robot speech playback,
 * and the "agent running" indicator — and return a controller
 * the router uses to reflect the active section on each navigation. Called once
 * by the router, not per page (navigation is client-side now).
 * @param {(path: string) => void} navigate Router navigation, for key shortcuts.
 * @returns {{ setActive: (key: string) => void }}
 */
// iOS ignores user-scalable=no; Safari fires proprietary gesture events for
// pinch -- cancel them so the app UI never zooms (the 3D canvas keeps its own
// pinch-to-dolly via pointer events).
document.addEventListener("gesturestart", (e) => {
  if (!(e.target instanceof HTMLCanvasElement)) e.preventDefault();
});

/** @param {(path: string) => void} navigate */
export function initShell(navigate) {
  // Buttons fire on press-down instead of release, app-wide. Installed here
  // because the router builds the shell exactly once per page load. Idempotent.
  installPressActivate();

  const rail = document.createElement("aside");
  rail.className = "rail";

  const mark = document.createElement("a");
  mark.className = "rail-mark";
  mark.href = "/";
  mark.title = "Innate";
  // The Innate wordmark, scaled to the 38px rail width.
  mark.innerHTML =
    '<svg viewBox="0 0 48 12" width="34" height="8.5" fill="none" aria-hidden="true">' +
    '<path d="M2.92993 2.37757C2.29217 2.37757 1.75006 1.83546 1.75006 1.18175C1.75006 0.528041 2.29217 0.00188278 2.92993 0.00188278C3.5677 0.00188278 4.1098 0.528041 4.1098 1.18175C4.1098 1.83546 3.5677 2.37757 2.92993 2.37757ZM1.87762 10.2699V4.75325C1.87762 4.37059 1.71818 4.29087 1.3674 4.29087H0.93691V3.31827H4.12575V10.2699C4.12575 10.7164 4.26925 10.7642 4.68379 10.7642H5.05051V11.6571H0.93691V10.7642H1.30363C1.71818 10.7642 1.87762 10.7164 1.87762 10.2699ZM9.54926 10.7642V11.6571H5.48349V10.7642H5.85021C6.26476 10.7642 6.4242 10.7164 6.4242 10.2699V4.75325C6.4242 4.37059 6.26476 4.29087 5.91399 4.29087H5.48349V3.31827H8.67233V4.64164C9.19849 3.71688 10.1073 3.17478 11.351 3.17478C13.6469 3.17478 14.0615 4.35464 14.0615 5.75773V10.2699C14.0615 10.7164 14.205 10.7642 14.6195 10.7642H14.9703V11.6571H10.9045V10.7642H11.2393C11.6539 10.7642 11.7974 10.7164 11.7974 10.2699V5.64612C11.7974 4.68947 11.4466 4.11548 10.4421 4.11548C9.11877 4.11548 8.67233 5.08808 8.67233 6.60278V10.2699C8.67233 10.7164 8.81583 10.7642 9.23038 10.7642H9.54926ZM19.4832 10.7642V11.6571H15.4175V10.7642H15.7842C16.1987 10.7642 16.3582 10.7164 16.3582 10.2699V4.75325C16.3582 4.37059 16.1987 4.29087 15.848 4.29087H15.4175V3.31827H18.6063V4.64164C19.1325 3.71688 20.0413 3.17478 21.2849 3.17478C23.5809 3.17478 23.9954 4.35464 23.9954 5.75773V10.2699C23.9954 10.7164 24.1389 10.7642 24.5535 10.7642H24.9043V11.6571H20.8385V10.7642H21.1733C21.5879 10.7642 21.7314 10.7164 21.7314 10.2699V5.64612C21.7314 4.68947 21.3806 4.11548 20.3761 4.11548C19.0527 4.11548 18.6063 5.08808 18.6063 6.60278V10.2699C18.6063 10.7164 18.7498 10.7642 19.1644 10.7642H19.4832ZM25.9414 5.58235C25.9414 4.45031 26.5951 3.14289 29.4491 3.14289C32.4147 3.14289 32.9249 4.35464 32.9249 5.98095V10.2699C32.9249 10.7164 33.0684 10.7642 33.4989 10.7642H33.8497V11.6571H30.7246V10.4932H30.6609C29.9912 11.386 29.2737 11.8325 28.0141 11.8325C26.4038 11.8325 25.5906 10.9555 25.5906 9.45678C25.5906 7.97397 26.4197 7.03327 29.2418 7.03327H30.6609V5.07213C30.6609 4.37059 30.4376 3.62121 29.3694 3.62121C28.4924 3.62121 28.1098 4.00387 28.1098 4.8808V6.04473H25.9414V5.58235ZM29.0983 10.8758C30.1666 10.8758 30.6609 10.0148 30.6609 8.77118V7.54348H29.5767C28.2055 7.54348 27.9025 8.14936 27.9025 9.04224V9.47273C27.9025 10.4134 28.2373 10.8758 29.0983 10.8758ZM34.0679 4.29087V3.31827H35.2797V1.53252L37.5278 0.735314V3.31827H39.1222V4.29087H37.5278V10.1743C37.5278 10.6367 37.6554 10.7642 38.1656 10.7642H39.1222V11.6571H36.7625C35.5667 11.6571 35.2637 11.3063 35.2637 10.3178V4.29087H34.0679ZM47.6347 8.91468C47.6347 10.4932 46.8853 11.8644 43.9835 11.8644C40.396 11.8644 39.7423 9.85539 39.7423 7.6232V7.43187C39.7423 5.15185 40.5874 3.12694 43.824 3.12694C47.1882 3.12694 47.6666 5.24752 47.6666 6.98544V7.38404H42.1658V8.61174C42.1658 10.7164 42.676 11.4179 44.0154 11.4179C45.3547 11.4179 45.817 10.7004 45.833 8.7393H47.6347V8.91468ZM42.1658 6.14039V6.87383H45.2909V5.80557C45.2909 4.65758 45.0996 3.57338 43.7443 3.57338C42.4528 3.57338 42.1658 4.59381 42.1658 6.14039Z" fill="currentColor"></path></svg>';
  rail.appendChild(mark);

  const nav = document.createElement("nav");
  nav.className = "rail-nav";
  nav.setAttribute("aria-label", "Sections");
  // Settings (and any future utility page) pins to the rail's very bottom.
  const footNav = document.createElement("nav");
  footNav.className = "rail-nav rail-foot";
  footNav.setAttribute("aria-label", "Utility");
  let activeKey = "";

  /**
   * (Re)build the rail from railRows — links in group order, a divider at each
   * labeled-group boundary, footer sections pinned at the bottom. `visible`
   * filters sections (the sim rebuild).
   * @param {Set<string> | null} visible
   */
  function renderNav(visible) {
    nav.innerHTML = "";
    for (const row of railRows(GROUPS, visible)) {
      nav.appendChild(row.kind === "divider" ? buildDivider(row.label) : buildLink(row.section));
    }
    footNav.innerHTML = "";
    for (const section of FOOTER_SECTIONS) {
      if (!visible || visible.has(section.key)) footNav.appendChild(buildLink(section));
    }
    applyActive();
  }

  /** @param {Section} section */
  function buildLink(section) {
    // Only single digits exist as key shortcuts, so sections past the ninth
    // advertise none — a keycap that can't be pressed is a small lie.
    const index = SECTIONS.indexOf(section);
    const shortcut = index < 9 ? index + 1 : null;
    const a = document.createElement("a");
    a.className = "rail-link";
    a.dataset.section = section.key;
    a.href = pathForKey(section.key);
    a.title = shortcut ? `${section.label} (${shortcut})` : section.label;
    a.setAttribute("aria-label", section.label);
    if (shortcut) a.setAttribute("aria-keyshortcuts", String(shortcut));
    a.innerHTML =
      `<span class="rail-ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${section.icon}</svg></span>` +
      `<span class="rail-label">${section.label}</span>` +
      (shortcut ? `<span class="rail-key" aria-hidden="true">${shortcut}</span>` : "");
    return a;
  }

  /** @param {string | null} label */
  function buildDivider(label) {
    const div = document.createElement("div");
    div.className = "rail-div";
    if (!label) return div;
    div.classList.add("has-label");
    const span = document.createElement("span");
    span.className = "rail-div-label";
    span.textContent = label;
    div.appendChild(span);
    return div;
  }

  function applyActive() {
    for (const link of rail.querySelectorAll(".rail-link")) {
      const el = /** @type {HTMLElement} */ (link);
      const active = el.dataset.section === activeKey;
      el.classList.toggle("active", active);
      if (active) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    }
  }

  renderNav(null);
  rail.append(nav, footNav);

  // Number keys 1..N jump between sections (the number shows in each tooltip).
  // Guarded so it never fires while typing in a field or as part of a
  // browser/OS combo like Cmd+1 (tab switch). A removed link (sim-mode filter)
  // simply has no match, so its number is inert.
  window.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.repeat || isTypingContext()) return;
    const section = SECTIONS[Number(e.key) - 1];
    if (!section) return;
    const link = rail.querySelector(`.rail-link[data-section="${section.key}"]`);
    if (link) navigate(pathForKey(section.key));
  });

  document.body.prepend(rail);

  // Sim deployments hide robot-data workflows (SIM_SECTIONS) — rebuild the
  // rail without them once the (env-driven) config says we're in sim mode.
  void getConfig().then((config) => {
    // {} on any failure → assume real robot, keep every section.
    if (config?.simControls) renderNav(SIM_SECTIONS);
  });

  // Play robot speech (/tts/audio) regardless of which page is open; idempotent.
  initTtsAudio();

  // A running agent shows a top-center "running" pill, linking back to the Agent
  // page to take control. It's persistent (built once); setActive hides it while
  // the Agent page — which has its own Start/Stop — is open.
  const agentIndicator = createAgentIndicator(createAgentState(ros), "/agent");

  // A servo latched into (overcurrent) protection shows a discrete amber card
  // with the reboot remedy, on every page. Real robots only — the sim's arm
  // services are no-ops and /mars/arm/status never publishes.
  void getConfig().then((config) => {
    if (!config?.simControls) createArmAlert(ros);
  });

  // On a phone/tablet, nudge toward the native app (shown once, then remembered).
  maybeShowAppPromo("/");

  /**
   * Reflect the active section: highlight its rail link, hide the agent pill on
   * the Agent route, and title the tab.
   * @param {string} key
   */
  function setActive(key) {
    activeKey = key;
    applyActive();
    agentIndicator.el.style.display = key === "agent" ? "none" : "";
    const section = SECTIONS.find((s) => s.key === key);
    document.title = section ? `Innate · ${section.label}` : "Innate";
  }

  return { setActive };
}

