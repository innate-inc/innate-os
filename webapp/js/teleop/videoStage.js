// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Video stage — the full-bleed <video> plus a hidden <audio> for the robot
// mic, with quiet connecting/error states layered on top. Also exports the
// audio toggle, which must flip the track and call audio.play() inside the
// click gesture to satisfy autoplay policy.

/**
 * @param {HTMLElement} parent
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ el: HTMLElement, videoEl: HTMLVideoElement, audioEl: HTMLAudioElement, destroy: () => void }}
 */
export function createVideoStage(parent, session) {
  const wrap = document.createElement("div");
  wrap.className = "video-stage";

  const video = document.createElement("video");
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;

  const audio = document.createElement("audio");
  audio.hidden = true;

  const status = document.createElement("div");
  status.className = "video-status";
  const statusText = document.createElement("p");
  statusText.className = "microlabel video-status-text";
  const retry = document.createElement("button");
  retry.className = "video-retry";
  retry.type = "button";
  retry.textContent = "Retry video";
  retry.title = "Tear down and rebuild the WebRTC video link";
  retry.hidden = true;
  status.append(statusText, retry);

  wrap.append(video, audio, status);
  parent.appendChild(wrap);

  // A freshly attached MediaStream paints its first decoded frame and then
  // holds it for a beat (jitter-buffer / playout warmup) before smooth playback
  // — a multi-second frozen frame on a cold start. Cover that with a "loading
  // video stream" state until frames are actually flowing. Only the FIRST
  // bring-up: later rebuilds deliberately keep the previous frame as a
  // freeze-frame (nicer than blanking back to a loading screen).
  let latest = session.state;
  let buffering = false;
  let initialLoadComplete = false;
  let currentVideoReady = false;
  let frameCbId = 0;
  /** @type {number | null} */ let bufferTimer = null;

  const stopFrameWatch = () => {
    if (frameCbId && "cancelVideoFrameCallback" in video) {
      // @ts-ignore — rVFC not in older TS dom lib
      video.cancelVideoFrameCallback(frameCbId);
    }
    frameCbId = 0;
    if (bufferTimer !== null) {
      clearTimeout(bufferTimer);
      bufferTimer = null;
    }
  };

  // Give up waiting and reveal whatever's there — safety net so we never sit on
  // "loading" forever (non-Chromium without rVFC, or a genuinely dead stream;
  // the session's own watchdog handles real failures by erroring/rebuilding).
  const revealReady = () => {
    buffering = false;
    initialLoadComplete = true;
    stopFrameWatch();
    render();
  };

  // Watch a newly attached stream until real frames flow, then reveal it.
  const watchFirstFrames = () => {
    buffering = true;
    stopFrameWatch();
    bufferTimer = setTimeout(revealReady, 5_000);
    if ("requestVideoFrameCallback" in video) {
      let seen = 0;
      const tick = () => {
        // A held/frozen frame is presented once; several distinct presentations
        // mean playback is genuinely advancing.
        if (++seen >= 3) {
          buffering = false;
          initialLoadComplete = true;
          currentVideoReady = true;
          stopFrameWatch();
          render();
          return;
        }
        // @ts-ignore — rVFC not in older TS dom lib
        frameCbId = video.requestVideoFrameCallback(tick);
      };
      // @ts-ignore — rVFC not in older TS dom lib
      frameCbId = video.requestVideoFrameCallback(tick);
    }
  };

  const render = () => {
    const state = latest;
    // Cold-start "loading" only — never over a rebuild's freeze-frame.
    const loading = buffering && !initialLoadComplete;
    const showStatus = (state.status !== "streaming" && !state.videoStream) || loading;
    status.hidden = !showStatus;
    wrap.classList.toggle("buffering", loading);
    wrap.classList.toggle("video-ready", currentVideoReady);
    wrap.classList.toggle("degraded", state.status === "connecting" && !!state.videoStream);
    if (state.status === "error") {
      statusText.textContent = "video link failed";
      retry.hidden = false;
    } else if (loading) {
      statusText.textContent = "loading video stream";
      retry.hidden = true;
    } else {
      statusText.textContent = state.status === "connecting" ? "establishing video link" : "video idle";
      retry.hidden = true;
    }
    status.classList.toggle("waiting", state.status === "connecting" || loading);
  };

  // An explicit Retry is a fresh cold start (unlike an automatic reconnect,
  // which keeps the freeze-frame): reset the guard and cover whatever's on
  // screen with "loading video stream" until the rebuilt stream actually flows.
  retry.addEventListener("click", () => {
    initialLoadComplete = false;
    currentVideoReady = false;
    buffering = true;
    stopFrameWatch();
    bufferTimer = setTimeout(revealReady, 5_000);
    render();
    session.start();
  });

  const unsub = session.onChange((state) => {
    latest = state;
    // Keep the previous frame during rebuilds: only swap srcObject when a
    // new stream actually arrives, never clear it mid-handshake.
    if (state.videoStream && video.srcObject !== state.videoStream) {
      currentVideoReady = false;
      video.srcObject = state.videoStream;
      // autoplay alone can leave a swapped-in stream paused on its first
      // frame (Chromium); muted play() is always allowed, so be explicit.
      video.play().catch(() => {});
      watchFirstFrames();
    }
    if (!state.videoStream && state.status === "idle") {
      video.srcObject = null;
      buffering = false;
      currentVideoReady = false;
      stopFrameWatch();
    }

    if (state.audioStream) {
      if (audio.srcObject !== state.audioStream) {
        audio.srcObject = state.audioStream;
        if (state.audioRequested) {
          // Allowed: the operator clicked the toggle earlier this page-load,
          // which grants audible autoplay in Chromium/Firefox. Safari may
          // still refuse; the next toggle click plays inside its gesture.
          audio.play().catch((err) => console.warn("[video] audio play blocked:", err));
        }
      }
    } else {
      audio.srcObject = null;
    }

    render();
  });

  return {
    el: wrap,
    videoEl: video,
    audioEl: audio,
    destroy() {
      unsub();
      stopFrameWatch();
      video.srcObject = null;
      audio.srcObject = null;
      wrap.remove();
    },
  };
}

