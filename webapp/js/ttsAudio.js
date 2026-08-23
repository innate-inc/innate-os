// @ts-check
// Robot speech playback. In SIM mode the brain publishes synthesized speech
// identified WAV clips on /tts/audio whenever it speaks — "make the robot
// speak", agent replies, skill narration. The sim has no audio device, so the
// browser is the speaker. The real robot plays speech out its own physical
// speaker and publishes nothing here (a browser playing it too would double
// the voice), so against a robot this module simply never fires.
// Mounted from the shell (which loads on every page), so speech plays no matter
// which page is open.

import { ros } from "./rosClient.js";
import { TTS_AUDIO_TOPIC, TTS_PLAYBACK_TOPIC } from "./constants.js";
import { isMicAudioActive, setTtsPlaying } from "./micAudioState.js";

let started = false;

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
  ros.advertise(TTS_PLAYBACK_TOPIC, "std_msgs/msg/String");

  ros.subscribe(TTS_AUDIO_TOPIC, (msg) => {
    if (!speaker) return; // another tab is the elected speaker
    const clip = parseClip(msg?.data);
    if (clip === null) return;
    // Defensive: if a clip does arrive while the operator has the robot mic
    // open, skip it — the speaker would be heard through the mic as well.
    if (isMicAudioActive()) {
      report(clip, "aborted");
      return;
    }
    enqueue(clip);
  }, undefined, "std_msgs/msg/String");
}

// The brain waits for this speaker's ended/aborted event before publishing the
// next clip. The local queue still absorbs reconnect and legacy-client bursts.
/** @typedef {{ id: string | null, audio: string, nearEndLeadSeconds: number }} TtsClip */
/** @type {TtsClip[]} */
const pending = [];
let playing = false;

// A hidden or muted tab must not bank a monologue and deliver it late (the same
// reason speak_text_async drops superseded speech).
const MAX_PENDING = 4;

/** @param {TtsClip} clip */
function enqueue(clip) {
  pending.push(clip);
  while (pending.length > MAX_PENDING) {
    const dropped = pending.shift();
    if (dropped) report(dropped, "aborted");
    console.warn("[tts] playback backlog full — dropping the oldest clip");
  }
  if (!playing) playNext();
}

function playNext() {
  const clip = pending.shift();
  if (clip === undefined) {
    playing = false;
    return;
  }
  playing = true;
  try {
    play(clip);
  } catch (err) {
    console.warn("[tts] failed to play audio:", err);
    report(clip, "aborted");
    playNext(); // one bad clip must not strand the rest of the reply
  }
}

/** @param {TtsClip} clip */
function play(clip) {
  const blob = new Blob([/** @type {BlobPart} */ (base64ToBytes(clip.audio))], { type: "audio/wav" });
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  // The mic stream stops publishing while this is set, so every path must
  // release it — one that never finishes mutes the microphone for the session.
  setTtsPlaying(true);
  let released = false;
  let nearEndSent = false;
  const nearEnd = () => {
    if (nearEndSent || clip.nearEndLeadSeconds <= 0) return;
    if (!Number.isFinite(audio.duration)) return;
    if (audio.duration - audio.currentTime > clip.nearEndLeadSeconds) return;
    nearEndSent = true;
    report(clip, "near_end");
  };
  /** @param {"ended" | "aborted"} event */
  const done = (event) => {
    if (released) return;
    released = true;
    if (event === "ended" && !nearEndSent && clip.nearEndLeadSeconds > 0) {
      nearEndSent = true;
      report(clip, "near_end");
    }
    report(clip, event);
    setTtsPlaying(false);
    URL.revokeObjectURL(url);
    playNext();
  };
  audio.addEventListener("playing", () => report(clip, "started"), { once: true });
  audio.addEventListener("timeupdate", nearEnd);
  audio.addEventListener("ended", () => done("ended"), { once: true });
  audio.addEventListener("error", () => done("aborted"), { once: true });
  audio.play().catch((err) => {
    // Browser autoplay policies block playback until the user has interacted
    // with the page; after any click/keypress this succeeds.
    console.warn("[tts] autoplay blocked (interact with the page first):", err?.message || err);
    done("aborted");
  });
}

/** @param {unknown} data @returns {TtsClip | null} */
function parseClip(data) {
  if (typeof data !== "string" || !data) return null;
  try {
    const parsed = JSON.parse(data);
    if (typeof parsed?.id !== "string" || typeof parsed?.audio !== "string") return null;
    return {
      id: parsed.id,
      audio: parsed.audio,
      nearEndLeadSeconds: Number(parsed.near_end_lead_seconds) || 0,
    };
  } catch {
    return { id: null, audio: data, nearEndLeadSeconds: 0 };
  }
}

/** @param {TtsClip} clip @param {"started" | "near_end" | "ended" | "aborted"} event */
function report(clip, event) {
  if (clip.id === null) return;
  ros.publish(TTS_PLAYBACK_TOPIC, {
    data: JSON.stringify({ id: clip.id, event }),
  });
}

/** @param {string} b64 @returns {Uint8Array} */
function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
