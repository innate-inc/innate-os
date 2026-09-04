// @ts-check
// The physical robot plays its motor voice through ALSA. Simulation publishes
// the identical synth as short mono PCM chunks because its container has no
// audio device. Chunks are scheduled back to back on the AudioContext clock;
// an HTMLAudioElement per chunk would click at every seam.

import { ros } from "./rosClient.js";
import { base64ToBytes, isRobotAudioSpeaker } from "./ttsAudio.js";

const TOPIC = "/motor_sound/audio";
const LEAD_S = 0.12;
const MAX_QUEUED_S = 0.5;

let started = false;
/** @type {AudioContext | null} */
let context = null;
let nextAt = 0;

export function initMotorSoundAudio() {
  if (started) return;
  started = true;

  // Creating/resuming the context inside the operator's first gesture satisfies
  // browser autoplay rules. Until then chunks are deliberately dropped.
  const unlock = () => {
    if (!context) {
      const Context = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
      if (Context) context = new Context();
    }
    void context?.resume();
  };
  window.addEventListener("pointerdown", unlock, { once: true, capture: true });
  window.addEventListener("keydown", unlock, { once: true, capture: true });
  // Suspending silences at most MAX_QUEUED_S of already-scheduled hum, which
  // plays out on resume; not worth tracking sources to stop them.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden" || !context) return;
    nextAt = 0;
    void context.suspend().catch(() => {});
  });

  ros.subscribe(TOPIC, (msg) => {
    if (!context || document.visibilityState === "hidden" || !isRobotAudioSpeaker() || typeof msg?.data !== "string") {
      return;
    }
    let payload;
    try {
      payload = JSON.parse(msg.data);
    } catch {
      return; // a malformed audio packet must not disrupt the shell
    }
    if (context.state === "running") {
      schedule(context, payload);
      return;
    }
    void context.resume().then(() => {
      if (context && document.visibilityState !== "hidden") schedule(context, payload);
    }).catch(() => {});
  }, undefined, "std_msgs/msg/String");
}

/** @param {AudioContext} ctx @param {{ sample_rate?: unknown, pcm?: unknown }} payload */
function schedule(ctx, payload) {
  const rate = Number(payload?.sample_rate);
  if (!Number.isFinite(rate) || rate < 8_000 || rate > 192_000 || typeof payload?.pcm !== "string") return;
  let bytes;
  try {
    bytes = base64ToBytes(payload.pcm);
  } catch {
    return;
  }
  if (!bytes.length || bytes.length % 2) return;

  const now = ctx.currentTime;
  if (nextAt > now + MAX_QUEUED_S) return; // backlog full: drop rather than drift late
  if (nextAt < now) nextAt = now + LEAD_S;

  const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
  const buffer = ctx.createBuffer(1, samples.length, rate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);
  source.start(nextAt);
  nextAt += buffer.duration;
}
