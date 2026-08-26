// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Teleop half-duplex audio: hold to transmit, then listen until three seconds
// of room silence. A new hold always preempts listening.

import { createMicControl } from "../micControl.js";
import { createAudioToggle } from "./videoStage.js";

const WAVEFORM_POINT_COUNT = 9;
const FFT_SIZE = 256;
const RECEIVE_QUIET_MS = 3_000;
const REMOTE_ACTIVITY_THRESHOLD = 0.02;
const LEVEL_SMOOTHING = 0.75;

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
  let receiveDeadline = 0;
  let receiveTimer = 0;

  const control = createMicControl(mount, {
    startListening: startTransmitting,
    stopListening: stopTransmitting,
    holdLabel: "Hold to transmit in two-way talk",
    listeningLabel: "Transmitting — you are audible",
    buttonLabel: "2-WAY TALK",
    buttonHint: "Hold to transmit",
    activeButtonLabel: "TRANSMITTING",
    activeButtonHint: "Release to listen",
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
    if (
      meterMode === "receive" &&
      !alwaysListen &&
      isReceiving &&
      smoothedLevel >= REMOTE_ACTIVITY_THRESHOLD
    ) {
      receiveDeadline = performance.now() + RECEIVE_QUIET_MS;
    }
    frame = requestAnimationFrame(tick);
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
      if (alwaysListen || !isReceiving) return;
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
    if (alwaysListen) {
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
    if (session.state.talkRequested) return;
    if (active) startReceiving();
    else stopReceiving();
  }

  async function startTransmitting() {
    stopReceiving();
    listenToggle.setEnabled(false);
    await session.setTalk(true);
  }

  function stopTransmitting() {
    listenToggle.setEnabled(true);
    const didTransmit = session.state.talkRequested;
    void session.setTalk(false);
    if (didTransmit || alwaysListen) startReceiving();
  }

  const unsub = session.onChange((state) => {
    listenToggle.setEnabled(!state.talkRequested);
    if (state.talkRequested) listenToggle.setActive(false);
    control.setCaptureState({ on: state.talkRequested, busy: false, error: state.talkError });
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
