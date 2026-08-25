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
import { createJoystick } from "./joystick.js";
import { createKeyboardDrive, createWasdChips } from "./keyboardDrive.js";
import { createHeadTilt } from "./headTilt.js";
import { createSpeedModes } from "./speedModes.js";
import { createTtsBar } from "./ttsBar.js";
import { createTelemetry } from "./telemetry.js";
import { createArmPanel } from "./armPanel.js";
import { createProfilingPanel } from "./profilingPanel.js";
import { createSkillsMenu } from "./skillsMenu.js";
import { createCameraSwitch } from "./cameraSwitch.js";
import { dismissAllConfirms } from "../nav/confirm.js";

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
  const parts = [videoStage, ...(telemetry ? [telemetry] : [])];
  // Robot-mic toggle. Skipped in the sim: the simulator's WebRTC server streams
  // video only (no microphone), so the toggle would do nothing. config.simControls
  // is the sim deployment's feature flag (env-driven; false on the real robot).
  if (!config.simControls && videoStage.audioEl) {
    parts.push(createAudioToggle(rightRail, session, videoStage.audioEl));
  }
  const cameraSwitch = createCameraSwitch(root, session, ros);
  parts.push(
    createSpeedModes(rightRail, ros),
    createRelocateButton(rightRail, cameraSwitch.openRelocate),
    createHeadTilt(rightRail, ros),
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros),
    // Collapsible skill launcher pinned next to the speak bar.
    createSkillsMenu(ttsOverlay, ros),
    createArmPanel(armOverlay, ros, { hideServices: !!config.simControls }),
    ...(config.simControls ? [] : [createProfilingPanel(root, session)]),
    cameraSwitch,
    keyboard,
  );

  session.start();

  return {
    destroy() {
      drive.haltAll();
      // Confirm dialogs (speed picker) live on document.body — sweep them so
      // navigating away doesn't leave one floating over the next page.
      dismissAllConfirms();
      for (const part of parts) part.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}

/**
 * Keep relocation one click away while driving: the map switcher promotes the
 * live map and opens the exact Auto / Manual controls used by Navigation.
 * @param {HTMLElement} parent
 * @param {() => void} openRelocate
 * @returns {{ destroy: () => void }}
 */
function createRelocateButton(parent, openRelocate) {
  const wrap = document.createElement("div");
  wrap.className = "relocate-control";

  const label = document.createElement("span");
  label.className = "relocate-label";
  label.textContent = "RELOCATE";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-toggle relocate-button";
  button.title = "Relocate robot on the map (auto or manual)";
  button.setAttribute("aria-label", "Relocate robot");
  button.innerHTML =
    '<svg viewBox="0 0 16 16" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true">' +
    '<circle cx="8" cy="8" r="4.5"/><path d="M8 .8v2.4M8 12.8v2.4M.8 8h2.4M12.8 8h2.4"/>' +
    "</svg>";
  button.addEventListener("click", openRelocate);

  wrap.append(label, button);
  parent.appendChild(wrap);
  return {
    destroy() {
      button.removeEventListener("click", openRelocate);
      wrap.remove();
    },
  };
}
