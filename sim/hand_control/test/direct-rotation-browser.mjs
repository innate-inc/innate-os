// Known rigid 3D rotations, synthetic tracker/camera, real app and MuJoCo server.
import fs from "node:fs";
import assert from "node:assert/strict";
import { chromium, expect } from "@playwright/test";
import { Vector3 } from "three";
import { hand } from "./hand-fixture.mjs";
import { wristQuaternion } from "../src/fingertip-rotation.mjs";

const output = "artifacts/innate-ik";
fs.mkdirSync(output, { recursive: true });
function pose(degrees, gap = 0.09) {
  const result = hand(gap),
    points = result.worldLandmarks[0];
  // Known camera-axis rotation; never conjugate by the mapper's hand basis.
  const delta = wristQuaternion(degrees.map((v) => (v * Math.PI) / 180));
  const mid = new Vector3(
    (points[4].z + points[8].z) / 2,
    (points[4].x + points[8].x) / 2,
    (points[4].y + points[8].y) / 2,
  );
  result.worldLandmarks[0] = points.map((p) => {
    const v = new Vector3(p.z, p.x, p.y)
      .sub(mid)
      .applyQuaternion(delta)
      .add(mid);
    return { x: v.y, y: v.z, z: v.x };
  });
  result.landmarks[0] = result.worldLandmarks[0].map((p) => ({
    x: 0.5 + p.x * 2,
    y: 0.45 + ((p.y * 640) / 480) * 2,
    z: p.z * 2,
  }));
  return result;
}
const browser = await chromium.launch({
  executablePath:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
const errors = [],
  report = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.addInitScript(
  (initial) => {
    let frames = [initial],
      start = 0;
    window.chooseFrames = (next) => {
      frames = next;
      start = performance.now();
    };
    window.sent = [];
    window.missing = false;
    const Socket = WebSocket;
    window.WebSocket = class extends Socket {
      constructor(...args) {
        super(...args);
        this.addEventListener("message", (e) => {
          const m = JSON.parse(e.data);
          if (m.type === "state") window.simState = m;
        });
      }
      send(data) {
        const m = JSON.parse(data);
        if (m.op === "move") window.sent.push(m);
        super.send(data);
      }
    };
    navigator.mediaDevices.getUserMedia = async () => {
      const canvas = document.createElement("canvas");
      canvas.width = 640;
      canvas.height = 480;
      function draw() {
        canvas.getContext("2d").fillRect(0, 0, 640, 480);
        requestAnimationFrame(draw);
      }
      draw();
      return canvas.captureStream(30);
    };
    window.Worker = class {
      postMessage(data) {
        if (data.type === "init") {
          setTimeout(() => this.onmessage?.({ data: { type: "ready" } }), 0);
          return;
        }
        data.bitmap.close();
        const result =
          frames[
            Math.min(
              frames.length - 1,
              Math.floor((performance.now() - start) / 25),
            )
          ];
        setTimeout(() => {
          if (!this.stopped)
            this.onmessage?.({
              data: {
                ...data,
                type: "result",
                inferenceMs: 15,
                result: window.missing ? { landmarks: [] } : result,
              },
            });
        }, 15);
      }
      terminate() {
        this.stopped = true;
      }
    };
  },
  pose([0, 0, 0], 0),
);

try {
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8841/");
  await expect(page.locator("#personal-status")).toContainText(
    "fingertip pose control",
  );
  await expect(page.locator("#primary")).toBeEnabled();
  await page.locator("#primary").click();
  await expect(page.locator("#feedback")).toContainText("jaw line is visible");
  await page.waitForTimeout(1200);
  await expect(page.locator("#primary")).toHaveText("Calibrating…");
  assert.equal(await page.evaluate(() => window.sent.length), 0);
  await page.evaluate((frame) => window.chooseFrames([frame]), pose([0, 0, 0]));
  await expect(page.locator("#primary")).toContainText("Start following", {
    timeout: 15000,
  });
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand");
  await page.waitForTimeout(1000);
  const initial = await page.evaluate(() => window.sent.at(-1));
  let previous = [0, 0, 0],
    previousGap = 0.09;
  for (const [degrees, gap] of [
    [[30, 0, 0], 0.09],
    [[-30, 0, 0], 0.09],
    [[0, 30, 0], 0.09],
    [[0, -30, 0], 0.09],
    [[0, 0, 45], 0.09],
    [[0, 0, -45], 0.09],
    [[20, 25, 0], 0.09],
    [[0, 0, 0], 0.09],
    [[0, 0, 0], 0],
    [[30, 0, 0], 0],
    [[0, 30, 0], 0],
    [[0, 0, 0], 0.09],
  ]) {
    const frames = Array.from({ length: 61 }, (_, i) =>
      pose(
        degrees.map((v, j) => previous[j] + ((v - previous[j]) * i) / 60),
        previousGap + ((gap - previousGap) * i) / 60,
      ),
    );
    await page.evaluate((frames) => window.chooseFrames(frames), frames);
    await page.waitForTimeout(6500);
    const value = await page.evaluate(() => ({
      state: window.simState,
      command: window.sent.at(-1),
      title: document.querySelector("#stage-title").textContent,
    }));
    report.push({ degrees, gap, ...value });
    fs.writeFileSync(`${output}/browser.json`, JSON.stringify(report, null, 2));
    const expected = wristQuaternion(
      degrees.map((v) => (v * Math.PI) / 180),
    ).multiply(wristQuaternion(initial.wrist));
    const commandError =
      (wristQuaternion(value.command.wrist).angleTo(expected) * 180) / Math.PI;
    const physicalTarget = [...value.command.wrist];
    if (degrees[2] !== 0) physicalTarget[2] = 0;
    const measuredError =
      (wristQuaternion(value.state.wrist_measured).angleTo(
        wristQuaternion(physicalTarget),
      ) *
        180) /
      Math.PI;
    console.log(
      JSON.stringify({
        degrees,
        gap,
        commandError,
        measuredError,
        physicalTarget: physicalTarget.map((v) => (v * 180) / Math.PI),
        actual: value.state.wrist_measured.map((v) => (v * 180) / Math.PI),
        limited: value.state.rotation_limited,
        grip: value.state.grip,
        title: value.title,
      }),
    );
    assert.equal(value.title, "Following your hand");
    assert.ok(value.state.active);
    assert.ok(commandError < 0.02, `Unit-gain command error: ${commandError}°`);
    assert.ok(measuredError < 2, `Measured rotation error: ${measuredError}°`);
    assert.equal(value.state.ik_solver, "innate_kdl");
    assert.ok(
      value.state.error_mm < 3,
      `Grasp point moved ${value.state.error_mm} mm`,
    );
    if (degrees[2])
      assert.ok(
        value.state.rotation_limited,
        "Unreachable yaw must be reported, not applied as lateral travel",
      );
    {
      for (const key of ["horizontal", "vertical", "reach"])
        assert.ok(
          Math.abs(value.command[key] - initial[key]) < 0.0001,
          "Every twist, including closed fingers, must preserve the midpoint position command",
        );
    }
    if (gap === 0) assert.ok(value.state.grip < 0.02);
    previous = degrees;
    previousGap = gap;
  }
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
  // Calibration must remove a genuine starting quarter-turn, not just track
  // deltas around a mismatched neutral. Check both command and actual joints.
  await page.evaluate(
    (frame) => window.chooseFrames([frame]),
    pose([90, 0, 0]),
  );
  await page.locator("#recenter").click();
  await expect(page.locator("#primary")).toContainText("Start following");
  await page.locator("#primary").click();
  await expect
    .poll(
      async () => {
        const state = await page.evaluate(() => window.simState);
        return Math.abs(Math.abs(state.wrist_measured[0]) - Math.PI / 2);
      },
      { timeout: 10000 },
    )
    .toBeLessThan(0.02);
  const aligned = await page.evaluate(() => window.sent.at(-1));
  for (const key of ["horizontal", "vertical", "reach"])
    assert.ok(Math.abs(aligned[key] - initial[key]) < 0.005);
  // Resuming with a different hand angle keeps the held claw, rather than
  // applying the calibration correction again.
  await page.locator("#primary").click();
  const held = await page.evaluate(() => window.simState.wrist_measured[0]);
  await page.evaluate(
    (frame) => window.chooseFrames([frame]),
    pose([30, 0, 0]),
  );
  await expect(page.locator("#hand-status")).toHaveText("Hand tracked");
  await page.waitForTimeout(200);
  await page.locator("#primary").click();
  await expect(page.locator("#stage-title")).toHaveText("Following your hand");
  await page.waitForTimeout(600);
  const resumed = await page.evaluate(() => window.simState.wrist_measured[0]);
  assert.ok(
    Math.abs(resumed - held) < 0.02,
    "Resume must preserve achieved roll",
  );
  await page.locator("#primary").click();
  await page.screenshot({ path: `${output}/browser.png` });
  assert.deepEqual(errors, []);
  console.log(
    "Direct fingertip rotation, actual joints, closed-hand turns, recovery, roll calibration, pause and rendering passed.",
  );
} finally {
  await browser.close();
}
