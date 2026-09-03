// @ts-check
// Session factory: the camera panel's data source. Real robots stream WebRTC
// video (WebRtcSession); the simulator has no video pipeline -- SimSession
// (built from sim/viewer, lazy-loaded) renders the live sim with Three.js and
// exposes canvas captureStreams behind the exact same interface, so
// videoStage/cameraSwitch/profiling consume either without knowing.
//
// Usage (module top level -- the import must resolve before buildCockpit):
//   const { createSession, releaseSession, createStage } = await robotSessionFactory();
//   ...inside buildCockpit:
//     const session = createSession();
//     const stage = createStage ? createStage(root, session) : createVideoStage(root, session);
//   ...inside destroy: stage.destroy() then releaseSession(session), never session.destroy().
//
// Either backend hands every page ONE app-level session: real robots keep the
// live WebRTC link (sharedVideoSession.js), and the sim keeps its whole stage,
// whose page-facing destroy() only detaches it. Rebuilding that stage per page
// never gave its memory back, which killed the tab on a phone (simStage.ts).

import { WebRtcSession } from "./webrtcSession.js";
import { acquireVideoSession, releaseVideoSession } from "./sharedVideoSession.js";
import { getConfig } from "./config.js";

/** @typedef {{ audioEl: null, attach: (parent: HTMLElement) => void, detach: () => void, destroy: () => void, setSafeInsets: (insets: { right?: number }) => void }} SimStage */

// Long next to sharedVideoSession's 10s: a detour to Settings must not pay to
// reparse ~80 MB of models.
const SIM_LINGER_MS = 60_000;

/** @type {any} */ let simSession = null;
/** @type {SimStage | null} */ let simStage = null;
let simHolders = 0;
/** @type {number | null} */ let simLinger = null;

/**
 * @returns {Promise<{ createSession: () => WebRtcSession, releaseSession: (session: WebRtcSession) => void, createStage: ((root: HTMLElement, session: WebRtcSession) => { audioEl: HTMLAudioElement | null, destroy: () => void }) | null }>}
 * createStage is null for real robots (pages use createVideoStage); in sim it
 * mounts the live Three.js canvas (full resolution, drag-to-orbit).
 */
export async function robotSessionFactory() {
  /** @type {any} */
  const config = await getConfig();
  if (config.simControls) {
    try {
      // Served by proxy/https_server.py from sim/viewer's build -- a runtime
      // URL, not a module path tsc can resolve.
      // @ts-ignore
      const mod = await import("/sim-viewer/sim-session.js");
      return {
        createSession: () => acquireSimSession(mod, config),
        releaseSession: () => releaseSimSession(),
        createStage: (root, session) => acquireSimStage(mod, root, session),
      };
    } catch (err) {
      console.error("[robotSession] sim viewer bundle unavailable, falling back to WebRTC:", err);
    }
  }
  return { createSession: acquireVideoSession, releaseSession: releaseVideoSession, createStage: null };
}

/**
 * @param {any} mod
 * @param {any} config
 */
function acquireSimSession(mod, config) {
  if (simLinger !== null) {
    clearTimeout(simLinger);
    simLinger = null;
  }
  simSession ??= mod.createSimSession({ statePort: config.worldStatePort });
  simHolders += 1;
  return /** @type {any} */ (simSession);
}

function releaseSimSession() {
  if (!simSession || simHolders === 0) return;
  simHolders -= 1;
  if (simHolders > 0) return;
  simLinger = setTimeout(() => {
    simLinger = null;
    simStage?.destroy();
    simStage = null;
    simSession?.destroy();
    simSession = null;
  }, SIM_LINGER_MS);
}

/**
 * The one stage, re-parented into each page it serves.
 * @param {any} mod
 * @param {HTMLElement} root
 * @param {any} session
 */
function acquireSimStage(mod, root, session) {
  if (simStage) simStage.attach(root);
  else simStage = /** @type {SimStage} */ (mod.createSimStage(root, session));
  const stage = simStage;
  return { ...stage, destroy: () => stage.detach() };
}
