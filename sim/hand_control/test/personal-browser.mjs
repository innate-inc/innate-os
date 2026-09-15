// Recorded-landmark replay through the real UI, transport and physics.
// Usage: STUDIO_URL=http://127.0.0.1:8841/ node test/personal-browser.mjs study-data/SESSION
// The camera and worker are test-only; this does not access the webcam.
import fs from "node:fs";
import path from "node:path";
import { chromium, expect } from "@playwright/test";
const directory = process.argv[2];
const manifest = JSON.parse(
  fs.readFileSync(path.join(directory, "manifest.json")),
);
const clips = Object.fromEntries(
  Object.entries(manifest.matches).map(([id, e]) => [
    id,
    JSON.parse(fs.readFileSync(path.join(directory, e.record))).frames,
  ]),
);
const browser = await chromium.launch({
  executablePath:
    process.env.CHROME_PATH ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.addInitScript((clips) => {
  window.clip = "neutral";
  window.missing = false;
  window.sent = [];
  window.starts = 0;
  window.syntheticYaw = 0;
  const Socket = WebSocket;
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      window.socket = this;
      this.addEventListener("message", (e) => {
        const m = JSON.parse(e.data);
        if (m.type === "state") window.simState = m;
        if (m.type === "begun") window.starts++;
      });
    }
    send(data) {
      const m = JSON.parse(data);
      if (m.op === "move") window.sent.push(m);
      super.send(data);
    }
  };
  navigator.mediaDevices.getUserMedia = async () => {
    const c = document.createElement("canvas");
    c.width = 640;
    c.height = 480;
    const draw = () => {
      c.getContext("2d").fillRect(0, 0, 640, 480);
      requestAnimationFrame(draw);
    };
    draw();
    return c.captureStream(30);
  };
  window.Worker = class {
    postMessage(data) {
      if (data.type === "init") {
        setTimeout(() => this.onmessage?.({ data: { type: "ready" } }), 0);
        return;
      }
      data.bitmap.close();
      const frames = clips[window.clip],
        frame = frames[Math.floor(performance.now() / 34) % frames.length];
      const c = Math.cos(window.syntheticYaw),
        s = Math.sin(window.syntheticYaw);
      const world = frame.world_landmarks.map((p) => ({
        ...p,
        x: c * p.x - s * p.z,
        z: s * p.x + c * p.z,
      }));
      setTimeout(() => {
        if (!this.stopped)
          this.onmessage?.({
            data: {
              ...data,
              type: "result",
              inferenceMs: 15,
              result: {
                landmarks: window.missing ? [] : [frame.landmarks],
                worldLandmarks: [world],
                handedness: [frame.handedness],
              },
            },
          });
      }, 15);
    }
    terminate() {
      this.stopped = true;
    }
  };
}, clips);
const pose = async (id, expected) => {
  await page.evaluate((id) => {
    window.clip = id;
  }, id);
  await expect
    .poll(
      async () => {
        const s = await page.evaluate(() => window.simState);
        return Math.max(
          ...expected.map((v, i) => Math.abs(s.wrist_measured[i] - v)),
        );
      },
      { timeout: 20000 },
    )
    .toBeLessThan(0.12);
};
try {
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8841/");
  await expect(page.locator("#personal-status")).toBeVisible();
  await expect(page.locator("#primary")).toBeEnabled({ timeout: 15000 });
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toContainText("Start following", {
    timeout: 15000,
  });
  await expect(page.locator("#ground-guide")).toBeHidden();
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand");
  await pose("pitch_up", [0, -0.5]);
  await pose("neutral", [0, 0]);
  await pose("pitch_down", [0, 0.5]);
  await pose("neutral", [0, 0]);
  await pose("roll_left", [-0.7, 0]);
  await pose("neutral", [0, 0]);
  await pose("roll_right", [0.7, 0]);
  await pose("neutral", [0, 0]);
  const yawReference = await page.evaluate(() => ({
    command: window.simState.wrist[2],
    measured: window.simState.wrist_measured[2],
  }));
  for (const yaw of [0.35, -0.35, 0]) {
    await page.evaluate((yaw) => {
      window.syntheticYaw = yaw;
    }, yaw);
    await expect
      .poll(
        () =>
          page.evaluate(
            (target) => Math.abs(window.simState.wrist[2] - target),
            Math.max(-0.45, Math.min(0.45, yawReference.command + yaw)),
          ),
        { timeout: 10000 },
      )
      .toBeLessThan(0.07);
    // The command must actually turn the physical base, not just a UI value.
    if (yaw)
      await expect
        .poll(
          () =>
            page.evaluate(
              ({ yaw, reference }) =>
                (window.simState.wrist_measured[2] - reference) *
                Math.sign(yaw),
              { yaw, reference: yawReference.measured },
            ),
          { timeout: 10000 },
        )
        .toBeGreaterThan(0.2);
  }
  // Closing and opening were recorded later at a different resting position.
  // Recenter there, exactly as the live UI supports, then check the grip pair.
  await page.evaluate(() => {
    window.clip = "open";
  });
  await page.locator("#recenter").click();
  await expect(page.locator("#primary")).toContainText("Start following", {
    timeout: 10000,
  });
  await page.locator("#primary").click();
  await expect
    .poll(() => page.evaluate(() => window.simState.grip), { timeout: 8000 })
    .toBeGreaterThan(0.92);
  await page.evaluate(() => {
    window.clip = "closed";
  });
  await expect
    .poll(() => page.evaluate(() => window.simState.grip), { timeout: 8000 })
    .toBeLessThan(0.08);
  await expect
    .poll(
      () => page.evaluate(() => Math.abs(window.simState.wrist_measured[1])),
      { timeout: 8000 },
    )
    .toBeLessThan(0.12);
  await page.evaluate(() => {
    window.missing = true;
  });
  await expect(page.locator("#stage-title")).toHaveText(
    "Holding your position",
  );
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  const starts = await page.evaluate(() => window.starts);
  await page.evaluate(() => {
    window.missing = false;
  });
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 10000,
  });
  expect(await page.evaluate(() => window.starts)).toBe(starts);
  expect(
    await page.evaluate(() =>
      window.sent.every(
        (m) => Number.isFinite(m.wrist[2]) && Math.abs(m.wrist[2]) <= 0.45,
      ),
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  await page.locator("#camera-off").click();
  await expect(page.locator("#hand-status")).toHaveText("Camera off");
  await page.screenshot({
    path: "artifacts/personal-mapping/studio.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
  console.log(
    "Personal replay passed: both pitches, both rolls, synthetic yaw turns the physical base both ways, gripper closure/opening, recenter, tracking hold/resume, bounded yaw, Escape, camera cleanup.",
  );
} catch (e) {
  console.log(
    await page.locator("#feedback").textContent(),
    await page.locator("#error").textContent(),
  );
  console.log(
    await page.evaluate(() => ({
      state: window.simState,
      last: window.sent.slice(-1),
    })),
  );
  throw e;
} finally {
  await browser.close();
}
