// Pose replay through the live UI/transport/physics. No live webcam is accessed.
import fs from "node:fs";
import path from "node:path";
import { chromium, expect } from "@playwright/test";

const directory = process.argv[2];
const manifest = JSON.parse(
  fs.readFileSync(path.join(directory, "manifest.json")),
);
const records = Object.fromEntries(
  Object.entries(manifest.matches).map(([id, entry]) => [
    id,
    JSON.parse(fs.readFileSync(path.join(directory, entry.record))),
  ]),
);
const clips = process.env.INFERRED_FRAMES_FILE
  ? JSON.parse(fs.readFileSync(process.env.INFERRED_FRAMES_FILE))
  : Object.fromEntries(
      Object.entries(records).map(([id, record]) => [id, record.frames]),
    );
const url = process.env.STUDIO_URL || "http://127.0.0.1:8841/";
const browser = await chromium.launch({
  executablePath:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.addInitScript((clips) => {
  window.clip = "floor_reference";
  window.events = [];
  window.states = [];
  window.starts = 0;
  const Socket = WebSocket;
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", ({ data }) => {
        const m = JSON.parse(data);
        if (m.type === "state") {
          window.state = m;
          window.states.push({ ...m, received: performance.now() });
          if (window.states.length > 500) window.states.shift();
        }
        if (m.type === "error") window.events.push(m);
        if (m.type === "begun" && m.request !== 900) window.starts++;
      });
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
  let activeClip,
    changed = 0,
    previous,
    last;
  const blend = (a, b, t) => ({
    ...b,
    landmarks: b.landmarks.map((p, i) =>
      Object.fromEntries(
        ["x", "y", "z"].map((axis) => [
          axis,
          a.landmarks[i][axis] * (1 - t) + p[axis] * t,
        ]),
      ),
    ),
    world_landmarks: b.world_landmarks.map((p, i) =>
      Object.fromEntries(
        ["x", "y", "z"].map((axis) => [
          axis,
          a.world_landmarks[i][axis] * (1 - t) + p[axis] * t,
        ]),
      ),
    ),
  });
  window.Worker = class {
    postMessage(data) {
      if (data.type === "init") {
        setTimeout(() => this.onmessage?.({ data: { type: "ready" } }), 0);
        return;
      }
      data.bitmap.close();
      const now = performance.now(),
        frames = clips[window.clip];
      let frame = frames[Math.floor(now / 34) % frames.length];
      if (activeClip !== window.clip) {
        previous = last || frame;
        changed = now;
        activeClip = window.clip;
      }
      // The takes are separate static poses. Interpolate the transition so a
      // synthetic instantaneous image jump does not trigger a hand-change hold.
      frame = blend(previous, frame, Math.min(1, (now - changed) / 1200));
      last = frame;
      setTimeout(() => {
        if (!this.stopped)
          this.onmessage?.({
            data: {
              ...data,
              type: "result",
              inferenceMs: 15,
              result: {
                landmarks: [frame.landmarks],
                worldLandmarks: [frame.world_landmarks],
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

try {
  await page.goto(url);
  await expect(page.locator("#primary")).toBeEnabled({ timeout: 20000 });
  // Start at the exercise's actual reference pose, rather than silently
  // treating a different arm height as the calibration's absolute origin.
  await page.evaluate(
    (target) =>
      new Promise((resolve, reject) => {
        const socket = new WebSocket("ws://" + location.host + "/control");
        let token,
          sequence = 0;
        const finish = (error) => {
          clearInterval(timer);
          clearTimeout(timeout);
          socket.send(JSON.stringify({ op: "stop" }));
          socket.close();
          error ? reject(error) : resolve();
        };
        const timer = setInterval(() => {
          if (token)
            socket.send(
              JSON.stringify({
                op: "study_pose",
                token,
                pose_id: "floor_reference",
                sequence: ++sequence,
                captured_at: Date.now(),
              }),
            );
        }, 100);
        const timeout = setTimeout(
          () => finish(new Error("Reference pose did not settle")),
          15000,
        );
        socket.onopen = () =>
          socket.send(
            JSON.stringify({ op: "begin", mode: "study", request: 900 }),
          );
        socket.onmessage = ({ data }) => {
          const m = JSON.parse(data);
          if (m.type === "begun") token = m.token;
          if (m.type === "error") finish(new Error(m.message));
          if (
            m.type === "state" &&
            sequence > 8 &&
            Object.entries(target.joints).every(
              ([n, v]) => Math.abs(m.joints[n] - v) < 0.02,
            )
          )
            finish();
        };
      }),
    records.floor_reference.target,
  );
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toHaveText("Start following", {
    timeout: 15000,
  });
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand");
  const results = [];
  for (const id of [
    "floor_down_mid",
    "floor_down_steep",
    "floor_open",
    "floor_closed",
    "floor_lift",
    "floor_repeat",
  ]) {
    await page.evaluate((id) => {
      window.clip = id;
      window.changed = performance.now();
    }, id);
    const expected = records[id].target;
    await expect
      .poll(
        () =>
          page.evaluate((expected) => {
            const recent = window.states
              .filter((s) => s.received > window.changed + 3000)
              .slice(-30);
            if (recent.length < 30) return 10;
            const mean =
              recent.reduce((n, s) => n + s.wrist_measured[1], 0) /
              recent.length;
            return Math.abs(
              mean - expected.angles.slice(1, 4).reduce((a, b) => a + b, 0),
            );
          }, expected),
        { timeout: 22000 },
      )
      .toBeLessThan(0.16);
    const result = await page.evaluate(() => {
      const recent = window.states.slice(-30);
      const mean = (f) => recent.reduce((n, s) => n + f(s), 0) / recent.length;
      return {
        clip: window.clip,
        pitch: mean((s) => s.wrist_measured[1]),
        roll: mean((s) => s.wrist_measured[0]),
        ee: [0, 1, 2].map((i) => mean((s) => s.ee[i])),
        grip: mean((s) => s.grip),
        starts: window.starts,
      };
    });
    expect(Math.abs(result.roll)).toBeLessThan(0.16);
    if (id !== "floor_repeat") {
      expect(
        Math.hypot(...result.ee.map((v, i) => v - expected.ee[i])),
      ).toBeLessThan(0.03);
      if (id === "floor_open") expect(result.grip).toBeGreaterThan(0.85);
      if (id === "floor_closed" || id === "floor_lift")
        expect(result.grip).toBeLessThan(0.1);
    }
    results.push(result);
    console.log(JSON.stringify(result));
  }
  expect(await page.evaluate(() => window.starts)).toBe(1);
  await page.keyboard.press("Escape");
  await expect.poll(() => page.evaluate(() => window.state.active)).toBe(false);
  await page.locator("#camera-off").click();
  expect(await page.evaluate(() => window.events)).toEqual([]);
  expect(errors).toEqual([]);
  const kind = process.env.INFERRED_FRAMES_FILE
    ? "fresh-inference"
    : "recorded-landmarks";
  fs.writeFileSync(
    "artifacts/floor-mapping/" + kind + "-replay.json",
    JSON.stringify(results, null, 2),
  );
  console.log(
    "Floor replay passed: pitch, position, open/close/lift, held-out return, no reanchoring, stop.",
  );
} catch (error) {
  console.log(
    await page.evaluate(() => ({
      state: window.state,
      events: window.events,
      starts: window.starts,
    })),
  );
  await page.screenshot({
    path: "artifacts/floor-mapping/replay-failure.png",
    fullPage: true,
  });
  throw error;
} finally {
  await browser.close();
}
