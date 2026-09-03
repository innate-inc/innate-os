// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import {
  ONBOARDING_REQUEST_EVENT,
  ONBOARDING_SEEN_KEY,
  ONBOARDING_VERSION,
} from "../js/onboarding.js";
import { FIRST_NUDGE, INTRO_NUDGE, REPLY_NUDGE } from "../js/agent/agentOnboarding.js";
import {
  TELEOP_ONBOARDING_PROGRESS_KEY,
  TELEOP_ONBOARDING_STEPS,
  isCylinderPickupCompletion,
  isWaveCompletion,
  paddedSpotlightRect,
  positionAboveTarget,
  resolveAvailableStep,
} from "../js/teleop/teleopOnboarding.js";

assert.equal(ONBOARDING_SEEN_KEY, `innate.onboardingSeen.v${ONBOARDING_VERSION}`);
assert.equal(ONBOARDING_VERSION, 3);
assert.equal(ONBOARDING_REQUEST_EVENT, "innate:onboarding-request");
assert.equal(TELEOP_ONBOARDING_PROGRESS_KEY, `innate.teleopOnboardingProgress.v${ONBOARDING_VERSION}`);
assert.match(TELEOP_ONBOARDING_STEPS.intro.body, /microphone to hear what it hears/);
assert.match(TELEOP_ONBOARDING_STEPS.wave.body, /\{shortcut\}/);
assert.match(TELEOP_ONBOARDING_STEPS.talk.body, /speech bar/);
assert.match(TELEOP_ONBOARDING_STEPS.pick.body, /Pick Any Object/);
assert.match(TELEOP_ONBOARDING_STEPS.pick.body, /The Cylinder/);
assert.match(TELEOP_ONBOARDING_STEPS.agent.body, /combine skills/);
assert.ok(isWaveCompletion({ skillId: "innate-os/wave" }));
assert.ok(isCylinderPickupCompletion({ skillId: "innate-os/pick_any_object", inputs: { prompt: "The Cylinder" } }));
assert.ok(!isCylinderPickupCompletion({ skillId: "innate-os/pick_any_object", inputs: { prompt: "the sock" } }));
assert.equal(resolveAvailableStep("talk", false), "pick");
assert.equal(resolveAvailableStep("talk", true), "talk");
assert.equal(resolveAvailableStep("talk", null), "talk");
assert.deepEqual(
  positionAboveTarget(
    { left: 762, right: 992, top: 658 },
    { width: 350, height: 184 },
    { width: 1280, height: 720 },
  ),
  { left: 702, top: 458 },
);
assert.deepEqual(
  positionAboveTarget(
    { left: 80, right: 310, top: 120 },
    { width: 350, height: 184 },
    { width: 390, height: 720 },
  ),
  { left: 28, top: 12 },
);
assert.deepEqual(
  paddedSpotlightRect(
    { left: 762, right: 992, top: 658, bottom: 702 },
    { width: 1280, height: 720 },
  ),
  { left: 748, top: 644, width: 258, height: 72 },
);
assert.deepEqual(
  paddedSpotlightRect(
    { left: 4, right: 386, top: 4, bottom: 716 },
    { width: 390, height: 720 },
  ),
  { left: 0, top: 0, width: 390, height: 720 },
);
assert.equal(INTRO_NUDGE.title, "Meet MARS");
assert.match(INTRO_NUDGE.body, /control room/);
assert.match(FIRST_NUDGE.body, /Agent menu/);
assert.match(FIRST_NUDGE.body, /type the message/);
assert.deepEqual(FIRST_NUDGE.examples, ["What can you do?"]);
assert.match(REPLY_NUDGE.body, /navigate/);
assert.match(REPLY_NUDGE.body, /wave/);
assert.match(REPLY_NUDGE.body, /pick up/);

for (const nudge of [INTRO_NUDGE, FIRST_NUDGE, REPLY_NUDGE]) {
  assert.ok(nudge.eyebrow.trim());
  assert.ok(nudge.title.trim());
  assert.ok(nudge.body.trim());
}

console.log("ok - conversation onboarding contract");
