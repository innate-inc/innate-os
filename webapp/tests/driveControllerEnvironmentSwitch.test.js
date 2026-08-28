// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";

// DriveController is browser code; these are the small surfaces its module and
// the shared RosClient touch at construction time.
globalThis.window = new EventTarget();
globalThis.document = Object.assign(new EventTarget(), {
  visibilityState: "visible",
  documentElement: { classList: { contains: () => false } },
});
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };

const { DriveController } = await import("../js/driveController.js");
const drive = new DriveController();
let latest = null;
drive.onActiveChange((state) => {
  latest = state;
});

drive.setInput("keyboard", 0, 1, true);
assert.equal(latest?.engaged, true);

document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: { active: true } }));
assert.deepEqual(latest, { source: null, x: 0, y: 0, engaged: false });

drive.setInput("joystick", 0.5, 0.5, true);
assert.equal(latest?.engaged, false, "input is ignored while the environment is changing");

document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: { active: false } }));
drive.setInput("joystick", 0.5, 0.5, true);
assert.equal(latest?.engaged, false, "a source held across the switch must return to neutral first");
drive.setInput("joystick", 0, 0, false);
drive.setInput("joystick", 0.5, 0.5, true);
assert.deepEqual(latest, { source: "joystick", x: 0.5, y: 0.5, engaged: true });

drive.haltAll();

document.documentElement.classList.contains = (name) => name === "sim-environment-switching";
const lateDrive = new DriveController();
let lateLatest = null;
lateDrive.onActiveChange((state) => {
  lateLatest = state;
});
lateDrive.setInput("keyboard", 0, 1, true);
assert.equal(lateLatest?.engaged, false, "a controller mounted under the overlay inherits the interlock");
document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: { active: false } }));
lateDrive.setInput("keyboard", 0, 1, true);
assert.equal(lateLatest?.engaged, false, "late controls also require a neutral release");
lateDrive.setInput("keyboard", 0, 0, false);
lateDrive.setInput("keyboard", 0, 1, true);
assert.equal(lateLatest?.engaged, true);
lateDrive.haltAll();

document.documentElement.classList.contains = () => false;
const idleDrive = new DriveController();
let idleLatest = null;
idleDrive.onActiveChange((state) => {
  idleLatest = state;
});
document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: { active: true } }));
document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: { active: false } }));
idleDrive.setInput("joystick", 0.25, 0.75, true);
assert.deepEqual(
  idleLatest,
  { source: "joystick", x: 0.25, y: 0.75, engaged: true },
  "an idle source must work on its first post-switch input",
);
idleDrive.haltAll();

console.log("ok - environment switching interlocks shared drive input");
