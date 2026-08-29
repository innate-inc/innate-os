// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Focused placement contract for the persistent map moving stage <-> PiP.

import assert from "node:assert/strict";
import { mapViewCenter } from "../js/map/framing.js";
import { mapPresentation } from "../js/teleop/cameraSwitch.js";

const full = mapPresentation("__map__");
assert.deepEqual(full, {
  big: true,
  mode: "big",
  followRobot: false,
});

const corner = mapPresentation("main");
assert.deepEqual(corner, {
  big: false,
  mode: "small",
  followRobot: true,
});

const robot = { x: 4, y: 7, yaw: 0.5 };
const stalePan = { x: -20, y: 35 };
assert.equal(mapViewCenter(robot, stalePan, 6, corner.followRobot), robot);
assert.equal(mapViewCenter(robot, stalePan, 16, full.followRobot), stalePan);

console.log("4 passed");
