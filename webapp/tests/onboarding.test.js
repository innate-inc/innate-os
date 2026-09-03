// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import {
  ONBOARDING_REQUEST_EVENT,
  ONBOARDING_SEEN_KEY,
  ONBOARDING_VERSION,
} from "../js/onboarding.js";
import { FIRST_NUDGE, INTRO_NUDGE, J3SO_NUDGE, REPLY_NUDGE, SWITCH_NUDGE } from "../js/agent/agentOnboarding.js";
import {
  TELEOP_ONBOARDING_PROGRESS_KEY,
  TELEOP_ONBOARDING_STEPS,
  SPEECH_UNAVAILABLE_STEP,
  boundingSpotlightRect,
  copyForStep,
  isPickupCompletion,
  isWaveCompletion,
  paddedSpotlightRect,
  positionAboveTarget,
  resolveAvailableStep,
  resolvePreviousStep,
  SPEAK_ADVANCE_MS,
  runningCopy,
} from "../js/teleop/teleopOnboarding.js";

assert.equal(ONBOARDING_SEEN_KEY, `innate.onboardingSeen.v${ONBOARDING_VERSION}`);
assert.equal(ONBOARDING_VERSION, 3);
assert.equal(ONBOARDING_REQUEST_EVENT, "innate:onboarding-request");
assert.equal(TELEOP_ONBOARDING_PROGRESS_KEY, `innate.teleopOnboardingProgress.v${ONBOARDING_VERSION}`);
assert.match(TELEOP_ONBOARDING_STEPS.intro.body, /microphone to hear what it hears/);
assert.match(TELEOP_ONBOARDING_STEPS.wave.body, /\{shortcut\}/);
assert.match(TELEOP_ONBOARDING_STEPS.talk.body, /speech bar/);
assert.match(TELEOP_ONBOARDING_STEPS.pick.body, /Pick Any Object/);
assert.match(TELEOP_ONBOARDING_STEPS.pick.body, /red LEGO brick/);
assert.match(TELEOP_ONBOARDING_STEPS.agent.body, /combine skills/);
assert.ok(isWaveCompletion({ skillId: "innate-os/wave" }));
assert.ok(isPickupCompletion({ skillId: "innate-os/pick_any_object" }));
assert.ok(!isPickupCompletion({ skillId: "innate-os/wave" }));
assert.equal(resolveAvailableStep("talk", false), "talk");
assert.equal(resolveAvailableStep("talk", true), "talk");
assert.equal(resolveAvailableStep("talk", null), "talk");
assert.equal(copyForStep("talk", false), SPEECH_UNAVAILABLE_STEP);
assert.equal(copyForStep("talk", true), TELEOP_ONBOARDING_STEPS.talk);
assert.match(SPEECH_UNAVAILABLE_STEP.body, /INNATE_SERVICE_KEY/);
assert.match(SPEECH_UNAVAILABLE_STEP.body, /Cartesia/);
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
assert.deepEqual(
  boundingSpotlightRect([
    { left: 712, right: 992, top: 108, bottom: 648 },
    { left: 762, right: 992, top: 658, bottom: 702 },
  ]),
  { left: 712, right: 992, top: 108, bottom: 702 },
);
assert.equal(resolvePreviousStep("wave", null), "intro");
assert.equal(resolvePreviousStep("pick", true), "talk");
assert.equal(resolvePreviousStep("pick", false), "talk");
assert.ok(SPEAK_ADVANCE_MS >= 2000);
// A running card is a repaint of its own step, not a step of its own.
for (const step of ["wave", "pick"]) {
  assert.equal(runningCopy(step).eyebrow, TELEOP_ONBOARDING_STEPS[step].eyebrow);
  assert.equal(runningCopy(step).title, "Watch it happen");
}
assert.equal(INTRO_NUDGE.title, "This is Agent");
assert.match(INTRO_NUDGE.body, /one turn at a time/);
assert.match(FIRST_NUDGE.body, /type the message/);
assert.match(SWITCH_NUDGE.body, /agent menu/i);
assert.match(SWITCH_NUDGE.body, /J3SO/);
assert.deepEqual(FIRST_NUDGE.examples, ["What can you do?"]);
assert.match(REPLY_NUDGE.body, /navigate/);
assert.match(REPLY_NUDGE.body, /wave/);
assert.match(REPLY_NUDGE.body, /pick up/);

for (const nudge of [INTRO_NUDGE, FIRST_NUDGE, REPLY_NUDGE, SWITCH_NUDGE, J3SO_NUDGE]) {
  assert.ok(nudge.eyebrow.trim());
  assert.ok(nudge.title.trim());
  assert.ok(nudge.body.trim());
}

console.log("ok - conversation onboarding contract");
