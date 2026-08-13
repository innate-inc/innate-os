// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Shell — the 64px icon rail + connection badge rendered into every page,
// and the placeholder renderer for not-yet-built sections.

import { ros } from "./rosClient.js";
import { initTtsAudio } from "./ttsAudio.js";
import { getConfig } from "./config.js";
import { createAgentState } from "./teleop/agentState.js";
import { createAgentIndicator } from "./agentIndicator.js";
import { createArmAlert } from "./armAlert.js";
import { maybeShowAppPromo } from "./appPromo.js";
import { installPressActivate } from "./pressActivate.js";

/** @typedef {{ key: string, label: string, icon: string }} Section */

// In sim mode (config.simControls) only these sections make sense — the rest
// (Datasets/Collect/Training/Profiling/Calibration) are robot-data workflows
// with no sim backing — the sim has no real stereo camera or ChArUco board to
// calibrate against — so they're hidden from the rail. Arm SDK stays: the sim
// runs the same IK node and answers the goto services, so the page exercises
// the real Manipulation SDK against the simulated arm.
const SIM_SECTIONS = new Set(["teleop", "agent", "brain", "nav", "logging", "armsdk", "benchmark", "settings"]);
// The inverse set: sections whose backend exists only beside a sim, hidden
// from the rail on a real robot (router.js gates the routes to match).
const SIM_ONLY_SECTIONS = new Set(["benchmark"]);

/** @type {Section[]} */
const SECTIONS = [
  {
    key: "teleop",
    label: "Teleop",
    // The joystick motif: rim, cardinal ticks, knob.
    icon: '<circle cx="12" cy="12" r="8.5"/><line x1="12" y1="3.5" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="20.5"/><line x1="3.5" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="20.5" y2="12"/><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/>',
  },
  {
    key: "agent",
    label: "Agent",
    // Sparkle motif: a four-point star for the autonomous brain.
    icon: '<path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/>',
  },
  {
    key: "brain",
    label: "Brain",
    // Monitor motif: an EKG pulse line, for the live agent-loop monitor.
    icon: '<polyline points="3.5,12 7.5,12 10,6.5 13,17.5 15.5,12 20.5,12"/>',
  },
  {
    key: "nav",
    label: "Nav",
    // Radar motif: sweep arcs and a contact dot, for the live sensor view.
    icon: '<path d="M12 12L18.4 5.6"/><path d="M15.2 8.8a4.5 4.5 0 1 0 1.3 3.2"/><path d="M18.4 5.6A9 9 0 1 0 21 12"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
  },
  {
    key: "logging",
    label: "Logging",
    icon: '<polyline points="4.5,7 10,12 4.5,17"/><line x1="12.5" y1="17" x2="19.5" y2="17"/>',
  },
  {
    key: "datasets",
    label: "Datasets",
    icon: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3V6"/><path d="M4.5 12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3"/>',
  },
  {
    key: "collect",
    label: "Collect",
    icon: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>',
  },
  {
    key: "training",
    label: "Training",
    icon: '<polyline points="4,17.5 9.5,11.5 13.5,15 20,7"/><polyline points="15.5,7 20,7 20,11.5"/>',
  },
  {
    key: "profiling",
    label: "Profiling",
    // Stopwatch motif: dial, crown, and a sweeping hand.
    icon: '<circle cx="12" cy="13" r="7.5"/><line x1="12" y1="13" x2="15" y2="10"/><line x1="12" y1="2.5" x2="12" y2="5" /><line x1="9.5" y1="2.5" x2="14.5" y2="2.5"/>',
  },
  {
    key: "calibration",
    label: "Calibration",
    // Camera motif: body with a viewfinder bump, and a lens. The body spans
    // x 4–19, y 6–19, so its centre lands on the half unit.
    icon: '<path d="M4 8a2 2 0 0 1 2-2h2l1.2-1.8a1 1 0 0 1 .8-.4h3a1 1 0 0 1 .8.4L15 6h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8z"/><circle cx="11.5" cy="12.5" r="3.4"/>',
  },
  {
    key: "armsdk",
    label: "Arm SDK",
    // Articulated-arm motif: base, two links with a joint, and a claw.
    icon: '<circle cx="6" cy="19" r="2"/><path d="M7.5 17.5L10.5 10"/><circle cx="11" cy="8.8" r="1.4"/><path d="M12.3 8L17 5.5"/><path d="M17 5.5l2.5 1M17 5.5l.5 2.7"/>',
  },
  {
    key: "benchmark",
    label: "Benchmark",
    // Podium motif: baseline with three rising bars, for the eval scoreboard.
    icon: '<line x1="4" y1="20" x2="20" y2="20"/><line x1="7" y1="20" x2="7" y2="13.5"/><line x1="12" y1="20" x2="12" y2="8.5"/><line x1="17" y1="20" x2="17" y2="4.5"/>',
  },
  {
    key: "settings",
    label: "Settings",
    // Sliders motif: two tracks, each with a knob.
    icon: '<line x1="4" y1="8.5" x2="20" y2="8.5"/><circle cx="10" cy="8.5" r="2.3" fill="currentColor" stroke="none"/><line x1="4" y1="15.5" x2="20" y2="15.5"/><circle cx="15" cy="15.5" r="2.3" fill="currentColor" stroke="none"/>',
  },
];

