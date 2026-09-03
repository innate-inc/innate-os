// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Teleoperation page entry — wires the shared singletons to the page modules
// and owns the connected/disconnected lifecycle.
//
// Disconnected: one quiet connect card. Connected: the video is the room —
// full-bleed stage with glass overlays (telemetry top-left, head tilt and
// mic toggle on the right edge, WASD chips bottom-left, joystick + TTS
// bottom-center). On reconnecting we keep the cockpit (frozen video, badge
// pulses); only an intentional disconnect or failed connect shows the card.

import { ros } from "../rosClient.js";
import { drive } from "../driveController.js";
import { getConfig } from "../config.js";
import { WebRtcSession } from "../webrtcSession.js";
import { robotSessionFactory } from "../robotSession.js";
import { mountPage } from "../pageMount.js";
import { createVideoStage, createAudioToggle } from "./videoStage.js";
import { createTrajectoryOverlay } from "./trajectoryOverlay.js";
import { createJoystick } from "./joystick.js";
import { createKeyboardDrive, createWasdChips } from "./keyboardDrive.js";
import { createHeadTilt } from "./headTilt.js";
import { createSpeedModes } from "./speedModes.js";
import { createTtsBar } from "./ttsBar.js";
import { createTelemetry } from "./telemetry.js";
import { createArmPanel } from "./armPanel.js";
import { createProfilingPanel } from "./profilingPanel.js";
import { createSkillsMenu } from "./skillsMenu.js";
import { createTeleopOnboarding } from "./teleopOnboarding.js";
import { createCameraSwitch } from "./cameraSwitch.js";
import { dismissAllConfirms } from "../nav/confirm.js";
import { setTtsAudioEnabled } from "../ttsAudio.js";

// Runtime feature flags (config.json, served static). Sim-only debug controls are
// off unless a deployment opts in. Fetched once when this module first loads (the
// router's dynamic import awaits this), so buildCockpit can read it synchronously.
/** @type {any} */
const config = await getConfig();

// Console debugging hook (also handy alongside the Logging page).
/** @type {{ ros: typeof ros, drive: typeof drive, session: WebRtcSession | null }} */
const dbg = { ros, drive, session: null };
/** @type {any} */ (window).innate = dbg;

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, releaseSession, createStage } = await robotSessionFactory();

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "cockpit", buildCockpit);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildCockpit(root) {
  // Hardware shows the telemetry card in the map's top-left corner; expose the
  // mode to CSS so map controls can clear it without moving the sim layout.
  root.classList.toggle("teleop-hardware", !config.simControls);

  const session = createSession();
  dbg.session = session;

  // realVideo is the WebRTC stage on physical robots, null when the sim's
  // Three.js stage takes over — parts that need the head camera key off it.
  const realVideo = createStage ? null : createVideoStage(root, session);
  const videoStage = realVideo ?? /** @type {NonNullable<typeof createStage>} */ (createStage)(root, session);

  const telemetryOverlay = config.simControls ? null : overlay("overlay-top-left telemetry-overlay");
  const rightRail = overlay("overlay-right");
  const chipsOverlay = overlay("overlay-bottom-left");
  const stickOverlay = overlay("overlay-joystick");
  const ttsOverlay = overlay("overlay-tts");
  const armOverlay = overlay("overlay-arm");
  root.append(...(telemetryOverlay ? [telemetryOverlay] : []), rightRail, chipsOverlay, stickOverlay, ttsOverlay, armOverlay);

  /** @param {string} className */
  function overlay(className) {
    const el = document.createElement("div");
    el.className = `overlay ${className}`;
    return el;
  }

  const keyboard = createKeyboardDrive(drive);
  const telemetry = telemetryOverlay ? createTelemetry(telemetryOverlay, ros) : null;
  const simProps = /** @type {{
   *   onProps?: (cb: (props: {name: string}[]) => void) => () => void,
   *   placePropAtRobot?: (name: string) => void,
   * }} */ (/** @type {unknown} */ (session));
  /** @type {(() => void) | null} */
  let stopCylinderPrep = null;
  const prepareCylinder = () => {
    stopCylinderPrep?.();
    stopCylinderPrep = null;
    if (!config.simControls || !simProps.onProps || !simProps.placePropAtRobot) return;
    let placed = false;
    let subscribed = false;
    let unsubscribe = () => {};
    unsubscribe = simProps.onProps((props) => {
      if (placed || !props.some((prop) => prop.name === "can")) return;
      placed = true;
      simProps.placePropAtRobot?.("can");
      if (subscribed) {
        unsubscribe();
        stopCylinderPrep = null;
      }
    });
    // onProps fires synchronously when the roster has already arrived.
    subscribed = true;
    if (placed) {
      unsubscribe();
      stopCylinderPrep = null;
    }
    else stopCylinderPrep = unsubscribe;
  };
  const onboarding = createTeleopOnboarding(root, { prepareCylinder });
  const parts = [videoStage, ...(telemetry ? [telemetry] : [])];
  // Keep the listen control in the same place on sim and hardware. The sim
  // starts listening by default; hardware remains opt-in for privacy.
  if (config.simControls) {
    setTtsAudioEnabled(true);
    session.setAudio(true);
  }
  parts.push(createAudioToggle(rightRail, session, videoStage.audioEl, {
    onChange: config.simControls ? setTtsAudioEnabled : undefined,
  }));
  parts.push(
    createSpeedModes(rightRail, ros),
    createHeadTilt(rightRail, ros),
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros, {
      onSpeak: onboarding.onSpeak,
      onAvailabilityChange: onboarding.onSpeechAvailabilityChange,
    }),
    // Collapsible skill launcher pinned next to the speak bar.
    createSkillsMenu(ttsOverlay, ros, {
      onSkillCompleted: onboarding.onSkillCompleted,
      onOpenChange: onboarding.onSkillsMenuOpenChange,
    }),
    createArmPanel(armOverlay, ros, { hideServices: !!config.simControls }),
    ...(config.simControls ? [] : [createProfilingPanel(root, session)]),
    // Teleop is the head-camera control room. Do not let a saved Arm, Top View,
    // or Map choice make onboarding begin from the wrong perspective.
    createCameraSwitch(root, session, ros, { primaryOnMount: "main" }),
    keyboard,
    onboarding,
  );
  // The sim's main view renders the same head camera (camera_optical_frame at
  // the driver's FOV), so the ribbon projects there too — the overlay swaps the
  // real lens's calibration for the render's ideal pinhole. Either stage keeps
  // the .video-stage class, and both hide the ribbon off the main camera.
  const ribbonStage = realVideo?.el ?? root.querySelector(".video-stage");
  if (ribbonStage instanceof HTMLElement) {
    parts.push(createTrajectoryOverlay(ribbonStage, realVideo?.videoEl ?? null, rightRail, ros, session));
  }

  session.start();

  return {
    destroy() {
      drive.haltAll();
      // Confirm dialogs (speed picker) live on document.body — sweep them so
      // navigating away doesn't leave one floating over the next page.
      dismissAllConfirms();
      stopCylinderPrep?.();
      for (const part of parts) part.destroy();
      if (config.simControls) setTtsAudioEnabled(true);
      releaseSession(session);
      root.innerHTML = "";
    },
  };
}
