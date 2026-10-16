// @ts-check
// The browser as the robot's microphone, mirroring ttsAudio where it is the
// sim's speaker. Streams base64 PCM16 on /mic/audio, where MicroInput takes it in
// place of arecord and runs the robot's own VAD + transcription. Mounts in the
// sim, which has no microphone, and against a real robot whose stt_mic_source is
// "browser" (a laptop mic held near a visitor in a hall too loud for its own).
//
// Publishing pauses while a clip plays in this page: the sim's speaker is these
// same speakers, and the robot must not transcribe itself. A real robot speaks
// from its own speaker and ducks this stream on its side (/tts/is_playing).

import { MIC_AUDIO_TOPIC } from "../constants.js";
import { isTtsPlaying } from "../micAudioState.js";

const WORKLET_URL = "/js/agent/micWorklet.js";
// The rate MicroInput's VAD and the vendor wire formats are built around.
const SAMPLE_RATE = 24000;
const WAVEFORM_POINT_COUNT = 9;
// Batch and realtime VAD need audio after speech to close the utterance.
const VAD_TAIL_MS = 800;

/**
 * @typedef {{ on: boolean, busy: boolean, level: number, waveform: number[], error: string | null }} MicState
 */

/**
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {(state: MicState) => void} onState
 * @returns {{ start: () => Promise<void>, stop: () => void, destroy: () => void }}
 */
export function createMicStream(rosClient, onState) {
  /** @type {MicState} */
  let state = { on: false, busy: false, level: 0, waveform: emptyWaveform(), error: null };
  /** @type {MediaStream | null} */
  let media = null;
  /** @type {AudioContext | null} */
  let ctx = null;
  /** @type {(() => void) | null} */
  let unadvertise = null;
  let destroyed = false;
  let startId = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let stopTimer = null;

  /** @param {Partial<MicState>} patch */
  function patch(patch) {
    state = { ...state, ...patch };
    onState(state);
  }

  async function start() {
    if (stopTimer !== null) {
      clearTimeout(stopTimer);
      stopTimer = null;
      for (const track of media?.getAudioTracks() ?? []) track.enabled = true;
    }
    if (state.on || state.busy) return;
    const id = ++startId;
    if (!navigator.mediaDevices?.getUserMedia) {
      // getUserMedia exists only in a secure context — over plain http from
      // another machine the API is simply absent.
      patch({ error: "Open the app over https to use the microphone" });
      return;
    }
    patch({ busy: true, error: null });
    try {
      const nextMedia = await navigator.mediaDevices.getUserMedia({
        // Keeps the robot's own speech out of its ears; the gate in publish()
        // covers what leaks past it.
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (destroyed || id !== startId) {
        for (const track of nextMedia.getTracks()) track.stop();
        return;
      }
      media = nextMedia;
      // The mic device subscribes only once the agent runs, so the bridge has
      // nothing to infer the type from yet.
      unadvertise = rosClient.advertise(MIC_AUDIO_TOPIC, "std_msgs/msg/String");
      ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      await ctx.audioWorklet.addModule(WORKLET_URL);
      if (destroyed || id !== startId) return teardown();
      const chunker = new AudioWorkletNode(ctx, "mic-chunker");
      chunker.port.onmessage = (e) => publish(e.data);
      ctx.createMediaStreamSource(media).connect(chunker);
      // A processor whose output goes nowhere is not guaranteed to be pulled,
      // so the graph reaches the destination through a silent gain node.
      const mute = ctx.createGain();
      mute.gain.value = 0;
      chunker.connect(mute).connect(ctx.destination);
      patch({ on: true, busy: false });
    } catch (err) {
      if (destroyed || id !== startId) return;
      teardown();
      patch({ on: false, busy: false, level: 0, waveform: emptyWaveform(), error: describe(err) });
    }
  }

  /** @param {ArrayBuffer} buf */
  function publish(buf) {
    if (destroyed) return;
    const pcm = new Int16Array(buf);
    patch({ level: rms(pcm), waveform: waveformAmplitudes(pcm) });
    if (isTtsPlaying()) return;
    rosClient.publish(MIC_AUDIO_TOPIC, { data: toBase64(new Uint8Array(buf)) });
  }

  function teardown() {
    for (const track of media?.getTracks() ?? []) track.stop();
    media = null;
    void ctx?.close();
    ctx = null;
    unadvertise?.();
    unadvertise = null;
  }

  function stop() {
    if (state.busy) {
      startId++;
      teardown();
      patch({ on: false, busy: false, level: 0, waveform: emptyWaveform() });
      return;
    }
    if (!state.on || stopTimer !== null) return;
    // Disabled audio tracks emit zero-valued frames, so VAD gets silence while
    // speech after release never leaves the browser.
    for (const track of media?.getAudioTracks() ?? []) track.enabled = false;
    stopTimer = setTimeout(() => {
      stopTimer = null;
      startId++;
      teardown();
      patch({ on: false, level: 0, waveform: emptyWaveform() });
    }, VAD_TAIL_MS);
  }

  return {
    start,
    stop,
    destroy() {
      destroyed = true;
      if (stopTimer !== null) clearTimeout(stopTimer);
      startId++;
      teardown();
    },
  };
}

/** @param {Int16Array} pcm @returns {number} 0..1 */
function rms(pcm) {
  let sum = 0;
  for (const s of pcm) sum += s * s;
  return Math.sqrt(sum / pcm.length) / 32768;
}

/** @returns {number[]} */
function emptyWaveform() {
  return Array(WAVEFORM_POINT_COUNT).fill(0);
}

/** @param {Int16Array} pcm @returns {number[]} */
function waveformAmplitudes(pcm) {
  return Array.from({ length: WAVEFORM_POINT_COUNT }, (_, point) => {
    const start = Math.floor((point * pcm.length) / WAVEFORM_POINT_COUNT);
    const end = Math.floor(((point + 1) * pcm.length) / WAVEFORM_POINT_COUNT);
    let sum = 0;
    for (let i = start; i < end; i++) sum += pcm[i] * pcm[i];
    return Math.sqrt(sum / Math.max(1, end - start)) / 32768;
  });
}

/** @param {Uint8Array} bytes @returns {string} */
function toBase64(bytes) {
  let bin = "";
  // Chunked: fromCharCode spreads its arguments, and a whole chunk at once sits
  // close enough to the argument limit to be worth not testing.
  for (let i = 0; i < bytes.length; i += 1024) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 1024));
  }
  return btoa(bin);
}

/** @param {any} err @returns {string} */
function describe(err) {
  if (err?.name === "NotAllowedError") return "Microphone permission denied";
  if (err?.name === "NotFoundError") return "No microphone found";
  return err?.message || "Microphone unavailable";
}
