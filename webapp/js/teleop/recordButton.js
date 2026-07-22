// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Record toggle — right-rail button that starts/stops the on-robot MP4
// recorder (video_recorder node). The lit state follows the latched
// /video_recorder/status Bool so a reloaded page shows what the robot is
// actually doing; files land in the robot's data/recordings/ folder.

import {
  VIDEO_RECORD_START_SERVICE,
  VIDEO_RECORD_STOP_SERVICE,
  VIDEO_RECORD_STATUS_TOPIC,
} from "../constants.js";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createRecordButton(parent, rosClient) {
  const button = document.createElement("button");
  button.className = "icon-toggle record-toggle";
  button.type = "button";
  button.innerHTML =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="5.5"/>' +
    "</svg>";
  button.setAttribute("aria-label", "Record camera video");

  let recording = false;
  let busy = false;

  function render() {
    button.classList.toggle("active", recording);
    button.disabled = busy;
    button.title = recording
      ? "Recording cameras — click to stop"
      : "Record cameras to MP4 on the robot";
    button.setAttribute("aria-pressed", String(recording));
  }
  render();

  const unsub = rosClient.subscribe(VIDEO_RECORD_STATUS_TOPIC, (msg) => {
    recording = Boolean(msg?.data);
    render();
  });

  button.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    render();
    const starting = !recording;
    try {
      const res = await rosClient.callService(starting ? VIDEO_RECORD_START_SERVICE : VIDEO_RECORD_STOP_SERVICE);
      if (!res?.success) console.warn("video recorder:", res?.message);
      // A Trigger failure only means "already in that state", so either way
      // the robot has now converged on what we asked for.
      recording = starting;
    } catch (err) {
      console.warn("video recorder call failed:", err);
    }
    busy = false;
    render();
  });

  parent.appendChild(button);
  return {
    destroy() {
      unsub();
      button.remove();
    },
  };
}
