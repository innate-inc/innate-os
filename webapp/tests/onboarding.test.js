// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import { ONBOARDING_SEEN_KEY, ONBOARDING_STEPS, ONBOARDING_VERSION } from "../js/onboarding.js";

assert.equal(ONBOARDING_SEEN_KEY, `innate.onboardingSeen.v${ONBOARDING_VERSION}`);
assert.equal(ONBOARDING_STEPS[0].target, null, "tour opens with an unanchored welcome");
assert.equal(ONBOARDING_STEPS.at(-1)?.target, ".rail-help", "tour ends on its permanent launcher");
assert.ok(ONBOARDING_STEPS.some((step) => step.target?.includes('data-section="settings"')));
assert.equal(new Set(ONBOARDING_STEPS.map((step) => step.title)).size, ONBOARDING_STEPS.length);

for (const step of ONBOARDING_STEPS) {
  assert.ok(step.eyebrow.trim());
  assert.ok(step.title.trim());
  assert.ok(step.body.trim());
}

console.log(`ok - ${ONBOARDING_STEPS.length}-step onboarding contract`);
