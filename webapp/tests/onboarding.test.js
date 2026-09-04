// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ONBOARDING_REQUEST_EVENT,
  ONBOARDING_SEEN_KEY,
  ONBOARDING_START_SECTION,
  ONBOARDING_VERSION,
} from "../js/onboarding.js";
import {
  AGENT_ONBOARDING_PROGRESS_KEY,
  backendReadinessFromMessage,
  GUIDED_PROMPTS,
  hasIntroAgent,
  ONBOARDING_GREETING,
  parseRevealSections,
  parsePromptStage,
  REVEAL_SECTIONS,
  revealSectionFromMessage,
} from "../js/agent/agentOnboarding.js";
import { isInternalOnboardingSkill } from "../js/agent/chatStream.js";
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
assert.equal(ONBOARDING_VERSION, 4);
assert.equal(ONBOARDING_REQUEST_EVENT, "innate:onboarding-request");
assert.equal(ONBOARDING_START_SECTION, "agent");
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
assert.equal(AGENT_ONBOARDING_PROGRESS_KEY, `innate.agentOnboarding.v${ONBOARDING_VERSION}`);
assert.deepEqual(REVEAL_SECTIONS, ["cameras", "controls", "complete"]);
assert.match(ONBOARDING_GREETING, /^Hi, I’m MARS/);
assert.doesNotMatch(ONBOARDING_GREETING, /hold Space|type in the chat/i);
assert.equal(GUIDED_PROMPTS.capabilities, "What can you do?");
assert.equal(GUIDED_PROMPTS.pickup, "Pick up this Lego piece in front of you.");
assert.equal(hasIntroAgent({ agents: [{ id: "intro_agent" }] }), true);
assert.equal(hasIntroAgent({ agents: [{ id: "default_agent" }] }), false);
assert.equal(hasIntroAgent({ agents: [] }), false);
assert.equal(parsePromptStage({ promptStage: "pickup" }), "pickup");
assert.equal(parsePromptStage({ promptStage: "done" }), "done");
assert.equal(parsePromptStage({ promptStage: "unknown" }), "capabilities");
assert.deepEqual(
  parseRevealSections({ revealed: ["controls", "bogus", "cameras", "controls"] }),
  ["cameras", "controls"],
);
assert.deepEqual(parseRevealSections({ revealed: "cameras" }), []);
assert.equal(revealSectionFromMessage({ data: '{"section":"cameras"}' }), "cameras");
assert.equal(revealSectionFromMessage({ data: '{"section":"bogus"}' }), null);
assert.equal(revealSectionFromMessage({ data: "not-json" }), null);
assert.equal(backendReadinessFromMessage({ data: '{"state":"ready","connected":true}' }), true);
assert.equal(backendReadinessFromMessage({ data: '{"state":"invalid_config","connected":false}' }), false);
assert.equal(backendReadinessFromMessage({ data: '{"state":"starting","connected":false}' }), null);
assert.ok(isInternalOnboardingSkill("RevealOnboarding"));
assert.ok(isInternalOnboardingSkill("reveal_onboarding"));
assert.ok(!isInternalOnboardingSkill("wave"));

const appCss = readFileSync(new URL("../css/app.css", import.meta.url), "utf8");
assert.match(
  appCss,
  /\.agent-conversation-onboarding:not\(\.agent-onboarding-show-controls\)[\s\S]*?\.agent-control-panel\s*\{\s*display:\s*none;/,
);

const agentOnboardingSource = readFileSync(new URL("../js/agent/agentOnboarding.js", import.meta.url), "utf8");
assert.match(
  agentOnboardingSource,
  /const greeting = greetWhenBrainIsPresent\(startedAt\);/,
  "resuming an unfinished onboarding session must replay MARS's greeting",
);
assert.doesNotMatch(
  agentOnboardingSource,
  /fresh\s*\?\s*greetWhenBrainIsPresent/,
  "the greeting must not be limited to a brand-new browser session",
);
assert.match(
  agentOnboardingSource,
  /if \(fresh\) await options\.prepareEnvironment\?\.\(\);[\s\S]*?const greeting = greetWhenBrainIsPresent/,
  "a fresh onboarding must finish preparing its environment before MARS greets",
);

const agentMainSource = readFileSync(new URL("../js/agent/main.js", import.meta.url), "utf8");
assert.match(agentMainSource, /switchEnvironment\("backrooms"\)/);
assert.match(agentMainSource, /prepareEnvironment: prepareBackrooms/);

console.log("ok - conversation onboarding contract");
