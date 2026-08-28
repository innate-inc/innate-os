// @ts-check
// Robot speech playback. In SIM mode the brain publishes synthesized speech
// (base64 WAV) on /tts/audio whenever it speaks — "make the robot speak", agent
// replies, skill narration — because the sim has no audio device, so the
// browser is the speaker. The real robot plays speech out its own physical
// speaker and publishes nothing here (a browser playing it too would double
// the voice), so against a robot this module simply never fires.
// Mounted from the shell (which loads on every page), so speech plays no matter
// which page is open.

import { ros } from "./rosClient.js";
import { isMicAudioActive, setTtsPlaying } from "./micAudioState.js";

const TTS_AUDIO_TOPIC = "/tts/audio";

let started = false;
/** @type {Set<() => void>} */
const playbackStartListeners = new Set();

/** @param {() => void} listener */
export function onTtsPlaybackStart(listener) {
  playbackStartListeners.add(listener);
  return () => playbackStartListeners.delete(listener);
}

// One speaker across tabs: rosbridge fans /tts/audio out to every client, so
// N open tabs played N overlapping copies. A held Web Lock elects exactly one
// playing tab; when that tab closes, the browser passes the lock (and the
// voice) to the next one. Browsers without Web Locks keep the old behavior.
let speaker = !("locks" in navigator);
navigator.locks?.request("innate-tts-speaker", () => {
  speaker = true;
  return new Promise(() => {}); // hold until this tab closes
});

export function initTtsAudio() {
  if (started) return;
  started = true;

  ros.subscribe(TTS_AUDIO_TOPIC, (msg) => {
    if (!speaker) return; // another tab is the elected speaker
    const b64 = msg?.data;
    if (typeof b64 !== "string" || !b64) return;
    // Defensive: if a clip does arrive while the operator has the robot mic
    // open, skip it — the speaker would be heard through the mic as well.
    if (isMicAudioActive()) return;
    enqueue(b64);
  }, undefined, "std_msgs/msg/String");
}

// The robot speaks one utterance at a time, because speak_text blocks until
// aplay finishes. In sim a clip is "done" once published, so the robot half can
// only serialize synthesis — played on arrival, a two-sentence reply talks over
// itself. This queue is what puts that behavior back.
/** @type {string[]} */
const pending = [];
let playing = false;

// A hidden or muted tab must not bank a monologue and deliver it late (the same
// reason speak_text_async drops superseded speech).
const MAX_PENDING = 4;

/** @param {string} b64 */
function enqueue(b64) {
  pending.push(b64);
  while (pending.length > MAX_PENDING) {
    pending.shift();
    console.warn("[tts] playback backlog full — dropping the oldest clip");
  }
  if (!playing) playNext();
}

function playNext() {
  const b64 = pending.shift();
  if (b64 === undefined) {
    playing = false;
    return;
  }
  playing = true;
  try {
    play(b64);
  } catch (err) {
    console.warn("[tts] failed to play audio:", err);
    playNext(); // one bad clip must not strand the rest of the reply
  }
}

/** @param {string} b64 base64-encoded WAV */
function play(b64) {
  const blob = new Blob([/** @type {BlobPart} */ (base64ToBytes(b64))], { type: "audio/wav" });
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  // The mic stream stops publishing while this is set, so every path must
  // release it — one that never finishes mutes the microphone for the session.
  setTtsPlaying(true);
  let released = false;
  const done = () => {
    if (released) return;
    released = true;
    setTtsPlaying(false);
    URL.revokeObjectURL(url);
    playNext();
  };
  audio.addEventListener("ended", done, { once: true });
  audio.addEventListener("error", done, { once: true });
  audio.addEventListener("playing", () => {
    for (const listener of playbackStartListeners) listener();
  }, { once: true });
  audio.play().catch((err) => {
    // Browser autoplay policies block playback until the user has interacted
    // with the page; after any click/keypress this succeeds.
    console.warn("[tts] autoplay blocked (interact with the page first):", err?.message || err);
    done();
  });
}

/** @param {string} b64 @returns {Uint8Array} */
function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
