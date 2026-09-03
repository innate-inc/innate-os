// @ts-check
// Agent page entry — the autonomous-control room. Same full-bleed live feed as
// teleop (WebRTC video + camera/map PiP + telemetry), but the right edge hosts
// one liquid-glass Agent panel: directive selection, a Start/Stop toggle, the
// agent's live thinking traces + chat, and a message composer. Too narrow for
// that dock, the same panel docks to the bottom as a sheet (agentSheet.js).
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
import { createTrajectoryOverlay } from "../teleop/trajectoryOverlay.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createCameraSwitch } from "../teleop/cameraSwitch.js";
import { sharedAgentState } from "../teleop/agentState.js";
import { createAgentPanel } from "./agentPanel.js";
import { createChallengePanel } from "./challengePanel.js";
import { createAgentMicControl } from "./agentMicControl.js";
import { createAgentOnboarding } from "./agentOnboarding.js";

// Runtime feature flags (config.json, served static), same as teleop. simControls
// marks a sim deployment — used here to drop the (absent) battery readout. Fetched
// once on first import (the router's dynamic import awaits it) so the view reads
// it synchronously.
/** @type {any} */
const config = await getConfig();

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, releaseSession, createStage } = await robotSessionFactory();
// Two thresholds, both mirrored in app.css: the dock floats over the feed
// rather than taking a column, so it survives far below what the monitor needs.
const COMPACT_LAYOUT_QUERY = "(max-width: 820px)";
const BRAIN_MONITOR_QUERY = "(max-width: 1280px)";

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

  const feedFrame = document.createElement("div");
  feedFrame.className = "agent-feed-frame";
  root.append(feedFrame);
  // realVideo is the WebRTC stage on physical robots, null when the sim's
  // Three.js stage takes over — parts that need the head camera key off it.
  const realVideo = createStage ? null : createVideoStage(feedFrame, session);
  const videoStage =
    realVideo ??
    /** @type {NonNullable<typeof createStage>} */ (createStage)(feedFrame, session);
  const sceneSetup = feedFrame.querySelector(".sim-debug-stack");
  if (sceneSetup) root.append(sceneSetup);

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
  const onboarding = createAgentOnboarding(root);
  const panel = createAgentPanel(root, ros, agentState, {
    enableMic: Boolean(config.simControls),
    onMicState: (state) => {
      micControl?.setCaptureState(state);
      micControl?.setAudioFeedback({
        level: state.on ? state.level : 0,
        waveform: state.waveform,
      });
    },
    onUserMessage: onboarding.onUserMessage,
    onRobotMessage: onboarding.onRobotMessage,
    onAgentName: onboarding.onAgentChange,
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

  const compactLayout = window.matchMedia(COMPACT_LAYOUT_QUERY);
  const monitorTooNarrow = window.matchMedia(BRAIN_MONITOR_QUERY);

  // The dock floats over the feed, so the canvas's centre is behind it. Video
  // stages ignore this: a real camera's framing is the robot's to decide.
  const dockPanel = /** @type {HTMLElement | null} */ (root.querySelector(".agent-panel"));
  const setSafeInsets = /** @type {{ setSafeInsets?: (i: { right?: number }) => void }} */ (
    videoStage
  ).setSafeInsets;
  const reportSafeArea = () => {
    const feed = feedFrame.querySelector(".video-stage");
    if (!setSafeInsets || !dockPanel || !(feed instanceof HTMLElement)) return;
    // Against the canvas, not the cockpit: past 1400px the feed sits beside
    // the dock rather than under it, so nothing is covered.
    const canvas = feed.getBoundingClientRect();
    const dock = dockPanel.getBoundingClientRect();
    const covered = compactLayout.matches ? 0 : Math.max(0, canvas.right - dock.left);
    setSafeInsets({ right: Math.min(covered, canvas.width) });
  };

  const applyLayout = () => {
    root.classList.toggle("agent-compact", compactLayout.matches);
    // Its toggle is hidden at this width, so an open monitor would strand the
    // page on a stage it cannot leave.
    if (monitorTooNarrow.matches) setView("live");
    panel.setCompact(compactLayout.matches);
    reportSafeArea();
  };
  compactLayout.addEventListener("change", applyLayout);
  monitorTooNarrow.addEventListener("change", applyLayout);
  const safeAreaObserver = new ResizeObserver(reportSafeArea);
  safeAreaObserver.observe(root);
  safeAreaObserver.observe(feedFrame);
  applyLayout();

  const parts = [
    videoStage,
    {
      destroy: () => {
        compactLayout.removeEventListener("change", applyLayout);
        monitorTooNarrow.removeEventListener("change", applyLayout);
        safeAreaObserver.disconnect();
      },
    },
    ...(challengePanel ? [challengePanel] : []),
    ...(telemetry ? [telemetry] : []),
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    cameraSwitch,
    ...(micControl ? [micControl] : []),
    onboarding,
    panel,
    {
      destroy: () => {
        root.removeEventListener("pointerdown", onScenePointerDown);
      },
    },
    { destroy: () => stageViewToggle.remove() },
    {
      destroy: () => {
        unmounted = true; // a monitor import still in flight must not build into the dead layer
        monitor?.destroy();
      },
    },
  ];
  // Watching the agent drive is where the projected route earns its keep. The
  // agent panel owns the right edge here, so the toggle joins the top-left
  // stack instead of a rail.
  const ribbonStage = realVideo?.el ?? feedFrame.querySelector(".video-stage");
  if (ribbonStage instanceof HTMLElement) {
    parts.push(createTrajectoryOverlay(ribbonStage, realVideo?.videoEl ?? null, cornerStack, ros, session));
  }

  session.start();

  const entryPath = location.pathname.replace(/\/+$/, "");
  if (entryPath === "/brain" && !monitorTooNarrow.matches) setView("brain");
  if (entryPath === "/brain" || entryPath === "/agent") {
    history.replaceState({}, "", "/" + location.search + location.hash);
  }

  return {
    destroy() {
      for (const part of parts) part.destroy();
      releaseSession(session);
      root.innerHTML = "";
    },
  };
}

