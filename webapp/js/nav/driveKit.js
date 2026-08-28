// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Mapping drive kit — the teleop controls the Nav page mounts over the scene
// while the robot is recording a map, mirroring the mobile app's
// RecordMapScreen (map + camera + joystick + head slider). You actively drive
// the robot to build the map, so this is pure reuse of the teleop modules:
// main-camera video as a PiP tile (WebRTC on robots, the Three.js sim viewer
// in sim — robotSessionFactory hides the difference), the virtual joystick,
// WASD keyboard drive (its typing guard keeps the map-name field safe), and
// head tilt. Torn down the moment mapping ends; the joystick/keyboard publish
// through the shared drive controller, so haltAll() on teardown zeroes the
// robot like every other page.

import { ros } from "../rosClient.js";
import { drive } from "../driveController.js";
import { robotSessionFactory } from "../robotSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import { createJoystick } from "../teleop/joystick.js";
import { createKeyboardDrive, createWasdChips } from "../teleop/keyboardDrive.js";
import { createHeadTilt } from "../teleop/headTilt.js";

// One factory for the page's lifetime (it fetches config + maybe the sim
// viewer bundle); the session is acquired per mapping run and released after.
const factoryPromise = robotSessionFactory();

/**
 * @param {HTMLElement} scene the map stage; all controls overlay it.
 * @returns {Promise<{ destroy: () => void }>}
 */
export async function createDriveKit(scene) {
  const { createSession, releaseSession, createStage } = await factoryPromise;
  const session = createSession();
  // The PiP always shows the main camera; the shared session may arrive on
  // another page's view. (Sim sessions are per-page and lack the method.)
  session.showMainCamera?.();

  // Main-camera PiP, bottom-right.
  const pip = document.createElement("div");
  pip.className = "nav-cam-pip";
  const stickOverlay = document.createElement("div");
  stickOverlay.className = "overlay overlay-joystick";
  const chipsOverlay = document.createElement("div");
  chipsOverlay.className = "overlay overlay-bottom-left";
  const rightRail = document.createElement("div");
  rightRail.className = "overlay overlay-right";
  scene.append(pip, stickOverlay, chipsOverlay, rightRail);

  const videoStage = createStage ? createStage(pip, session) : createVideoStage(pip, session);
  const keyboard = createKeyboardDrive(drive);
  const parts = [
    videoStage,
    createJoystick(stickOverlay, drive),
    createWasdChips(chipsOverlay, keyboard),
    createHeadTilt(rightRail, ros),
    keyboard,
  ];

  session.start();

  return {
    destroy() {
      drive.haltAll();
      for (const part of parts) part.destroy();
      releaseSession(session);
      pip.remove();
      stickOverlay.remove();
      chipsOverlay.remove();
      rightRail.remove();
    },
  };
}
