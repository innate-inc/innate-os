// @ts-check
// Agent page entry — the autonomous-control room. Same full-bleed live feed as
// teleop (WebRTC video + camera/map PiP + telemetry), but the right edge hosts
// one liquid-glass Agent panel: directive selection, a Start/Stop toggle, the
// agent's live thinking traces + active skill + chat, and a message composer.
//
// The page has two stages behind that one panel: the live camera view, and the
// Brain monitor (the agent loop instrumented turn by turn) which flips in over
// it via the panel's Live/Brain switch — controls and chat stay docked in both.
// The monitor is built on first open and kept until the page unmounts, so its
// turn history survives flipping back and forth. /brain deep-links here with
// the monitor open.
//
// Connect/disconnect lifecycle and optimistic mount mirror teleop (see
// pageMount.js): the view builds immediately and panels fill in once the socket
// is up. No mic toggle here — robot speech already plays in-browser via the
// shell's ttsAudio.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { getConfig } from "../config.js";
import { robotSessionFactory } from "../robotSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createCameraSwitch } from "../teleop/cameraSwitch.js";
import { createAgentState } from "../teleop/agentState.js";
import { createAgentPanel } from "./agentPanel.js";
import { createChallengePanel } from "./challengePanel.js";

// Runtime feature flags (config.json, served static), same as teleop. simControls
// marks a sim deployment — used here to drop the (absent) battery readout. Fetched
// once on first import (the router's dynamic import awaits it) so the view reads
// it synchronously.
/** @type {any} */
const config = await getConfig();

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, createStage } = await robotSessionFactory();

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "cockpit agent-cockpit", buildAgentView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildAgentView(root) {
  const session = createSession();

  const videoStage = createStage ? createStage(root, session) : createVideoStage(root, session);

  // Top-left column: the sim challenge panel (when the session supports it)
  // stacked above the telemetry card; on real robots it holds telemetry alone,
  // in the same corner position as before.
  const cornerStack = document.createElement("div");
  cornerStack.className = "overlay-stack-top-left";
  root.append(cornerStack);
  // Column order is DOM order: the challenge panel (sim only) first, then the
  // telemetry card under it. The stack does the positioning, so the card
  // carries no corner class of its own.
  const challengePanel = typeof session.onChallenge === "function" ? createChallengePanel(cornerStack, session) : null;
  const telemetryOverlay = document.createElement("div");
  telemetryOverlay.className = "overlay";
  cornerStack.append(telemetryOverlay);
  const agentState = createAgentState(ros);

  const cameraSwitch = createCameraSwitch(root, session, ros, { storeKey: "innate.cameras.agent" });

  // The Brain monitor's layer sits between the camera overlays and the panel
  // (DOM order + z-index): opening it covers the stage but never the controls.
  const brainLayer = document.createElement("div");
  brainLayer.className = "agent-brain brain-page";
  brainLayer.hidden = true;
  root.append(brainLayer);

  /** @type {{ destroy: () => void, setVisible: (visible: boolean) => void } | null} */
  let monitor = null;
  let monitorLoading = false;
  let unmounted = false;
  /** @type {"live" | "brain"} */
  let view = "live";
  /** @param {"live" | "brain"} next */
  function setView(next) {
    if (next === view) return;
    view = next;
    if (next === "brain" && !monitor && !monitorLoading) {
      // The monitor is its own sizeable module, fetched on first open so Agent
      // mounts that never look inside don't pay for it. Kept once built (its
      // turn history survives flips); hidden, it pauses its animation loop and
      // camera fallback via setVisible.
      monitorLoading = true;
      void import("../brain/main.js")
        .then((m) => {
          if (unmounted) return;
          monitor = m.createBrainMonitor(brainLayer, { onRequestClose: () => setView("live") });
          monitor.setVisible(view === "brain");
        })
        .catch(() => {
          monitorLoading = false; // a failed fetch can retry on the next flip
        });
    }
    monitor?.setVisible(next === "brain");
    brainLayer.hidden = next !== "brain";
    root.classList.toggle("brain-open", next === "brain");
    panel.setView(next);
  }

  const panel = createAgentPanel(root, ros, agentState, { onView: setView });

  const parts = [
    videoStage,
    ...(challengePanel ? [challengePanel] : []),
    createTelemetry(telemetryOverlay, ros, { showBattery: !config.simControls }),
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    cameraSwitch,
    panel,
    createActiveChip(root, agentState, () => setView("brain")),
    { destroy: () => agentState.destroy() },
    {
      destroy: () => {
        unmounted = true; // a monitor import still in flight must not build into the dead layer
        monitor?.destroy();
      },
    },
  ];

  session.start();

  // /brain is this page with the monitor open; land there, then settle the URL
  // on /agent so the address bar matches the one-page model.
  if (location.pathname.replace(/\/+$/, "") === "/brain") {
    setView("brain");
    history.replaceState({}, "", "/agent" + location.search + location.hash);
  }

  return {
    destroy() {
      for (const part of parts) part.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}

/**
 * Bottom-left "AGENT ACTIVE" chip — shown only while the brain is running.
 * Clicking it opens the Brain monitor: the moment the robot is acting on its
 * own is exactly when you want to see inside.
 * @param {HTMLElement} root
 * @param {ReturnType<typeof import("../teleop/agentState.js").createAgentState>} agentState
 * @param {() => void} onWatch
 * @returns {{ destroy: () => void }}
 */
function createActiveChip(root, agentState, onWatch) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "agent-active-chip";
  chip.hidden = true;
  chip.title = "Open the Brain monitor";

  const dot = document.createElement("span");
  dot.className = "agent-active-dot";
  const label = document.createElement("div");
  label.className = "agent-active-text";
  const title = document.createElement("span");
  title.className = "agent-active-title";
  title.textContent = "Agent active";
  const sub = document.createElement("span");
  sub.className = "agent-active-sub";
  sub.innerHTML = 'Watch its brain <span class="agent-active-arrow">→</span>';
  label.append(title, sub);
  chip.append(dot, label);
  root.append(chip);

  chip.addEventListener("click", onWatch);
  const unsub = agentState.subscribe((s) => {
    chip.hidden = !s.brainActive;
  });

  return {
    destroy() {
      unsub();
      chip.remove();
    },
  };
}
