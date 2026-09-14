// Deterministic landmark-boundary integration tests, separate from real model
// accuracy tests in browser.mjs. No production hooks or webcam access.
import { chromium, expect } from "@playwright/test";
const browser = await chromium.launch({
  executablePath:
    process.env.CHROME_PATH ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
await page.addInitScript(() => {
  window.hand = {
    gesture: "open",
    missing: false,
    x: 0,
    y: 0.19,
    side: "Right",
    delay: 0,
    turn: 0,
  };
  window.starts = 0;
  window.connections = 0;
  const NativeSocket = window.WebSocket;
  window.WebSocket = class extends NativeSocket {
    constructor(...args) {
      super(...args);
      window.socket = this;
      window.connections++;
      this.addEventListener("message", (event) => {
        const message = JSON.parse(event.data);
        if (message.type === "state") window.simState = message;
        if (message.type === "begun") window.starts++;
      });
    }
  };
  navigator.mediaDevices.getUserMedia = async () => {
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 480;
    const ctx = canvas.getContext("2d");
    const draw = () => {
      ctx.fillStyle = "#202b26";
      ctx.fillRect(0, 0, 640, 480);
      requestAnimationFrame(draw);
    };
    draw();
    return canvas.captureStream(30);
  };
  window.Worker = class {
    postMessage(data) {
      if (data.type === "init") {
        setTimeout(() => this.onmessage?.({ data: { type: "ready" } }), 0);
        return;
      }
      if (data.type !== "frame") return;
      data.bitmap.close();
      const h = { ...window.hand };
      const points = Array.from({ length: 21 }, () => ({
        x: 0.5,
        y: 0.5,
        z: 0,
      }));
      points[0] = { x: 0.5, y: 0.75, z: 0 };
      points[4] = { x: 0.8, y: 0.5, z: 0 };
      for (const [f, x] of [
        [5, 0.62],
        [9, 0.54],
        [13, 0.46],
        [17, 0.38],
      ])
        for (let k = 0; k < 4; k++)
          points[f + k] = { x, y: 0.55 - k * 0.105, z: 0 };
      if (h.gesture === "together") points[4] = { ...points[8], x: 0.64 };
      if (h.gesture === "index-middle") points[12] = { ...points[8] };
      for (const p of points) {
        const x = p.x - 0.5;
        p.x = 0.5 + x * Math.cos(h.turn) + h.x;
        p.z = x * Math.sin(h.turn);
        p.y += h.y;
      }
      setTimeout(() => {
        if (!this.stopped)
          this.onmessage?.({
            data: {
              type: "result",
              generation: data.generation,
              timestamp: data.timestamp,
              capturedAt: data.capturedAt,
              inferenceMs: h.delay || 15,
              result: {
                landmarks: h.missing ? [] : [points],
                handedness: [[{ categoryName: h.side }]],
              },
            },
          });
      }, h.delay || 15);
    }
    terminate() {
      this.stopped = true;
    }
  };
});
try {
  await page.goto(process.env.STUDIO_URL || "http://127.0.0.1:8840/");
  await expect(page.locator("#primary")).toBeEnabled({ timeout: 20000 });
  await page.locator("#primary").click();
  await expect(page.locator("#primary")).toHaveText("Start following", {
    timeout: 5000,
  });
  await page.locator("#primary").click();
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(true);
  const initial = await page.evaluate(() => window.simState.ee);
  expect(initial[2]).toBeLessThan(0.005);
  await expect(page.locator("#ground-guide")).toBeVisible();
  const groundLine = await page.locator("#ground-guide").getAttribute("style");
  for (const y of [0.13, 0.07, 0.01, -0.05]) {
    await page.evaluate((y) => (window.hand.y = y), y);
    await page.waitForTimeout(100);
  }
  await expect
    .poll(() => page.evaluate(() => window.simState.ee[2]), { timeout: 3500 })
    .toBeGreaterThan(0.14);
  // Test finger independence in free space; real floor contact can displace
  // the arm when closing at ground level and is covered in the physics tests.
  await page.waitForTimeout(700);
  const grippingPose = await page.evaluate(() => window.simState.ee);
  for (const gesture of [
    "together",
    "open",
    "index-middle",
    "together",
    "open",
  ]) {
    await page.evaluate((gesture) => (window.hand.gesture = gesture), gesture);
    await expect
      .poll(() => page.evaluate(() => window.simState.grip), { timeout: 2500 })
      .toBeGreaterThanOrEqual(0);
    await expect
      .poll(
        () =>
          page.evaluate(
            ({ open }) => Math.abs(window.simState.grip - (open ? 1 : 0)),
            { open: gesture !== "together" },
          ),
        { timeout: 2500 },
      )
      .toBeLessThan(0.02);
    await expect(page.locator("#stage-title")).toHaveText(
      "Following your hand",
    );
    const ee = await page.evaluate(() => window.simState.ee);
    expect(Math.hypot(...ee.map((v, i) => v - grippingPose[i]))).toBeLessThan(
      0.003,
    );
  }
  console.log(
    "Thumb/index closure and separation drive the gripper; middle-finger movement has no effect.",
  );
  for (const y of [0.01, 0.07, 0.13, 0.19]) {
    await page.evaluate((y) => (window.hand.y = y), y);
    await page.waitForTimeout(100);
  }
  await expect
    .poll(() => page.evaluate(() => window.simState.ee[2]), { timeout: 3500 })
    .toBeLessThan(0.005);
  expect(await page.locator("#ground-guide").getAttribute("style")).toBe(
    groundLine,
  );
  console.log(
    "The lowest visible hand position starts at ground; lifting and lowering returns to ground.",
  );
  await page.evaluate(() => {
    window.hand.side = "Left";
    window.hand.turn = 1.2;
  });
  await page.waitForTimeout(400);
  await expect(page.locator("#stage-title")).toHaveText("Following your hand");
  expect(await page.evaluate(() => window.starts)).toBe(1);
  await page.evaluate(() => (window.hand.missing = true));
  await expect
    .poll(() => page.evaluate(() => window.simState.active), {
      intervals: [30],
      timeout: 600,
    })
    .toBe(false);
  await page.evaluate(() => (window.hand.missing = false));
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 600,
  });
  expect(await page.evaluate(() => window.starts)).toBe(1);
  console.log(
    "Palm turns, handedness flicker and brief tracking loss retain the session.",
  );
  await page.evaluate(() => (window.hand.delay = 480));
  await expect
    .poll(() => page.evaluate(() => window.simState.active), {
      intervals: [50],
      timeout: 1500,
    })
    .toBe(false);
  await page.evaluate(() => (window.hand.delay = 15));
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 1800,
  });
  expect(await page.evaluate(() => window.starts)).toBe(1);
  await page.evaluate(() => (window.hand.missing = true));
  await page.waitForTimeout(1200);
  const held = await page.evaluate(() => window.simState.ee);
  await page.evaluate(() => {
    window.hand.missing = false;
    window.hand.x = 0.14;
  });
  await expect(page.locator("#stage-title")).toHaveText("Following your hand", {
    timeout: 1200,
  });
  await page.waitForTimeout(400);
  const after = await page.evaluate(() => window.simState.ee);
  expect(Math.hypot(...after.map((v, i) => v - held[i]))).toBeLessThan(0.004);
  expect(await page.evaluate(() => window.starts)).toBe(1);
  expect(await page.evaluate(() => window.connections)).toBe(1);
  console.log(
    "Slow frames hold and recover; returning elsewhere reanchors without a position jump or reconnect.",
  );
  await page.keyboard.press("Escape");
  await expect
    .poll(() => page.evaluate(() => window.simState.active))
    .toBe(false);
  await page.waitForTimeout(400);
  await expect(page.locator("#primary")).toHaveText("Start following");
  expect(errors).toEqual([]);
} finally {
  await browser.close();
}
