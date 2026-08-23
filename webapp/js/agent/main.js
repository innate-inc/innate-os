// @ts-check
// Agent page entry — the autonomous-control room. Same full-bleed live feed as
// teleop (WebRTC video + camera/map PiP + telemetry), but the right edge hosts
// one liquid-glass Agent panel: directive selection, a Start/Stop toggle, the
// agent's live thinking traces + active skill + chat, and a message composer.
//
// The page has two stages behind that one panel: the live camera view, and the
// Brain monitor (the agent loop instrumented turn by turn) which flips in over
// it via a stage-level inspector button — controls and chat stay docked in both.
// The monitor is built on first open and kept until the page unmounts, so its
// turn history survives flipping back and forth. /brain deep-links here with
// the monitor open.
//
// Connect/disconnect lifecycle and optimistic mount mirror teleop (see
// pageMount.js): the view builds immediately and panels fill in once the socket
// is up. The centered hold-to-talk control is the sim's voice input; robot
// speech comes back through the shell's ttsAudio.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { getConfig } from "../config.js";
import { robotSessionFactory } from "../robotSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createCameraSwitch } from "../teleop/cameraSwitch.js";
import { sharedAgentState } from "../teleop/agentState.js";
import { createAgentPanel } from "./agentPanel.js";
import { createChallengePanel } from "./challengePanel.js";
import { createAgentMicControl } from "./agentMicControl.js";
import { createAudioOverlay } from "./audioOverlay.js";
import { createGazeOverlay } from "./gazeOverlay.js";

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
const MIN_AGENT_VIEW_WIDTH = 1281;

