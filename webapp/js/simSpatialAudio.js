// @ts-check
// Distance falloff for sound emitted inside the simulated world. The viewer
// dispatches a perspective snapshot after every primary render (null on
// teardown; never on hardware pages). Robot-camera views stay at full
// loudness; only the third-person orbit view fades sound with camera distance.

export const SIM_AUDIO_PERSPECTIVE_EVENT = "innate:sim-audio-perspective";

const FULL_VOLUME_RADIUS_M = 2;
const ROLLOFF_M = 2.5;
const FAR_GAIN = 0.08;

/** @typedef {[number, number, number]} Point3 */
/** @typedef {{ view: "orbit" | "main" | "arm", listener: Point3, sources: Record<string, Point3> }} Perspective */

/** @type {Perspective | null} */
let perspective = null;
/** @type {Set<() => void>} */
const followers = new Set();

document.addEventListener(SIM_AUDIO_PERSPECTIVE_EVENT, (event) => {
  perspective = /** @type {CustomEvent<Perspective | null>} */ (event).detail;
  for (const notify of followers) notify();
});

/** @param {string | null | undefined} source */
export function simAudioGain(source) {
  const point = source == null ? undefined : perspective?.sources[source];
  if (!perspective || perspective.view !== "orbit" || !point) return 1;
  const [lx, ly, lz] = perspective.listener;
  const distance = Math.hypot(point[0] - lx, point[1] - ly, point[2] - lz);
  if (distance <= FULL_VOLUME_RADIUS_M) return 1;
  const beyondNearField = (distance - FULL_VOLUME_RADIUS_M) / ROLLOFF_M;
  return Math.max(FAR_GAIN, 1 / (1 + beyondNearField * beyondNearField));
}

/**
 * Feed `apply` the gain for `source` now and on every viewer frame.
 * @param {string | null | undefined} source @param {(gain: number) => void} apply
 * @returns {() => void} stop following
 */
export function followSimAudioSource(source, apply) {
  const notify = () => apply(simAudioGain(source));
  followers.add(notify);
  notify();
  return () => followers.delete(notify);
}
