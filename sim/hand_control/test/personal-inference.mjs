// Real MediaPipe on the user's accepted videos, using a test-only canvas camera.
import { chromium, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
const directory = path.resolve(process.argv[2]);
const manifest = JSON.parse(
  fs.readFileSync(path.join(directory, "manifest.json")),
);
const videos = Object.fromEntries(
  ["neutral", "pitch_up", "pitch_down"].map((id) => [
    id,
    JSON.parse(
      fs.readFileSync(path.join(directory, manifest.matches[id].record)),
    ).video,
  ]),
);
const browser = await chromium.launch({
  executablePath:
    process.env.CHROME_PATH ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.route("**/test-recordings/*", (route) =>
    route.fulfill({
      path: path.join(
        directory,
        videos[route.request().url().split("/").at(-1)],
      ),
      contentType: "video/webm",
    }),
  );
  await page.addInitScript(() => {
    window.clip = "neutral";
    window.events = [];
    window.states = [];
    window.changed = 0;
    const Socket = WebSocket;
    window.WebSocket = class extends Socket {
      constructor(...args) {
        super(...args);
        this.addEventListener("message", (e) => {
          const m = JSON.parse(e.data);
          if (m.type === "state") {
            window.state = m;
            window.states.push({ ...m, received: performance.now() });
            if (window.states.length > 400) window.states.shift();
          }
          if (m.type === "error") window.events.push(m);
        });
      }
    };
    navigator.mediaDevices.getUserMedia = async () => {
      const videos = {};
      for (const id of ["neutral", "pitch_up", "pitch_down"]) {
        const v = document.createElement("video");
        v.src = "/test-recordings/" + id;
        v.loop = true;
        v.muted = true;
        await v.play();
        videos[id] = v;
      }
      const c = document.createElement("canvas");
      c.width = 640;
      c.height = 480;
      const ctx = c.getContext("2d");
      const draw = () => {
        ctx.drawImage(videos[window.clip], 0, 0, 640, 480);
        requestAnimationFrame(draw);
      };
      draw();
      return c.captureStream(30);
    };
  });
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8841/");
  await expect(page.locator("#primary")).toBeEnabled({ timeout: 20000 });
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toContainText("Start following", {
    timeout: 30000,
  });
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 10000,
  });
  const results = [];
  for (const [clip, expected] of [
    ["pitch_up", -0.5],
    ["neutral", 0],
    ["pitch_down", 0.5],
    ["neutral", 0],
  ]) {
    await page.evaluate((clip) => {
      window.clip = clip;
      window.changed = performance.now();
    }, clip);
    await expect
      .poll(
        () =>
          page.evaluate((expected) => {
            const recent = window.states
              .filter((s) => s.received > window.changed + 1500)
              .slice(-30);
            if (recent.length < 30) return 10;
            const mean =
              recent.reduce((a, s) => a + s.wrist_measured[1], 0) /
              recent.length;
            return Math.abs(mean - expected);
          }, expected),
        { timeout: 25000 },
      )
      .toBeLessThan(clip === "neutral" ? 0.1 : 0.18);
    results.push(
      await page.evaluate(() => ({
        clip: window.clip,
        pitch:
          window.states
            .slice(-30)
            .reduce((a, s) => a + s.wrist_measured[1], 0) / 30,
        target: window.state.target,
        command: window.state.wrist,
        grip: window.state.grip,
      })),
    );
  }
  expect(await page.evaluate(() => window.events)).toEqual([]);
  expect(errors).toEqual([]);
  console.log(JSON.stringify({ real_video_and_mediapipe: "passed", results }));
} catch (e) {
  console.log(
    await page.locator("#feedback").textContent(),
    await page.locator("#error").textContent(),
    await page.evaluate(() => window.state),
  );
  throw e;
} finally {
  await browser.close();
}