/** Path for a section key: teleop is the site root, the rest are /<key>. @param {string} key */
function pathForKey(key) {
  return key === "teleop" ? "/" : `/${key}`;
}

/** True when focus is in a text field, so global key shortcuts must stand down. */
function isTypingContext() {
  const el = document.activeElement;
  return (
    el instanceof HTMLElement &&
    (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)
  );
}

/**
 * Build the persistent app chrome once — the icon rail + connection badge, robot
 * speech playback, and the "agent running" indicator — and return a controller
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
  SECTIONS.forEach((section, i) => {
    const shortcut = i + 1; // 1..N, the number-key shortcut for this section
    const a = document.createElement("a");
    a.className = "rail-link";
    a.dataset.section = section.key;
    a.href = pathForKey(section.key);
    a.title = `${section.label} (${shortcut})`;
    a.setAttribute("aria-label", section.label);
    a.setAttribute("aria-keyshortcuts", String(shortcut));
    a.innerHTML =
      `<span class="rail-ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${section.icon}</svg></span>` +
      `<span class="rail-label">${section.label}</span>` +
      `<span class="rail-key" aria-hidden="true">${shortcut}</span>`;
    nav.appendChild(a);
  });
  rail.appendChild(nav);

  // Number keys 1..N jump between sections (the number shows in each tooltip).
  // Guarded so it never fires while typing in a field or as part of a
  // browser/OS combo like Cmd+1 (tab switch). A removed link (sim-mode filter)
  // simply has no match, so its number is inert.
  window.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.repeat || isTypingContext()) return;
    const section = SECTIONS[Number(e.key) - 1];
    if (!section) return;
    // A removed link (sim-mode filter) has no match, so its number stays inert.
    const link = nav.querySelector(`.rail-link[data-section="${section.key}"]`);
    if (link) navigate(pathForKey(section.key));
  });

  rail.appendChild(createBadge());
  document.body.prepend(rail);

  // Sim deployments only expose Teleop/Agent/Logging/Settings — drop the rest
  // from the rail once the (env-driven) config says we're in sim mode.
  void applySimSectionFilter(nav);

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
    for (const link of nav.querySelectorAll(".rail-link")) {
      const el = /** @type {HTMLElement} */ (link);
      const active = el.dataset.section === key;
      el.classList.toggle("active", active);
      if (active) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    }
    agentIndicator.el.style.display = key === "agent" ? "none" : "";
    const section = SECTIONS.find((s) => s.key === key);
    document.title = section ? `Innate · ${section.label}` : "Innate";
  }

  return { setActive };
}

/**
 * Hide robot-only sections from the rail in sim mode, and sim-only sections
 * on a real robot ({} on config failure → assume real robot).
 * @param {HTMLElement} nav
 */
async function applySimSectionFilter(nav) {
  /** @type {any} */
  let config;
  config = await getConfig();
  const sim = !!config?.simControls;
  for (const link of nav.querySelectorAll(".rail-link")) {
    const key = /** @type {HTMLElement} */ (link).dataset.section ?? "";
    if (sim ? !SIM_SECTIONS.has(key) : SIM_ONLY_SECTIONS.has(key)) link.remove();
  }
}

/**
 * Connection badge pinned at the rail bottom: pulsing state dot + mono IP.
 * Click disconnects when connected.
 * @returns {HTMLElement}
 */
function createBadge() {
  const badge = document.createElement("button");
  badge.className = "badge";
  badge.type = "button";

  const dot = document.createElement("span");
  dot.className = "badge-dot";
  const label = document.createElement("span");
  label.className = "badge-label";
  badge.append(dot, label);

  ros.onStateChange((state, ip) => {
    badge.dataset.state = state;
    /** @type {Record<ConnState, string>} */
    const text = {
      disconnected: "offline",
      connecting: "linking",
      connected: ip ?? "linked",
      reconnecting: "relink",
    };
    label.textContent = text[state];
    badge.title = state === "connected" ? `Connected to ${ip} — click to disconnect` : state;
    badge.disabled = state !== "connected";
  });

  badge.addEventListener("click", () => {
    if (ros.state === "connected") ros.disconnect();
  });
  return badge;
}
