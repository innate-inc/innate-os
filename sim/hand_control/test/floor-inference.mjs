// Re-detect the accepted videos with the actual browser worker, without arming.
// The output can be passed to floor-browser.mjs using INFERRED_FRAMES_FILE.
import fs from "node:fs";
import path from "node:path";
import { chromium, expect } from "@playwright/test";
import { personalSample, anglesFor, personalGrip } from "../src/personal.mjs";
const directory = path.resolve(process.argv[2]);
const profile = JSON.parse(fs.readFileSync(process.argv[3]));
const manifest = JSON.parse(
  fs.readFileSync(path.join(directory, "manifest.json")),
);
const clips = Object.fromEntries(
  Object.entries(manifest.matches).map(([id, entry]) => [
    id,
    JSON.parse(fs.readFileSync(path.join(directory, entry.record))).video,
  ]),
);
const browser = await chromium.launch({
  executablePath:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.route("**/floor-videos/*", (route) =>
    route.fulfill({
      path: path.join(
        directory,
        clips[route.request().url().split("/").at(-1)],
      ),
      contentType: "video/webm",
    }),
  );
  await page.addInitScript((ids) => {
    window.clip = "floor_reference";
    window.capturing = false;
    window.frames = Object.fromEntries(ids.map((id) => [id, []]));
    const WorkerBase = Worker;
    window.Worker = class extends WorkerBase {
      constructor(...args) {
        super(...args);
        this.addEventListener("message", ({ data }) => {
          if (
            window.capturing &&
            data.type === "result" &&
            data.result.landmarks?.length === 1
          )
            window.frames[window.clip].push({
              captured_at: data.capturedAt,
              video_time: performance.now() / 1000,
              inference_ms: data.inferenceMs,
              landmarks: data.result.landmarks[0],
              world_landmarks: data.result.worldLandmarks[0],
              handedness: data.result.handedness[0],
            });
        });
      }
    };
    navigator.mediaDevices.getUserMedia = async () => {
      const videos = {};
      for (const id of ids) {
        const v = document.createElement("video");
        v.src = "/floor-videos/" + id;
        v.loop = true;
        v.muted = true;
        await v.play();
        videos[id] = v;
      }
      const canvas = document.createElement("canvas");
      canvas.width = 640;
      canvas.height = 480;
      const ctx = canvas.getContext("2d");
      const draw = () => {
        ctx.drawImage(videos[window.clip], 0, 0, 640, 480);
        requestAnimationFrame(draw);
      };
      draw();
      return canvas.captureStream(30);
    };
  }, Object.keys(clips));
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8841/");
  await expect(page.locator("#primary")).toBeEnabled({ timeout: 20000 });
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toHaveText("Start following", {
    timeout: 30000,
  });
  for (const id of Object.keys(clips)) {
    await page.evaluate((id) => {
      window.capturing = false;
      window.clip = id;
    }, id);
    await page.waitForTimeout(600);
    await page.evaluate(() => {
      window.capturing = true;
    });
    await page.waitForTimeout(2300);
    await page.evaluate(() => {
      window.capturing = false;
    });
  }
  const captured = await page.evaluate(() => window.frames);
  const summaries = [];
  for (const [id, frames] of Object.entries(captured)) {
    const valid = frames.filter(
      (f) =>
        personalSample({
          landmarks: [f.landmarks],
          worldLandmarks: [f.world_landmarks],
          handedness: [f.handedness],
        }).valid,
    );
    expect(valid.length).toBeGreaterThan(12);
    captured[id] = valid;
    const samples = valid.map((f) =>
      personalSample({
        landmarks: [f.landmarks],
        worldLandmarks: [f.world_landmarks],
        handedness: [f.handedness],
      }),
    );
    const mean = (f) => samples.reduce((n, s) => n + f(s), 0) / samples.length;
    summaries.push({
      pose: id,
      frames: samples.length,
      pitch_degrees: (mean((s) => anglesFor(s, profile)[1]) * 180) / Math.PI,
      grip: mean((s) => personalGrip(s, profile)),
    });
  }
  expect(errors).toEqual([]);
  fs.writeFileSync(
    "artifacts/floor-mapping/inferred-frames.json",
    JSON.stringify(captured),
  );
  fs.writeFileSync(
    "artifacts/floor-mapping/inference-report.json",
    JSON.stringify(summaries, null, 2),
  );
  console.log(JSON.stringify(summaries));
} finally {
  await browser.close();
}
