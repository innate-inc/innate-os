// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import {
  ONBOARDING_REQUEST_EVENT,
  ONBOARDING_SEEN_KEY,
  ONBOARDING_VERSION,
} from "../js/onboarding.js";
import { FIRST_NUDGE, REPLY_NUDGE } from "../js/agent/agentOnboarding.js";

assert.equal(ONBOARDING_SEEN_KEY, `innate.onboardingSeen.v${ONBOARDING_VERSION}`);
assert.equal(ONBOARDING_VERSION, 2);
assert.equal(ONBOARDING_REQUEST_EVENT, "innate:onboarding-request");
assert.match(FIRST_NUDGE.body, /Agent menu/);
assert.match(FIRST_NUDGE.body, /type a message/);
assert.ok(FIRST_NUDGE.examples.some((example) => /see|wave/i.test(example)));
assert.match(REPLY_NUDGE.body, /navigate/);
assert.match(REPLY_NUDGE.body, /wave/);
assert.match(REPLY_NUDGE.body, /pick up/);

for (const nudge of [FIRST_NUDGE, REPLY_NUDGE]) {
  assert.ok(nudge.eyebrow.trim());
  assert.ok(nudge.title.trim());
  assert.ok(nudge.body.trim());
}

console.log("ok - conversation onboarding contract");