/**
 * Robot-mic toggle. Starts muted; the click both requests the audio m-line
 * (debounced rebuild in the session) and primes playback inside the gesture.
 * @param {HTMLElement} parent
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {HTMLAudioElement | null} audioEl
 * @returns {{ destroy: () => void }}
 */
export function createAudioToggle(parent, session, audioEl) {
  const button = document.createElement("button");
  button.className = "icon-toggle audio-toggle";
  button.type = "button";
  button.innerHTML =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M11 5 6.5 9H3.5v6h3L11 19z"/>' +
    '<path class="wave wave1" d="M15 9.5a4 4 0 0 1 0 5"/>' +
    '<path class="wave wave2" d="M17.5 7a8 8 0 0 1 0 10"/>' +
    "</svg>";

  const unsub = session.onChange((state) => {
    button.classList.toggle("active", state.audioRequested);
    button.title = state.audioRequested ? "Robot mic on — click to mute" : "Robot mic off — click to listen";
    button.setAttribute("aria-pressed", String(state.audioRequested));
    button.setAttribute("aria-label", "Robot microphone");
  });

  button.addEventListener("click", () => {
    const next = !session.state.audioRequested;
    session.setAudio(next);
    if (next && audioEl) {
      // Inside the gesture: unlocks audible playback for this page session.
      audioEl.play().catch(() => {
        // No stream yet — fine, the session onChange handler replays once
        // the rebuilt connection delivers the mic track.
      });
    } else if (audioEl) {
      audioEl.pause();
    }
  });

  parent.appendChild(button);
  return {
    destroy() {
      unsub();
      button.remove();
    },
  };
}
