// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

// First missions are browser-local. Help is a separate, passive interface tour.
export const ONBOARDING_VERSION = 4;
export const ONBOARDING_SEEN_KEY = `innate.onboardingSeen.v${ONBOARDING_VERSION}`;
export const ONBOARDING_REQUEST_EVENT = "innate:onboarding-request";
export const ONBOARDING_START_SECTION = "agent";

export function markOnboardingSeen() {
  try {
    localStorage.setItem(ONBOARDING_SEEN_KEY, "1");
  } catch {
    // Locked-down browsers may reject storage; onboarding still works.
  }
}

export const FIRST_RUN_KEY = "innate.firstMission.v1";
export const FIRST_RUN_REQUEST_EVENT = "innate:first-run-request";
const COMPLETION_CHANNEL = "innate:first-mission:v1";
/** @type {{phase:string}|null} */
let inheritedCompletion = null;
/** @type {any} */
let unstoredFirstRun = null;
/** @type {Promise<void>|undefined} */
let completionReady;

export function saveFirstRun(/** @type {any} */ saved) {
  try {
    localStorage.setItem(FIRST_RUN_KEY, JSON.stringify(saved));
    unstoredFirstRun = null;
  } catch {
    // Storage denial cannot make a route remount replay this tab's mission.
    unstoredFirstRun = saved;
  }
}

function embeddingOrigin() {
  if (window.parent === window || !document.referrer) return null;
  try {
    const url = new URL(document.referrer);
    return ["http:", "https:"].includes(url.protocol) ? url.origin : null;
  } catch { return null; }
}

/** Keep only terminal completion on the stable broker origin. Active attempts
 * stay with their simulator: a new container must never resume an old world. */
export function publishFirstRunCompletion(/** @type {string} */ phase) {
  const origin = embeddingOrigin();
  if (origin && ["done", "skipped"].includes(phase)) {
    window.parent.postMessage({channel:COMPLETION_CHANNEL, type:"completed", phase}, origin);
  }
}

export function initializeFirstRunCompletion() {
  if (completionReady) return completionReady;
  const origin = embeddingOrigin();
  if (!origin) return Promise.resolve();
  completionReady = new Promise(resolve => {
    const requestId = crypto.randomUUID();
    const finish = () => {
      clearTimeout(timer);
      window.removeEventListener("message", receive);
      const saved = readFirstRun();
      if (saved?.phase) publishFirstRunCompletion(saved.phase);
      resolve(undefined);
    };
    const receive = (/** @type {MessageEvent} */ event) => {
      if (event.source !== window.parent || event.origin !== origin) return;
      const data = event.data;
      if (!data || data.channel !== COMPLETION_CHANNEL || data.type !== "completion"
        || data.requestId !== requestId || ![null, "done", "skipped"].includes(data.phase)) return;
      // A still-running local attempt wins over another session's completion.
      if (!readFirstRun() && data.phase) {
        inheritedCompletion = {phase:data.phase};
        try { localStorage.setItem(FIRST_RUN_KEY, JSON.stringify(inheritedCompletion)); } catch { /* session-only fallback */ }
      }
      finish();
    };
    window.addEventListener("message", receive);
    const timer = setTimeout(finish, 1500);
    window.parent.postMessage({channel:COMPLETION_CHANNEL, type:"get-completion", requestId}, origin);
  });
  return completionReady;
}

export function readFirstRun() {
  if (unstoredFirstRun) return unstoredFirstRun;
  try {
    const saved = JSON.parse(localStorage.getItem(FIRST_RUN_KEY) || "null");
    if (!saved || typeof saved !== "object") return inheritedCompletion;
    if (saved.phase === "choosing") return {phase:"choosing"};
    if (["done", "skipped"].includes(saved.phase)) return saved;
    if (!["starting", "playing"].includes(saved.phase)
      || !["put_it_away", "way_out", "other_side"].includes(saved.id)
      || typeof saved.attemptId !== "string"
      || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(saved.attemptId)
      || !Number.isFinite(saved.startedAt) || saved.startedAt <= 0) return null;
    return saved;
  } catch { return inheritedCompletion; }
}

export function shouldAutoStartOnboarding() {
  const saved = readFirstRun();
  if (["choosing", "starting", "playing"].includes(saved?.phase)) return true;
  if (saved?.phase === "done" || saved?.phase === "skipped") return false;
  try { return !localStorage.getItem(ONBOARDING_SEEN_KEY); } catch { return true; }
}

export function startFirstRun(restart = false) {
  window.dispatchEvent(new CustomEvent(FIRST_RUN_REQUEST_EVENT, {detail:{restart}}));
}

/** Only the embedding broker can reopen the chooser; it cannot select or run a mission.
 * @param {()=>void} onChoose */
export function installFirstMissionReplay(onChoose) {
  const origin = embeddingOrigin();
  if (!origin) return () => {};
  let lastRequest = "";
  const receive = (/** @type {MessageEvent} */ event) => {
    if (event.source !== window.parent || event.origin !== origin) return;
    const data = event.data;
    if (!data || data.channel !== COMPLETION_CHANNEL) return;
    if (data.type === "get-controls") {
      window.parent.postMessage({channel:COMPLETION_CHANNEL, type:"controls", canChoose:true}, origin);
    } else if (data.type === "choose" && typeof data.requestId === "string"
      && data.requestId.length > 0 && data.requestId.length <= 128) {
      if (data.requestId !== lastRequest) { lastRequest = data.requestId; onChoose(); }
      window.parent.postMessage({channel:COMPLETION_CHANNEL, type:"choose-accepted", requestId:data.requestId}, origin);
    }
  };
  window.addEventListener("message", receive);
  return () => window.removeEventListener("message", receive);
}
