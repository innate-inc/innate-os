// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Teleop talkback behind a Meet-style mute toggle. Unmuted, the mic streams
// continuously and the room stays audible — full duplex through the robot's
// AEC — except while the operator speaks: voice activity drops local playback
// to DUCK_VOLUME (a duck, not a mute: loud room events and an interrupting
// voice still read through) and recovers DUCK_RELEASE_MS after the last
// syllable, long enough for the echo the AEC lets leak to finish its
// speaker→mic→return round trip. (A robot without AEC ducks the mic it sends
// while talk is on; there the operator hears nothing while unmuted.) Muting
// listens on until three seconds of room silence, as before.

import { createMicControl } from "../micControl.js";
import { createAudioToggle } from "./videoStage.js";

const WAVEFORM_POINT_COUNT = 9;
const FFT_SIZE = 256;
const RECEIVE_QUIET_MS = 3_000;
const REMOTE_ACTIVITY_THRESHOLD = 0.02;
const LEVEL_SMOOTHING = 0.75;
const SPEECH_THRESHOLD = 0.03;
// Voice→speaker→robot-mic→back is ~300-500ms on the LAN; the release must outlast it plus reverb.
const DUCK_RELEASE_MS = 900;
// Low enough to bury the AEC's residual echo, high enough that a shout or an interruption registers.
const DUCK_VOLUME = 0.12;

/**
 * @param {HTMLElement} parent
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {HTMLAudioElement} audioEl
 * @returns {{ destroy: () => void }}
 */
