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
//   ...inside destroy: releaseSession(session), never session.destroy().
//
// Real robots hand every page the app-level shared WebRtcSession (see
// sharedVideoSession.js), so page switches reuse the live link. Sim sessions
// stay per-page: they render local canvases, so there is no link to keep warm.

import { WebRtcSession } from "./webrtcSession.js";
import { acquireVideoSession, releaseVideoSession } from "./sharedVideoSession.js";
import { getConfig } from "./config.js";

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
        createSession: () => /** @type {any} */ (mod.createSimSession()),
        releaseSession: (session) => session.destroy(),
        createStage: (root, session) => mod.createSimStage(root, /** @type {any} */ (session)),
      };
    } catch (err) {
      console.error("[robotSession] sim viewer bundle unavailable, falling back to WebRTC:", err);
    }
  }
  return { createSession: acquireVideoSession, releaseSession: releaseVideoSession, createStage: null };
}
