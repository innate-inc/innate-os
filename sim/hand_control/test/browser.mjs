// Exercises the real browser tracker with the operator's locally saved videos.
// Test-only camera injection: no production hooks, no physical webcam access.
import { chromium, expect } from "@playwright/test";
import { readFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const recordings = path.resolve(root, "../../benchmarks/hand_tracking/data");
const manifest = JSON.parse(
  await readFile(path.join(recordings, "manifest.json"), "utf8"),
);
const clips = Object.fromEntries(
  manifest.clips.map((c) => [c.clip, path.join(recordings, c.video)]),
);
await mkdir(path.join(root, "artifacts"), { recursive: true });
const browser = await chromium.launch({
  executablePath:
    process.env.CHROME_PATH ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
page.on("console", (msg) => {
  if (msg.type() === "error") console.log("Browser:", msg.text());
});
await page.route("**/test-media/*.webm", async (route) => {
  const name = new URL(route.request().url()).pathname
    .split("/")
    .at(-1)
    .replace(".webm", "");
  await route.fulfill({ path: clips[name], contentType: "video/webm" });
});
await page.addInitScript(() => {
  window.__connectionEvents = [];
  document.addEventListener("visibilitychange", () =>
    window.__connectionEvents.push({ visibility: document.visibilityState }),
  );
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = class extends NativeWebSocket {
    constructor(...args) {
      super(...args);
      window.__controlSocket = this;
      this.addEventListener("close", (event) =>
        window.__connectionEvents.push({
          closed: event.code,
          reason: event.reason,
        }),
      );
      this.addEventListener("message", (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "state") window.__simState = data;
          if (data.type === "error") window.__connectionEvents.push(data);
        } catch {
          /* other packets */
        }
      });
    }
  };
  const NativeWorker = window.Worker;
  window.Worker = class extends NativeWorker {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", ({ data }) => {
        if (data.type === "result" && data.result.landmarks?.length === 1)
          window.__handPoints = data.result.landmarks[0];
      });
    }
  };
  window.__cameraShift = 0;
  navigator.mediaDevices.getUserMedia = async () => {
    const source = document.createElement("video");
    source.src = "/test-media/hold.webm";
    source.muted = true;
    source.loop = true;
    source.playsInline = true;
    await source.play();
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 480;
    const context = canvas.getContext("2d");
    const draw = () => {
      if (source.readyState >= 2) {
        context.fillStyle = "#334438";
        context.fillRect(0, 0, 640, 480);
        context.drawImage(source, 0, window.__cameraShift, 640, 480);
      }
      requestAnimationFrame(draw);
    };
    draw();
    window.__switchVideo = async (name) => {
      source.src = `/test-media/${name}.webm`;
      await source.play();
    };
    return canvas.captureStream(30);
  };
});
try {
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8840/");
  await expect(page.locator("#primary")).toBeEnabled({ timeout: 20000 });
  await page.locator("#primary").click();
  await page.waitForFunction(() => !!window.__handPoints, { timeout: 30000 });
  // Shift the recorded video downward to perform the new bottom-of-frame
  // calibration with real model inference, without changing the saved footage.
  await page.evaluate(() => {
    const lowest = Math.max(
      ...[0, 4, 5, 8, 9, 13, 17].map((i) => window.__handPoints[i].y),
    );
    window.__cameraShift = (0.94 - lowest) * 480;
  });
  await expect(page.locator("#primary")).toHaveText("Start following", {
    timeout: 35000,
  });
  console.log("Calibration passed with real MediaPipe inference.");
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 5000,
  });
  await page.evaluate(() => (window.__cameraShift = 0));
  await page.waitForTimeout(1200);
  const initial = await page.evaluate(() => window.__simState.ee);
  await page.evaluate(() => window.__switchVideo("left"));
  await expect
    .poll(() => page.evaluate(() => window.__simState.ee[1]), { timeout: 6500 })
    .toBeLessThan(initial[1] - 0.045);
  console.log("Recorded left motion moved the simulated end effector left.");
  await page.screenshot({
    path: path.join(root, "artifacts/desktop-following.png"),
    fullPage: true,
  });
  await page.evaluate(() => window.__switchVideo("empty"));
  await expect(page.locator("#stage-title")).toHaveText(
    "Holding your position",
    { timeout: 3000 },
  );
  await expect
    .poll(() => page.evaluate(() => window.__simState.active))
    .toBe(false);
  const held = await page.evaluate(() => window.__simState.ee);
  await page.waitForTimeout(700);
  const after = await page.evaluate(() => window.__simState.ee);
  expect(Math.hypot(...after.map((x, i) => x - held[i]))).toBeLessThan(0.01);
  console.log("Lost hand pauses physics targets.");
  await page.evaluate(() => window.__switchVideo("hold"));
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 6500,
  });
  const beforeDepth = await page.evaluate(() => window.__simState.ee);
  await page.evaluate(() => window.__switchVideo("toward"));
  await expect
    .poll(() => page.evaluate(() => window.__simState.ee[0]), { timeout: 7500 })
    .toBeGreaterThan(beforeDepth[0] + 0.02);
  console.log("Recorded approach reached the simulated end effector forward.");
  await page.keyboard.press("Escape");
  await expect
    .poll(() => page.evaluate(() => window.__simState.active))
    .toBe(false);
  await expect(page.locator("#primary")).toHaveText("Start following");
  console.log("Escape stops control.");
  await page.evaluate(() => window.__switchVideo("hold"));
  await page.evaluate(() => {
    const lowest = Math.max(
      ...[0, 4, 5, 8, 9, 13, 17].map((i) => window.__handPoints[i].y),
    );
    window.__cameraShift = (0.94 - lowest) * 480;
  });
  await page.keyboard.press("r");
  await expect(page.locator("#primary")).toHaveText("Calibrating…");
  await expect(page.locator("#primary")).toHaveText("Start following", {
    timeout: 6000,
  });
  await page.locator("#primary").click();
  await expect
    .poll(() => page.evaluate(() => window.__simState.active))
    .toBe(true);
  await page.evaluate(() => window.__controlSocket.close());
  await expect(page.locator("#primary")).toHaveText("Start following");
  await expect(page.locator("#connection")).toHaveText("MARS connected", {
    timeout: 6000,
  });
  await expect
    .poll(() => page.evaluate(() => window.__simState.active))
    .toBe(false);
  console.log(
    "Recenter shortcut and reconnect require a fresh explicit start.",
  );
  await page.locator("#camera-off").click();
  await expect(page.locator("#primary")).toHaveText("Enable camera");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(400);
  await page.screenshot({
    path: path.join(root, "artifacts/mobile.png"),
    fullPage: true,
  });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    390,
  );
  expect(errors).toEqual([]);
  console.log("Camera-off cleanup and responsive layout passed.");
  const denied = await browser.newPage();
  await denied.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException("Declined in test", "NotAllowedError");
    };
  });
  await denied.goto(process.env.STUDIO_URL || "http://127.0.0.1:8840/");
  await expect(denied.locator("#primary")).toBeEnabled({ timeout: 15000 });
  await denied.locator("#primary").click();
  await expect(denied.locator("#error")).toContainText(
    "Camera permission was declined",
  );
  await expect(denied.locator("#primary")).toBeEnabled();
  await expect(denied.locator("#primary")).toHaveText("Enable camera");
  await denied.close();
  console.log("Camera permission denial has an actionable retry state.");
} catch (error) {
  await page.screenshot({
    path: path.join(root, "artifacts/browser-failure.png"),
    fullPage: true,
  });
  console.log(
    await page.locator("#error").textContent(),
    await page.locator("#feedback").textContent(),
    errors,
    await page.evaluate(() => ({
      events: window.__connectionEvents,
      state: window.__simState,
    })),
  );
  throw error;
} finally {
  await browser.close();
}
