// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Collect page entry — the Teleop cockpit reused as a data-collection station.
// Same connect/cockpit lifecycle as teleop/datasets (connect card while
// disconnected; the cockpit kept across reconnecting, torn down on a real
// disconnect). The only addition over Teleop is the recording HUD overlay: it
// drives the recorder's idle → recording → completed loop while the operator
// teleoperates, and gates its Record button on the leader arm being engaged &
// streaming — fed in from the arm panel's onState callback.

import { ros } from "../rosClient.js";
import { drive } from "../driveController.js";
import { getConfig } from "../config.js";
import { robotSessionFactory } from "../robotSession.js";
import { mountPage } from "../pageMount.js";
import { createVideoStage, createAudioToggle } from "../teleop/videoStage.js";
import { createJoystick } from "../teleop/joystick.js";
import { createKeyboardDrive, createWasdChips } from "../teleop/keyboardDrive.js";
import { createHeadTilt } from "../teleop/headTilt.js";
import { createSpeedModes } from "../teleop/speedModes.js";
import { createTtsBar } from "../teleop/ttsBar.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createArmPanel } from "../teleop/armPanel.js";
import { createRecordPanel } from "./recordPanel.js";

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, releaseSession, createStage } = await robotSessionFactory();

/** @type {any} */
const config = await getConfig();

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
  // No camera switcher on this page; the shared session may arrive on another
  // page's view. (Sim sessions are per-page and lack the method.)
  session.showMainCamera?.();

  const videoStage = createStage ? createStage(root, session) : createVideoStage(root, session);

  const telemetryOverlay = overlay("overlay-top-left");
  const rightRail = overlay("overlay-right");
  const chipsOverlay = overlay("overlay-bottom-left");
  const stickOverlay = overlay("overlay-joystick");
  const ttsOverlay = overlay("overlay-tts");
  const armOverlay = overlay("overlay-arm");
  const recordOverlay = overlay("overlay-record");
  root.append(telemetryOverlay, rightRail, chipsOverlay, stickOverlay, ttsOverlay, armOverlay, recordOverlay);

  /** @param {string} className */
  function overlay(className) {
    const el = document.createElement("div");
    el.className = `overlay ${className}`;
    return el;
  }

  // Head control is hidden during learned-policy recording — the camera
  // viewpoint must stay fixed across episodes — and shown only in the
  // recorded-movement wizard, where head motion is captured and replayed.
  // (Mirrors the mobile app: RecordEpisodeScreen has no head slider;
  // RecordReplayScreen's JoystickControls does.)
  const headTilt = createHeadTilt(rightRail, ros);

  // Built before the arm panel so its onState can gate recording immediately.
  // modalRoot is the full cockpit layer so the new-skill modal fills the stage
  // rather than being clipped inside the small top-center overlay.
  const recordPanel = createRecordPanel(recordOverlay, ros, {
    modalRoot: root,
    onHeadControl: (allowed) => {
      headTilt.el.hidden = !allowed;
    },
  });

  const keyboard = createKeyboardDrive(drive);
  const parts = [
    videoStage,
    createTelemetry(telemetryOverlay, ros),
    ...(videoStage.audioEl ? [createAudioToggle(rightRail, session, videoStage.audioEl)] : []),
    // Driving speed matters here: episodes are more repeatable at a consistent pace, and
    // mars_app already nudges the robot to Medium when a recording starts. The picker is
    // shown so that default is visible and overridable rather than a mystery.
    createSpeedModes(rightRail, ros),
    headTilt,
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros),
    createArmPanel(armOverlay, ros, {
      onState: (s) => recordPanel.setArmReady(s.engaged && s.reading),
      hideServices: !!config.simControls,
    }),
    recordPanel,
    keyboard,
  ];

  session.start();

  return {
    destroy() {
      drive.haltAll();
      for (const part of parts) part.destroy();
      releaseSession(session);
      root.innerHTML = "";
    },
  };
}
