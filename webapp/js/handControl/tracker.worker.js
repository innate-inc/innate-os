// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Hand tracking off the UI thread. A *classic* worker on purpose: MediaPipe's
// WASM loader reaches for importScripts when it finds itself in a worker, and a
// module worker has neither that nor a document to append a <script> to, so it
// throws on load. importScripts also wants a script that assigns to a global
// `exports`, which is what the vendored CommonJS bundle does once we give it one.
//
// Excluded from tsconfig: this file runs in the worker global scope
// (importScripts, self.onmessage), which lib.dom does not declare.

const VENDOR = "/public/vendor/mediapipe-0.10.32/";

self.exports = {};
importScripts(`${VENDOR}vision_bundle.js`);
const { FilesetResolver, HandLandmarker } = self.exports;

let tracker;

self.onmessage = async ({ data }) => {
  if (data.type === "init") {
    try {
      const files = await FilesetResolver.forVisionTasks(`${VENDOR}wasm`);
      tracker = await HandLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: `${VENDOR}hand_landmarker.task`, delegate: "CPU" },
        runningMode: "VIDEO",
        // One hand: re-searching the frame for a second hand costs inference
        // time that the control loop needs, and two hands have no meaning here.
        numHands: 1,
        minHandDetectionConfidence: 0.5,
        minHandPresenceConfidence: 0.5,
        minTrackingConfidence: 0.5,
      });
      self.postMessage({ type: "ready" });
    } catch (error) {
      self.postMessage({ type: "error", message: error.message });
    }
    return;
  }
  if (data.type !== "frame") return;
  try {
    const start = performance.now();
    const result = tracker.detectForVideo(data.bitmap, data.timestamp);
    self.postMessage({
      type: "result",
      result,
      generation: data.generation,
      timestamp: data.timestamp,
      inferenceMs: performance.now() - start,
    });
  } catch (error) {
    self.postMessage({ type: "error", message: error.message });
  } finally {
    data.bitmap.close();
  }
};
