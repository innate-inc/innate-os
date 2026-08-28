// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

// The shell owns persistence and routing; Teleop and Agent own their contextual
// coaches because guidance follows real actions rather than a generic tour.
export const ONBOARDING_VERSION = 3;
export const ONBOARDING_SEEN_KEY = `innate.onboardingSeen.v${ONBOARDING_VERSION}`;
export const ONBOARDING_REQUEST_EVENT = "innate:onboarding-request";
export const AGENT_ONBOARDING_PENDING_KEY = `innate.agentOnboardingPending.v${ONBOARDING_VERSION}`;

export function markOnboardingSeen() {
  try {
    localStorage.setItem(ONBOARDING_SEEN_KEY, "1");
  } catch {
    // Locked-down browsers may reject storage; onboarding still works.
  }
}

export function shouldAutoStartOnboarding() {
  try {
    return !localStorage.getItem(ONBOARDING_SEEN_KEY);
  } catch {
    return true;
  }
}

export function requestAgentOnboarding() {
  try {
    sessionStorage.setItem(AGENT_ONBOARDING_PENDING_KEY, "1");
  } catch {
    // The shell also sees the navigation; locked storage only loses the handoff.
  }
}

export function consumeAgentOnboardingRequest() {
  try {
    if (!sessionStorage.getItem(AGENT_ONBOARDING_PENDING_KEY)) return false;
    sessionStorage.removeItem(AGENT_ONBOARDING_PENDING_KEY);
    return true;
  } catch {
    return false;
  }
}

/** @returns {{ shouldAutoStart: () => boolean, start: (restart?: boolean) => void }} */
export function createOnboarding() {
  function shouldAutoStart() {
    return shouldAutoStartOnboarding();
  }

  /** @param {boolean} [restart] */
  function start(restart = false) {
    window.dispatchEvent(new CustomEvent(ONBOARDING_REQUEST_EVENT, { detail: { restart } }));
  }

  return { shouldAutoStart, start };
}
