// Browser recording/storage flow with a synthetic camera; never uses the webcam.
import { chromium, expect } from "@playwright/test";
import { readFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
const floor = process.env.STUDY_FOCUS === "floor";
const total = floor ? 7 : 16;
const firstId = floor ? "floor_reference" : "neutral";
const firstTitle = floor
  ? "Start with a level claw"
  : "A relaxed starting pose";
const secondTitle = floor ? "Tip the claw halfway down" : "Claw tipped up";
const browser = await chromium.launch({
  executablePath:
    process.env.CHROME_PATH ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.addInitScript(() => {
  const Socket = WebSocket;
  window.sentOps = [];
  window.testTracks = [];
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", ({ data }) => {
        const m = JSON.parse(data);
        if (m.type === "study_opened") window.opened = m;
        if (m.type === "study_saved") window.saved = m;
        if (m.type === "state") window.simState = m;
      });
    }
    send(data) {
      window.sentOps.push(JSON.parse(data).op);
      super.send(data);
    }
  };
  navigator.mediaDevices.getUserMedia = async () => {
    const c = document.createElement("canvas");
    c.width = 640;
    c.height = 480;
    const ctx = c.getContext("2d");
    const draw = () => {
      ctx.fillStyle = "#d5e8d4";
      ctx.fillRect(0, 0, 640, 480);
      ctx.fillStyle = "#416957";
      ctx.beginPath();
      ctx.arc(
        320 + Math.sin(performance.now() / 300) * 10,
        250,
        70,
        0,
        Math.PI * 2,
      );
      ctx.fill();
      requestAnimationFrame(draw);
    };
    draw();
    const stream = c.captureStream(30);
    window.testTracks.push(...stream.getTracks());
    return stream;
  };
  window.Worker = class {
    postMessage(data) {
      if (data.type === "init") {
        setTimeout(() => this.onmessage?.({ data: { type: "ready" } }), 0);
        return;
      }
      data.bitmap.close();
      const p = Array.from({ length: 21 }, () => ({ x: 0.5, y: 0.5, z: 0 }));
      p[0] = { x: 0.5, y: 0.75, z: 0 };
      p[4] = { x: 0.8, y: 0.5, z: 0 };
      for (const [i, x] of [
        [5, 0.62],
        [9, 0.54],
        [13, 0.46],
        [17, 0.38],
      ])
        for (let k = 0; k < 4; k++) p[i + k] = { x, y: 0.55 - k * 0.105, z: 0 };
      setTimeout(() => {
        if (!this.stopped)
          this.onmessage?.({
            data: {
              type: "result",
              timestamp: data.timestamp,
              capturedAt: data.capturedAt,
              inferenceMs: 15,
              result: {
                landmarks: [p],
                worldLandmarks: [p],
                handedness: [[{ categoryName: "Right" }]],
              },
            },
          });
      }, 15);
    }
    terminate() {
      this.stopped = true;
    }
  };
});
try {
  await mkdir("artifacts", { recursive: true });
  await page.goto(
    new URL(
      `match.html${floor ? "?focus=floor" : ""}`,
      process.env.STUDIO_URL || "http://127.0.0.1:8841/",
    ).href,
  );
  await expect(page.locator("#primary")).toHaveText("Enable camera & start");
  await expect(page.locator("#primary")).toBeEnabled();
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toHaveText("Record my gesture", {
    timeout: 15000,
  });
  await expect(page.locator("#pose-title")).toHaveText(firstTitle);
  await page.screenshot({ path: "artifacts/study-ready.png", fullPage: true });
  await page.locator("#primary").click();
  await expect(page.locator("#countdown")).toBeVisible();
  await expect(page.locator("#review")).toBeVisible({ timeout: 9000 });
  await expect(page.locator("#take-preview")).toBeVisible();
  expect(await page.evaluate(() => window.sentOps.includes("move"))).toBe(
    false,
  );
  await page
    .locator("#notes")
    .fill("Synthetic test of the local recording workflow.");
  await page.locator("#comfortable").click();
  await expect(page.locator("#saved-count")).toHaveText(`1 / ${total} saved`, {
    timeout: 10000,
  });
  const saved = await page.evaluate(() => window.saved),
    session = saved.session;
  const record = JSON.parse(await readFile(saved.path, "utf8"));
  expect(record.pose_id).toBe(firstId);
  expect(record.frames.length).toBeGreaterThanOrEqual(8);
  expect(
    record.frames.every((f) => f.video_time >= 0 && f.landmarks.length === 21),
  ).toBe(true);
  const video = await readFile(
    path.join(path.dirname(saved.path), record.video),
  );
  expect(createHash("sha256").update(video).digest("hex")).toBe(
    record.video_sha256,
  );
  await page.reload();
  await expect(page.locator("#pose-title")).toHaveText(secondTitle);
  await expect(page.locator("#saved-count")).toHaveText(`1 / ${total} saved`);
  expect(await page.evaluate(() => window.opened.session)).toBe(session);
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toHaveText("Record my gesture", {
    timeout: 15000,
  });
  await page.locator("#primary").click();
  await page.keyboard.press("Escape");
  await expect(page.locator("#primary")).toHaveText("Resume pose matching");
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toHaveText("Record my gesture", {
    timeout: 10000,
  });
  await page.locator("#primary").click();
  await expect(page.locator("#review")).toBeVisible({ timeout: 9000 });
  await page.locator("#awkward").click();
  await expect(page.locator("#saved-count")).toHaveText(`2 / ${total} saved`, {
    timeout: 10000,
  });
  for (let i = 2; i < total; i++) {
    await expect(page.locator("#skip")).toBeEnabled({ timeout: 15000 });
    await page.locator("#skip").click();
    await expect(page.locator("#saved-count")).toHaveText(
      `${i + 1} / ${total} saved`,
      { timeout: 10000 },
    );
  }
  await expect(page.locator("#complete")).toBeVisible();
  expect(
    await page.evaluate(() =>
      window.testTracks.every((t) => t.readyState === "ended"),
    ),
  ).toBe(true);
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  await page.reload();
  await expect(page.locator("#complete")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: "artifacts/study-mobile.png", fullPage: true });
  expect(errors).toEqual([]);
  console.log(
    `Pose study passes: real clip recording, landmark/pose storage and hash, refresh/resume, Escape, comfort labels, all ${total} steps, completion cleanup, mobile layout. No old hand-to-arm mapping used.`,
  );
} catch (e) {
  console.log(
    await page.locator("#error").textContent(),
    await page.locator("#feedback").textContent(),
  );
  await page.screenshot({
    path: "artifacts/study-failure.png",
    fullPage: true,
  });
  throw e;
} finally {
  await browser.close();
}
