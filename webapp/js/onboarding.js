// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

// The shell owns persistence and routing; the Agent page owns the actual coach
// because its guidance follows a real conversation rather than a generic tour.
export const ONBOARDING_VERSION = 2;
export const ONBOARDING_SEEN_KEY = `innate.onboardingSeen.v${ONBOARDING_VERSION}`;
export const ONBOARDING_REQUEST_EVENT = "innate:onboarding-request";

export function markOnboardingSeen() {
  try {
    localStorage.setItem(ONBOARDING_SEEN_KEY, "1");
  } catch {
    // Locked-down browsers may reject storage; onboarding still works.
  }
}

/** @returns {{ shouldAutoStart: () => boolean, start: () => void }} */
export function createOnboarding() {
  function shouldAutoStart() {
    try {
      return !localStorage.getItem(ONBOARDING_SEEN_KEY);
    } catch {
      return true;
    }
  }

  function start() {
    window.dispatchEvent(new CustomEvent(ONBOARDING_REQUEST_EVENT));
  }

  return { shouldAutoStart, start };
}
