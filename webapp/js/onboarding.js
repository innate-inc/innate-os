// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

// First missions are browser-local. Help is a separate, passive interface tour.
export const ONBOARDING_REQUEST_EVENT = "innate:onboarding-request";
export const ONBOARDING_START_SECTION = "agent";

export const FIRST_RUN_KEY = "innate.firstMission.v1";
export const FIRST_RUN_REQUEST_EVENT = "innate:first-run-request";
export const FIRST_MISSIONS = [
  { id: "put_it_away", environment: "apartment", title: "Put it away", setting: "The apartment", brief: "One LEGO brick. One box. A robot that needs your direction.", icon: "brick" },
  { id: "way_out", environment: "backrooms", title: "Find a way out", setting: "The Backrooms", brief: "Endless yellow rooms. Help MARS find the green exit.", icon: "exit" },
  { id: "other_side", environment: "intersection", title: "The other side", setting: "Crossroads", brief: "Watch the traffic. Guide MARS safely across the street.", icon: "crossing" },
];
const COMPLETION_CHANNEL = "innate:first-mission:v1";
/** @type {any} */
let unstoredFirstRun = null;

/** A v4 UUID. crypto.randomUUID is absent on the plain-http LAN address the sim
 * proxy also serves, where getRandomValues still works. */
export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
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
    const requestId = uuid();
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
      if (!readFirstRun() && data.phase) saveFirstRun({phase:data.phase});
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
    if (!saved || typeof saved !== "object") return null;
    if (saved.phase === "choosing") return {phase:"choosing"};
    if (["done", "skipped"].includes(saved.phase)) return saved;
    if (!["starting", "playing"].includes(saved.phase)
      || !FIRST_MISSIONS.some(({id}) => id === saved.id)
      || typeof saved.attemptId !== "string"
      || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(saved.attemptId)
      || !Number.isFinite(saved.startedAt) || saved.startedAt <= 0) return null;
    return saved;
  } catch { return null; }
}

export function shouldAutoStartOnboarding() {
  const phase = readFirstRun()?.phase;
  return phase !== "done" && phase !== "skipped";
}

/** Reopen the mission chooser. The mounted Agent page answers by closing the
 * owned attempt first; with no controller mounted the request fails at once.
 * @param {(success:boolean)=>void} [complete] */
export function startFirstRun(complete) {
  const event = new CustomEvent(FIRST_RUN_REQUEST_EVENT, {cancelable:true, detail:{complete}});
  window.dispatchEvent(event);
  if (!event.defaultPrevented) complete?.(false);
}

/** Installed only in simulator mode. The trusted broker can reopen the existing
 * mission picker; the controller owns stopping the old attempt and scene setup.
 * @param {()=>Promise<boolean>} onOpen */
export function installMissionPicker(onOpen) {
  const origin = embeddingOrigin();
  if (!origin) return () => {};
  /** @type {Map<string, Promise<boolean>>} */
  const requests = new Map();
  let busy = false;
  let removed = false;
  const send = (/** @type {any} */ data) => {
    if (!removed) window.parent.postMessage({channel:COMPLETION_CHANNEL, ...data}, origin);
  };
  const controls = () => send({type:"controls", canOpenMissionPicker:true, busy});
  const receive = (/** @type {MessageEvent} */ event) => {
    if (event.source !== window.parent || event.origin !== origin) return;
    const data = event.data;
    if (!data || data.channel !== COMPLETION_CHANNEL) return;
    if (data.type === "get-controls") {
      controls();
    } else if (data.type === "open-mission-picker"
      && typeof data.requestId === "string" && data.requestId.length > 0 && data.requestId.length <= 128) {
      let result = requests.get(data.requestId);
      if (!result) {
        // One answer per id: a request refused while busy stays refused when the
        // broker re-delivers it, instead of closing a newer mission.
        if (busy) result = Promise.resolve(false);
        else {
          busy = true;
          controls();
          result = Promise.resolve().then(onOpen).catch(() => false)
            .finally(() => {busy = false; controls();});
        }
        requests.set(data.requestId, result);
        const oldest = requests.keys().next().value;
        if (requests.size > 32 && oldest !== undefined) requests.delete(oldest);
      }
      void result.then(success => send({type:"mission-picker-opened", requestId:data.requestId, success}));
    }
  };
  window.addEventListener("message", receive);
  controls(); // a broker that asked before this listener existed still learns the control is available
  return () => {removed = true; window.removeEventListener("message", receive);};
}
