// Recorded held poses with interpolated test transitions; synthetic camera only.
import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";
import { chromium, expect } from "@playwright/test";
const directory = process.argv[2];
if (!directory) throw new Error("Pass a completed refinement study directory.");
const output = process.env.PINCH_REPORT_DIR || "artifacts/pinch-browser";
fs.mkdirSync(output, { recursive: true });
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
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } }),
  errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.addInitScript((clips) => {
  const representative = (id) => clips[id][Math.floor(clips[id].length / 2)];
  let current = representative("refine_reference"),
    old = current,
    start = 0,
    next = current;
  window.choosePose = (id) => {
    old = current;
    next = representative(id);
    start = performance.now();
  };
  window.missing = false;
  window.sent = [];
  window.states = [];
  const Socket = WebSocket;
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      window.socket = this;
      this.addEventListener("message", (e) => {
        const m = JSON.parse(e.data);
        if (m.type === "state") {
          window.simState = m;
          window.states.push(m);
        }
        if (m.type === "hello") window.profile = m.personal_profile;
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
      const a = Math.min(1, (performance.now() - start) / 1200);
      current = {};
      for (const key of ["landmarks", "world_landmarks"])
        current[key] = next[key].map((p, i) =>
          Object.fromEntries(
            ["x", "y", "z"].map((k) => [
              k,
              old[key][i][k] + (p[k] - old[key][i][k]) * a,
            ]),
          ),
        );
      setTimeout(() => {
        if (!this.stopped)
          this.onmessage?.({
            data: {
              ...data,
              type: "result",
              inferenceMs: 15,
              result: {
                landmarks: window.missing ? [] : [current.landmarks],
                worldLandmarks: [current.world_landmarks],
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
const report = [];
try {
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8841/");
  await expect(page.locator("#personal-status")).toContainText("39 poses");
  await expect(page.locator("#primary")).toBeEnabled();
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toContainText("Start following", {
    timeout: 15000,
  });
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand");
  await page.waitForTimeout(1000);
  const initialTarget = await page.evaluate(() => window.simState.target);
  for (const id of [
    "refine_floor_open",
    "refine_floor_closed",
    "refine_lift",
    "refine_reference",
    "refine_yaw_left",
    "refine_yaw_right",
    "refine_reference",
  ]) {
    await page.evaluate((id) => window.choosePose(id), id);
    await page.waitForTimeout(5500);
    const value = await page.evaluate(() => ({
      state: window.simState,
      command: window.sent.at(-1),
      title: document.querySelector("#stage-title").textContent,
    }));
    report.push({ id, ...value });
    console.log(
      id,
      JSON.stringify({
        pitch: (value.state.wrist_measured[1] * 180) / Math.PI,
        yaw: (value.state.wrist_measured[2] * 180) / Math.PI,
        height: value.state.height_mm,
        grip: value.state.grip,
        error: value.state.error_mm,
        title: value.title,
      }),
    );
    assert.equal(value.title, "Following your hand");
    assert.ok(value.command);
    assert.ok(value.state.active);
  }
  const lastTarget = report.at(-1).state.target;
  assert.ok(
    Math.hypot(...lastTarget.map((v, i) => v - initialTarget[i])) < 0.002,
    "Returning the hand must restore the grasp point",
  );
  const opened = report[0].state,
    closed = report[1].state,
    lifted = report[2].state;
  assert.ok(opened.wrist_measured[1] > (65 * Math.PI) / 180);
  assert.ok(opened.height_mm < 30);
  assert.ok(closed.grip < 0.02);
  assert.ok(lifted.height_mm > closed.height_mm + 25);
  await page.evaluate(() => (window.missing = true));
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  await page.evaluate(() => (window.missing = false));
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 10000,
  });
  await page.locator("#primary").click();
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  await page.screenshot({ path: path.join(output, "browser.png") });
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    path.join(output, "browser.json"),
    JSON.stringify(report, null, 2),
  );
  console.log(
    "Browser transport, tracking recovery, pause, and rendering checks passed.",
  );
} finally {
  await browser.close();
}
