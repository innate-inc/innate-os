import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "vite";
import { PerspectiveCamera, Vector3 } from "three";

// Bundle the production scene without constructing its WebGL renderer. Test
// actual camera projection, including the offset sign, rather than its formula.
const bundle = await build({
  configFile: false,
  logLevel: "silent",
  build: {
    write: false,
    lib: { entry: fileURLToPath(new URL("../src/scene.ts", import.meta.url)), formats: ["es"] },
  },
});
const output = Array.isArray(bundle) ? bundle[0] : bundle;
const chunk = output.output.find((item) => item.type === "chunk")!;
const { SimScene } = await import(`data:text/javascript;base64,${Buffer.from(chunk.code).toString("base64")}`);

function scene(width: number, height: number) {
  const value = Object.create(SimScene.prototype);
  value.fixedSize = { width, height };
  value.safeInsetRight = 0;
  value.safeInsetBottom = 0;
  value.safeInsetTop = 0;
  value.safeInsetLeft = 0;
  value.camera = new PerspectiveCamera(50, width / height, 0.1, 100);
  return value;
}
function targetPixel(value: any) {
  const projected = new Vector3(0, 0, -5).project(value.camera);
  return {
    x: (projected.x + 1) * value.fixedSize.width / 2,
    y: (1 - projected.y) * value.fixedSize.height / 2,
  };
}

test("phone target follows the exposed area as chat opens, drags, and collapses", () => {
  const value = scene(407, 920);
  for (const bottom of [474, 600, 74, 892]) {
    value.setSafeInsets({ bottom });
    assert.ok(Math.abs(targetPixel(value).y - (920 - bottom) / 2) < 1e-8);
    assert.equal(targetPixel(value).x, 407 / 2);
  }
});

test("switching to desktop and detaching clears the mobile inset", () => {
  const value = scene(1200, 800);
  value.setSafeInsets({ bottom: 400 });
  value.setSafeInsets({ right: 400, bottom: 0 });
  assert.ok(Math.abs(targetPixel(value).x - 400) < 1e-8);
  assert.equal(targetPixel(value).y, 400);
  value.setSafeInsets({ right: 0 });
  assert.deepEqual(targetPixel(value), { x: 600, y: 400 });
  assert.equal(value.camera.view.enabled, false);
});

test("full-bleed canvas frames the robot inside asymmetric safe areas", () => {
  const value = scene(407, 920);
  value.setSafeInsets({ top: 59, bottom: 474, left: 0, right: 0 });
  assert.ok(Math.abs(targetPixel(value).y - (59 + 920 - 474) / 2) < 1e-8);
  value.fixedSize = { width: 920, height: 407 };
  value.camera.aspect = 920 / 407;
  value.setSafeInsets({ top: 0, bottom: 220, left: 59, right: 20 });
  assert.ok(Math.abs(targetPixel(value).x - (59 + 920 - 20) / 2) < 1e-8);
  assert.ok(Math.abs(targetPixel(value).y - (407 - 220) / 2) < 1e-8);
});