/** @param {HTMLElement} stage */
export function mount(stage) {
  const className = config.simControls
    ? "cockpit agent-cockpit agent-sim"
    : "cockpit agent-cockpit";
  return mountPage(stage, className, buildAgentView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildAgentView(root) {
  const session = createSession();
  const widthGuard = createWidthGuard(
    root,
    config.simControls ? "Simulator" : "Camera view",
  );

  const feedFrame = document.createElement("div");
  feedFrame.className = "agent-feed-frame";
  root.append(feedFrame);
  const videoStage = createStage
    ? createStage(feedFrame, session)
    : createVideoStage(feedFrame, session);
  const sceneSetup = feedFrame.querySelector(".sim-debug-stack");
  if (sceneSetup) root.append(sceneSetup);
  const gazeStage = /** @type {HTMLElement | null} */ (feedFrame.querySelector(".video-stage"));
  const debugStack = gazeStage ? document.createElement("div") : null;
  if (debugStack) {
    debugStack.className = "agent-debug-stack";
    gazeStage?.append(debugStack);
  }
  const audioOverlay = debugStack ? createAudioOverlay(debugStack, ros) : null;
  const gazeOverlay = gazeStage && debugStack ? createGazeOverlay(gazeStage, ros, session, debugStack) : null;

  const cornerStack = document.createElement("div");
  cornerStack.className = "overlay-stack-top-left";
  root.append(cornerStack);
  const agentState = sharedAgentState();

  const cameraSwitch = createCameraSwitch(root, session, ros, {
    storeKey: "innate.cameras.agent",
    stripParent: cornerStack,
    // The Agent page is for watching the agent work, so open on the sim's orbit
    // "top view" every visit rather than whatever was left selected last time.
    // Real robots have no orbit camera, so their saved choice is untouched.
    primaryOnMount: config.simControls ? "orbit" : undefined,
  });
  const telemetryOverlay = config.simControls ? null : document.createElement("div");
  if (telemetryOverlay) {
    telemetryOverlay.className = "overlay telemetry-overlay agent-telemetry-overlay";
    root.append(telemetryOverlay);
  }

  // The Brain monitor's layer sits between the camera overlays and the panel
  // (DOM order + z-index): opening it covers the stage but never the controls.
  const brainLayer = document.createElement("div");
  brainLayer.className = "agent-brain brain-page";
  brainLayer.hidden = true;
  root.append(brainLayer);

  const stageViewToggle = document.createElement("button");
  stageViewToggle.type = "button";
  stageViewToggle.className = "agent-stage-view-toggle";
  stageViewToggle.innerHTML =
    '<span class="agent-stage-view-icon" aria-hidden="true"></span><span class="agent-stage-view-label">Inspect\nBrain</span>';
  const stageViewLabel = /** @type {HTMLElement} */ (
    stageViewToggle.querySelector(".agent-stage-view-label")
  );
  stageViewToggle.addEventListener("click", () => setView(view === "live" ? "brain" : "live"));
  root.append(stageViewToggle);

  const audioDebugToggle = document.createElement("button");
  audioDebugToggle.type = "button";
  audioDebugToggle.className = "agent-audio-debug-toggle active";
  audioDebugToggle.hidden = audioOverlay === null;
  audioDebugToggle.innerHTML = '<span class="agent-audio-debug-icon" aria-hidden="true"></span>';
  let audioDebugVisible = true;
  function renderAudioDebug() {
    audioOverlay?.setVisible(audioDebugVisible);
    audioDebugToggle.classList.toggle("active", audioDebugVisible);
    audioDebugToggle.setAttribute("aria-pressed", String(audioDebugVisible));
    audioDebugToggle.setAttribute(
      "aria-label",
      audioDebugVisible ? "Hide audio debug overlay" : "Show audio debug overlay",
    );
    audioDebugToggle.title = audioDebugToggle.getAttribute("aria-label") ?? "";
  }
  audioDebugToggle.addEventListener("click", () => {
    audioDebugVisible = !audioDebugVisible;
    renderAudioDebug();
  });
  renderAudioDebug();
  root.append(audioDebugToggle);

  const followDebugToggle = document.createElement("button");
  followDebugToggle.type = "button";
  followDebugToggle.className = "agent-follow-debug-toggle active";
  followDebugToggle.hidden = gazeOverlay === null;
  followDebugToggle.innerHTML = '<span class="agent-follow-debug-icon" aria-hidden="true"></span>';
  let followDebugVisible = true;
  function renderFollowDebug() {
    gazeOverlay?.setFollowDebugVisible(followDebugVisible);
    followDebugToggle.classList.toggle("active", followDebugVisible);
    followDebugToggle.setAttribute("aria-pressed", String(followDebugVisible));
    followDebugToggle.setAttribute(
      "aria-label",
      followDebugVisible ? "Hide person follow debug panel" : "Show person follow debug panel",
    );
    followDebugToggle.title = followDebugToggle.getAttribute("aria-label") ?? "";
  }
  followDebugToggle.addEventListener("click", () => {
    followDebugVisible = !followDebugVisible;
    renderFollowDebug();
  });
  renderFollowDebug();
  root.append(followDebugToggle);

  /** @param {"live" | "brain"} next */
  function renderStageView(next) {
    const brain = next === "brain";
    stageViewToggle.classList.toggle("active", brain);
    stageViewToggle.setAttribute("aria-pressed", String(brain));
    stageViewToggle.setAttribute("aria-label", brain ? "Back to live camera" : "Inspect brain activity");
    stageViewToggle.title = brain
      ? "Return to the robot's live camera"
      : "Inspect model frames, tools, latency, and turn history";
    stageViewLabel.textContent = brain ? "Back to\nLive" : "Inspect\nBrain";
  }
  renderStageView("live");

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
    micControl?.setEnabled(next === "live");
    renderStageView(next);
  }

  /** @type {ReturnType<typeof createAgentMicControl> | null} */
  let micControl = null;
  const panel = createAgentPanel(root, ros, agentState, {
    enableMic: Boolean(config.simControls),
    onMicState: (state) => {
      micControl?.setCaptureState(state);
      micControl?.setAudioFeedback({
        level: state.on ? state.level : 0,
        waveform: state.waveform,
      });
    },
  });
  const simSession = /** @type {any} */ (session);
  const challengePanel =
    typeof simSession.onChallenge === "function" ? createChallengePanel(root, simSession) : null;
  const isSceneSurface = (/** @type {EventTarget | null} */ target) =>
    target instanceof Element &&
    (target.matches(".video-stage > canvas, .video-stage > video") || target.classList.contains("video-stage"));
  const onScenePointerDown = (/** @type {PointerEvent} */ event) => {
    if (!event.isPrimary || event.button !== 0 || !isSceneSurface(event.target)) return;
    challengePanel?.dismiss();
  };
  root.addEventListener("pointerdown", onScenePointerDown);
  const telemetry = telemetryOverlay ? createTelemetry(telemetryOverlay, ros) : null;
  if (config.simControls) {
    micControl = createAgentMicControl(panel.micMount, {
      startListening: panel.startMic,
      stopListening: panel.stopMic,
    });
  }

  const parts = [
    ...(gazeOverlay ? [gazeOverlay] : []),
    ...(audioOverlay ? [audioOverlay] : []),
    videoStage,
    widthGuard,
    ...(challengePanel ? [challengePanel] : []),
    ...(telemetry ? [telemetry] : []),
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    cameraSwitch,
    ...(micControl ? [micControl] : []),
    panel,
    {
      destroy: () => {
        root.removeEventListener("pointerdown", onScenePointerDown);
      },
    },
    { destroy: () => stageViewToggle.remove() },
    { destroy: () => audioDebugToggle.remove() },
    { destroy: () => followDebugToggle.remove() },
    {
      destroy: () => {
        unmounted = true; // a monitor import still in flight must not build into the dead layer
        monitor?.destroy();
      },
    },
  ];

  session.start();

  const entryPath = location.pathname.replace(/\/+$/, "");
  if (entryPath === "/brain") setView("brain");
  if (entryPath === "/brain" || entryPath === "/agent") {
    history.replaceState({}, "", "/" + location.search + location.hash);
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
 * @param {HTMLElement} root
 * @param {string} viewName
 * @returns {{ destroy: () => void }}
 */
function createWidthGuard(root, viewName) {
  const guard = document.createElement("aside");
  guard.className = "agent-width-guard";
  guard.setAttribute("aria-labelledby", "agent-width-guard-title");
  guard.innerHTML = `
    <div class="agent-width-guard-card">
      <h2 id="agent-width-guard-title" class="agent-width-guard-title">${viewName} unavailable</h2>
      <p class="agent-width-guard-message">Widen your browser to continue.</p>
      <div class="agent-width-meter">
        <div class="agent-width-meter-labels">
          <span>Current <output class="agent-width-current"></output></span>
          <span>Minimum <output>${MIN_AGENT_VIEW_WIDTH} px</output></span>
        </div>
        <div
          class="agent-width-meter-track"
          role="progressbar"
          aria-label="Browser width"
          aria-valuemin="0"
          aria-valuemax="${MIN_AGENT_VIEW_WIDTH}"
        ><span></span></div>
      </div>
    </div>
  `;

  const current = /** @type {HTMLOutputElement} */ (
    guard.querySelector(".agent-width-current")
  );
  const meter = /** @type {HTMLElement} */ (
    guard.querySelector(".agent-width-meter-track")
  );
  const render = () => {
    const width = window.innerWidth;
    const progress = Math.min(width / MIN_AGENT_VIEW_WIDTH, 1);
    current.textContent = `${width} px`;
    meter.setAttribute(
      "aria-valuenow",
      String(Math.min(width, MIN_AGENT_VIEW_WIDTH)),
    );
    guard.style.setProperty("--agent-width-progress", String(progress));
  };

  window.addEventListener("resize", render);
  render();
  root.append(guard);

  return {
    destroy() {
      window.removeEventListener("resize", render);
      guard.remove();
    },
  };
}
