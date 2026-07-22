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
import { WebRtcSession } from "../webrtcSession.js";
import { robotSessionFactory } from "../robotSession.js";
import { mountPage } from "../pageMount.js";
import { createVideoStage, createAudioToggle } from "./videoStage.js";
import { createJoystick } from "./joystick.js";
import { createKeyboardDrive, createWasdChips } from "./keyboardDrive.js";
import { createHeadTilt } from "./headTilt.js";
import { createRecordButton } from "./recordButton.js";
import { createTtsBar } from "./ttsBar.js";
import { createTelemetry } from "./telemetry.js";
import { createArmPanel } from "./armPanel.js";
import { createProfilingPanel } from "./profilingPanel.js";
import { createSkillsMenu } from "./skillsMenu.js";
import { createCameraSwitch } from "./cameraSwitch.js";

// Runtime feature flags (config.json, served static). Sim-only debug controls are
// off unless a deployment opts in. Fetched once when this module first loads (the
// router's dynamic import awaits this), so buildCockpit can read it synchronously.
/** @type {any} */
const config = await fetch("/config.json", { cache: "no-store" })
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}));

// Console debugging hook (also handy alongside the Logging page).
/** @type {{ ros: typeof ros, drive: typeof drive, session: WebRtcSession | null }} */
const dbg = { ros, drive, session: null };
/** @type {any} */ (window).innate = dbg;

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, createStage } = await robotSessionFactory();

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "cockpit", buildCockpit);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildCockpit(root) {
  const session = createSession();
  dbg.session = session;

  const videoStage = createStage ? createStage(root, session) : createVideoStage(root, session);

  const telemetryOverlay = overlay("overlay-top-left");
  const rightRail = overlay("overlay-right");
  const chipsOverlay = overlay("overlay-bottom-left");
  const stickOverlay = overlay("overlay-joystick");
  const ttsOverlay = overlay("overlay-tts");
  const armOverlay = overlay("overlay-arm");
  root.append(telemetryOverlay, rightRail, chipsOverlay, stickOverlay, ttsOverlay, armOverlay);

  /** @param {string} className */
  function overlay(className) {
    const el = document.createElement("div");
    el.className = `overlay ${className}`;
    return el;
  }

  const keyboard = createKeyboardDrive(drive);
  // No battery in the sim (the simulator has no power sensor).
  const parts = [videoStage, createTelemetry(telemetryOverlay, ros, { showBattery: !config.simControls })];
  // Robot-mic toggle. Skipped in the sim: the simulator's WebRTC server streams
  // video only (no microphone), so the toggle would do nothing. config.simControls
  // is the sim deployment's feature flag (env-driven; false on the real robot).
  if (!config.simControls && videoStage.audioEl) {
    parts.push(createAudioToggle(rightRail, session, videoStage.audioEl));
    // Camera MP4 recorder lives in the robot bringup, which the sim doesn't run.
    parts.push(createRecordButton(rightRail, ros));
  }
  parts.push(
    createHeadTilt(rightRail, ros),
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros),
    // Collapsible skill launcher pinned next to the speak bar.
    createSkillsMenu(ttsOverlay, ros),
    createArmPanel(armOverlay, ros, { hideServices: !!config.simControls }),
    ...(config.simControls ? [] : [createProfilingPanel(root, session)]),
    createCameraSwitch(root, session, ros),
    keyboard,
  );

  session.start();

  return {
    destroy() {
      drive.haltAll();
      for (const part of parts) part.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}