export function createTalkControl(parent, session, audioEl) {
  const mount = document.createElement("div");
  mount.className = "talk-control";
  parent.appendChild(mount);

  let isReceiving = false;
  let alwaysListen = false;
  let unmuted = false;
  let ducked = false;
  let lastVoiceAt = -Infinity;
  let receiveDeadline = 0;
  let receiveTimer = 0;

  const control = createMicControl(mount, {
    mode: "toggle",
    startListening: startTransmitting,
    stopListening: stopTransmitting,
    holdLabel: "Toggle your microphone",
    listeningLabel: "Unmuted — you are audible",
    buttonLabel: "MIC MUTED",
    buttonHint: "Unmute to talk",
    activeButtonLabel: "MIC LIVE",
    activeButtonHint: "Room ducks while you speak",
  });

  /** @type {ReturnType<typeof createAudioToggle>} */
  let listenToggle;
  listenToggle = createAudioToggle(mount, session, audioEl, {
    onToggle: toggleAlwaysListen,
    ariaLabel: "Always listen",
    activeTitle: "Listening — click to stop",
    inactiveTitle: "Always Listen off — click to keep the room mic open",
    icon: "ear",
  });
  listenToggle.setActive(false);

  /** @type {AudioContext | null} */ let ctx = null;
  /** @type {AnalyserNode | null} */ let analyser = null;
  /** @type {MediaStreamAudioSourceNode | null} */ let source = null;
  /** @type {MediaStream | null} */ let metered = null;
  /** @type {"transmit" | "receive" | null} */ let meterMode = null;
  let smoothedLevel = 0;
  let frame = 0;

  /** @param {MediaStream} stream @param {"transmit" | "receive"} mode */
  function meter(stream, mode) {
    if (metered !== stream || meterMode !== mode) {
      release();
      ctx = new AudioContext();
      void ctx.resume().catch(() => {});
      analyser = ctx.createAnalyser();
      analyser.fftSize = FFT_SIZE;
      source = ctx.createMediaStreamSource(stream);
      // Analyser only, never to ctx.destination — routing the mic to these
      // speakers is a feedback loop through the robot's.
      source.connect(analyser);
      metered = stream;
      meterMode = mode;
    }
    if (frame === 0) frame = requestAnimationFrame(tick);
  }

  function tick() {
    frame = 0;
    if (!analyser) return;
    const samples = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(samples);
    const level = rms(samples);
    control.setAudioFeedback({ level, waveform: buckets(samples) });
    smoothedLevel = smoothedLevel * LEVEL_SMOOTHING + level * (1 - LEVEL_SMOOTHING);
    if (meterMode === "transmit") {
      // Raw level, not smoothed: the duck must attack on the first frame of voice.
      const now = performance.now();
      if (level >= SPEECH_THRESHOLD) lastVoiceAt = now;
      setDucked(now - lastVoiceAt < DUCK_RELEASE_MS);
    }
    if (
      meterMode === "receive" &&
      !receiveHeld() &&
      isReceiving &&
      smoothedLevel >= REMOTE_ACTIVITY_THRESHOLD
    ) {
      receiveDeadline = performance.now() + RECEIVE_QUIET_MS;
    }
    frame = requestAnimationFrame(tick);
  }

  /** @param {boolean} d */
  function setDucked(d) {
    if (ducked === d) return;
    ducked = d;
    audioEl.volume = d ? DUCK_VOLUME : 1;
  }

  function receiveHeld() {
    return alwaysListen || unmuted;
  }

  function stopMetering() {
    if (frame !== 0) cancelAnimationFrame(frame);
    frame = 0;
    control.setAudioFeedback({ level: 0, waveform: Array(WAVEFORM_POINT_COUNT).fill(0) });
  }

  function release() {
    stopMetering();
    source?.disconnect();
    source = null;
    analyser = null;
    void ctx?.close();
    ctx = null;
    metered = null;
    meterMode = null;
    smoothedLevel = 0;
  }

  function scheduleReceiveClose() {
    window.clearTimeout(receiveTimer);
    const remaining = Math.max(0, receiveDeadline - performance.now());
    receiveTimer = window.setTimeout(() => {
      receiveTimer = 0;
      if (receiveHeld() || !isReceiving) return;
      if (performance.now() < receiveDeadline) {
        scheduleReceiveClose();
        return;
      }
      stopReceiving();
    }, remaining);
  }

  function renderReceiveState() {
    listenToggle.setActive(isReceiving);
    control.setReceiveState({
      on: isReceiving,
      label: "LISTENING",
      hint: alwaysListen ? "Always listen" : "Closes after 3s quiet",
    });
    mount.classList.toggle("always-listen", isReceiving && alwaysListen);
  }

  function startReceiving() {
    isReceiving = true;
    receiveDeadline = performance.now() + RECEIVE_QUIET_MS;
    renderReceiveState();
    session.setAudio(true);
    void audioEl.play().catch(() => {});
    if (receiveHeld()) {
      window.clearTimeout(receiveTimer);
      receiveTimer = 0;
    } else {
      scheduleReceiveClose();
    }
  }

  function stopReceiving() {
    window.clearTimeout(receiveTimer);
    receiveTimer = 0;
    isReceiving = false;
    renderReceiveState();
    session.setAudio(false);
    audioEl.pause();
    if (meterMode === "receive") release();
  }

  /** @param {boolean} active */
  function toggleAlwaysListen(active) {
    alwaysListen = active;
    if (unmuted) return; // receive is already held open while unmuted
    if (active) startReceiving();
    else stopReceiving();
  }

  async function startTransmitting() {
    unmuted = true;
    listenToggle.setEnabled(false);
    startReceiving(); // inside the click gesture, so audioEl.play() unlocks audible autoplay
    await session.setTalk(true);
  }

  function stopTransmitting() {
    unmuted = false;
    listenToggle.setEnabled(true);
    setDucked(false);
    const didTransmit = session.state.talkRequested;
    void session.setTalk(false);
    if (didTransmit || alwaysListen) startReceiving(); // re-arms the 3s-quiet window
    else stopReceiving();
  }

  const unsub = session.onChange((state) => {
    listenToggle.setEnabled(!state.talkRequested && !unmuted);
    control.setCaptureState({ on: state.talkRequested, busy: false, error: state.talkError });
    if (!state.talkRequested) setDucked(false);
    if (state.talkRequested && state.talkStream) {
      meter(state.talkStream, "transmit");
      return;
    }
    if (isReceiving && state.audioStream) {
      meter(state.audioStream, "receive");
      return;
    }
    stopMetering();
  });

  return {
    destroy() {
      unsub();
      control.destroy();
      stopReceiving();
      release();
      listenToggle.destroy();
      mount.remove();
    },
  };
}

/** @param {Uint8Array} samples 8-bit PCM centered on 128 @returns {number} 0..1 */
function rms(samples) {
  let sum = 0;
  for (const s of samples) sum += (s - 128) * (s - 128);
  return Math.sqrt(sum / samples.length) / 128;
}

/** @param {Uint8Array} samples @returns {number[]} */
function buckets(samples) {
  const width = samples.length / WAVEFORM_POINT_COUNT;
  return Array.from({ length: WAVEFORM_POINT_COUNT }, (_, i) =>
    rms(samples.subarray(Math.floor(i * width), Math.floor((i + 1) * width))),
  );
}
