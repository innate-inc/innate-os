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
} from "../js/teleop/teleopOnboarding.js";

assert.equal(ONBOARDING_SEEN_KEY, `innate.onboardingSeen.v${ONBOARDING_VERSION}`);
assert.equal(ONBOARDING_VERSION, 3);
assert.equal(ONBOARDING_REQUEST_EVENT, "innate:onboarding-request");
assert.equal(TELEOP_ONBOARDING_PROGRESS_KEY, `innate.teleopOnboardingProgress.v${ONBOARDING_VERSION}`);
assert.match(TELEOP_ONBOARDING_STEPS.intro.body, /move it around/);
assert.match(TELEOP_ONBOARDING_STEPS.wave.body, /\{shortcut\}/);
assert.match(TELEOP_ONBOARDING_STEPS.talk.body, /speech bar/);
assert.match(TELEOP_ONBOARDING_STEPS.pick.body, /Pick Any Object/);
assert.match(TELEOP_ONBOARDING_STEPS.pick.body, /The Cylinder/);
assert.match(TELEOP_ONBOARDING_STEPS.agent.body, /combine skills/);
assert.ok(isWaveCompletion({ skillId: "innate-os/wave" }));
assert.ok(isCylinderPickupCompletion({ skillId: "innate-os/pick_any_object", inputs: { prompt: "The Cylinder" } }));
assert.ok(!isCylinderPickupCompletion({ skillId: "innate-os/pick_any_object", inputs: { prompt: "the sock" } }));
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
