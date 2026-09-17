import { FilesetResolver, HandLandmarker } from "@mediapipe/tasks-vision";

let tracker;
self.onmessage = async ({ data }) => {
  if (data.type === "init") {
    try {
      const files = await FilesetResolver.forVisionTasks(`${data.base}wasm`);
      tracker = await HandLandmarker.createFromOptions(files, {
        baseOptions: {
          modelAssetPath: `${data.base}hand_landmarker.task`,
          delegate: "CPU",
        },
        runningMode: "VIDEO",
        numHands: 1,
        minHandDetectionConfidence: 0.5,
        minHandPresenceConfidence: 0.5,
        minTrackingConfidence: 0.5,
      });
      self.postMessage({ type: "ready" });
    } catch (error) {
      self.postMessage({ type: "error", message: error.message });
    }
  }
  if (data.type === "frame") {
    try {
      const start = performance.now();
      const result = tracker.detectForVideo(data.bitmap, data.timestamp);
      self.postMessage({
        type: "result",
        result,
        generation: data.generation,
        timestamp: data.timestamp,
        capturedAt: data.capturedAt,
        inferenceMs: performance.now() - start,
      });
    } catch (error) {
      self.postMessage({ type: "error", message: error.message });
    } finally {
      data.bitmap.close();
    }
  }
};
