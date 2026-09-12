// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// In Context Learning — the debug view for the demonstration-conditioned skills.
//
// These skills have no trained policy: a recorded episode's frames and EE
// trajectory ARE the model's context, and every move is a fresh decision made
// from that context plus the live cameras. Debugging one therefore means
// watching three things at once, which is what this page puts side by side:
// the demonstration being used (left), the robot right now (top), and what the
// model inferred and chose (right). Runs start and stop from the same bar, so
// the loop is edit → run → read the reasoning without leaving the page.
//
// Live views ride the shared WebRTC session (both cameras at once); the
// reasoning comes off /brain/icl_trace, published by innate/icl_trace.py.

import { WEBRTC_ACTIVE_STREAMS_TOPIC } from "../constants.js";
import { ros } from "../rosClient.js";
import { acquireVideoSession, releaseVideoSession } from "../sharedVideoSession.js";
import { createDemoPanel } from "./demoPanel.js";
import { createReasoningFeed } from "./reasoningFeed.js";
import { createRunControls } from "./runControls.js";
import { createTraceStream } from "./traceStream.js";

// The two views a demonstration run reasons over: the fixed head camera and the
// wrist camera that moves with the arm. Roster names, not display labels.
const VIEWS = [
  { name: "main", label: "Head" },
  { name: "arm", label: "Wrist" },
];

/** @param {HTMLElement} stage @returns {{ destroy: () => void }} */
export function mount(stage) {
  stage.innerHTML = "";
  const root = document.createElement("div");
  root.className = "icl-page";
  root.innerHTML = `
    <header class="icl-head">
      <h1>In Context Learning</h1>
      <p class="icl-sub microlabel">A recorded episode is the model's whole context. No trained policy, one decision per observation.</p>
    </header>
    <div class="icl-body">
      <div class="icl-left"></div>
      <div class="icl-mid">
        <section class="icl-panel icl-live">
          <header class="icl-panel-head"><h2>Live</h2><span class="icl-live-status microlabel"></span></header>
          <div class="icl-cams"></div>
        </section>
      </div>
      <div class="icl-right"></div>
    </div>`;
  stage.appendChild(root);

  const $ = (/** @type {string} */ s) => /** @type {HTMLElement} */ (root.querySelector(s));
  const cams = $(".icl-cams");
  const liveStatus = $(".icl-live-status");

  // ---- live cameras --------------------------------------------------------
  const session = acquireVideoSession();
  /** @type {Map<string, HTMLVideoElement>} */ const videos = new Map();
  for (const view of VIEWS) {
    const figure = document.createElement("figure");
    figure.className = "icl-cam";
    const video = document.createElement("video");
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    const cap = document.createElement("figcaption");
    cap.className = "microlabel";
    cap.textContent = view.label;
    figure.append(video, cap);
    cams.appendChild(figure);
    videos.set(view.name, video);
  }

  /** @type {string[]} */ let roster = [];
  function attachStreams() {
    const state = session.state;
    let live = 0;
    for (const view of VIEWS) {
      const video = videos.get(view.name);
      const index = roster.indexOf(view.name);
      const stream = index < 0 ? null : (state.videoStreams?.[index] ?? null);
      if (!video) continue;
      if (stream && video.srcObject !== stream) video.srcObject = stream;
      if (!stream && video.srcObject) video.srcObject = null;
      if (stream && state.videoLive?.[index]) live += 1;
    }
    liveStatus.textContent = roster.length === 0 ? "waiting for the camera roster" : `${live}/${VIEWS.length} streaming`;
    liveStatus.classList.toggle("is-bad", roster.length > 0 && live === 0);
  }

  const unsubRoster = ros.subscribe(
    WEBRTC_ACTIVE_STREAMS_TOPIC,
    (msg) => {
      const raw = msg?.data ?? msg?.msg?.data;
      if (typeof raw !== "string") return;
      /** @type {string[]} */ let next;
      try {
        next = JSON.parse(raw).cameras ?? [];
      } catch {
        return;
      }
      if (next.length === roster.length && next.every((c, i) => c === roster[i])) return;
      roster = next;
      // Ask the robot for exactly the two views a demonstration run reasons over.
      session.setActiveCameras(roster.filter((name) => VIEWS.some((v) => v.name === name)));
      attachStreams();
    },
    undefined,
    "std_msgs/msg/String",
  );
  const unsubSession = session.onChange(attachStreams);
  session.start();

  // ---- panels --------------------------------------------------------------
  const reasoning = createReasoningFeed($(".icl-right"));
  const demo = createDemoPanel($(".icl-left"), () => {});
  const trace = createTraceStream(ros, (run) => reasoning.render(run));
  const controls = createRunControls($(".icl-mid"), ros, {
    demonstration: () => demo.path(),
    onFeedback: (text) => reasoning.note(text),
  });
  reasoning.render(trace.run());

  return {
    destroy: () => {
      controls.destroy();
      trace.destroy();
      demo.destroy();
      reasoning.destroy();
      unsubRoster();
      unsubSession();
      for (const video of videos.values()) video.srcObject = null;
      // Hand the session back the way every other page leaves it, so the next
      // page doesn't inherit this page's two-camera set.
      session.showMainCamera();
      releaseVideoSession();
      stage.innerHTML = "";
    },
  };
}
