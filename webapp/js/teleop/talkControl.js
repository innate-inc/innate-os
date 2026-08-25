// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Teleop push-to-talk. Hold the button (or the spacebar) and this browser's
// microphone plays live out the robot's speaker, next to the bar that types
// speech for it. The session owns the transport — a track swap on the audio
// m-line it already negotiated — so this module owns only the button and the
// level meter, which is the operator's one confirmation that a room they may
// not be able to see is hearing them.

import { createMicControl } from "../micControl.js";

const WAVEFORM_POINT_COUNT = 9;
const FFT_SIZE = 256;

/**
 * @param {HTMLElement} parent
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ destroy: () => void }}
 */
export function createTalkControl(parent, session) {
  const mount = document.createElement("div");
  mount.className = "talk-control";
  parent.appendChild(mount);

  const control = createMicControl(mount, {
    startListening: () => session.setTalk(true),
    stopListening: () => void session.setTalk(false),
    holdLabel: "Hold to speak through the robot",
    // Deliberately louder than the agent page's "Listening…": this one is heard
    // by whoever is standing next to the robot, not just by the agent.
    listeningLabel: "Live — you are audible",
  });

  /** @type {AudioContext | null} */ let ctx = null;
  /** @type {AnalyserNode | null} */ let analyser = null;
  /** @type {MediaStreamAudioSourceNode | null} */ let source = null;
  /** @type {MediaStream | null} */ let metered = null;
  let frame = 0;

  /** @param {MediaStream} stream */
  function meter(stream) {
    if (metered !== stream) {
      release();
      ctx = new AudioContext();
      analyser = ctx.createAnalyser();
      analyser.fftSize = FFT_SIZE;
      source = ctx.createMediaStreamSource(stream);
      // Analyser only, never to ctx.destination — routing the mic to these
      // speakers is a feedback loop through the robot's.
      source.connect(analyser);
      metered = stream;
    }
    if (frame === 0) frame = requestAnimationFrame(tick);
  }

  function tick() {
    frame = 0;
    if (!analyser) return;
    const samples = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(samples);
    control.setAudioFeedback({ level: rms(samples), waveform: buckets(samples) });
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
  }

  const unsub = session.onChange((state) => {
    control.setCaptureState({ on: state.talkRequested, busy: false, error: state.talkError });
    if (state.talkRequested && state.talkStream) meter(state.talkStream);
    else stopMetering();
  });

  return {
    destroy() {
      unsub();
      release();
      control.destroy();
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
