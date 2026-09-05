import assert from "node:assert/strict";
import test from "node:test";
import { MAX_RENDER_PIXELS, stagePixelRatio } from "../src/renderBudget.ts";

test("stage keeps full resolution by default and only bounds pixels after opting in", () => {
  // Small -> measured Retina desktop -> 4K -> portrait -> small again,
  // including fractional DPR and >2x devices. Three floors buffer dimensions.
  for (const [width, height, dpr] of [
    [800, 600, 1], [800, 600, 2], [2147, 1420, 2], [3840, 2160, 1],
    [1440, 2560, 2], [1920, 1080, 1.25], [390, 844, 3], [800, 600, 2],
  ]) {
    assert.equal(stagePixelRatio(width, height, dpr), Math.min(dpr, 2), "no automatic downgrade");
    const ratio = stagePixelRatio(width, height, dpr, true);
    const pixels = Math.floor(width * ratio) * Math.floor(height * ratio);
    assert.ok(pixels <= MAX_RENDER_PIXELS, `${width}x${height}@${dpr}: ${pixels}`);
    assert.ok(ratio > 0 && ratio <= Math.min(dpr, 2));
    if (width * height * Math.min(dpr, 2) ** 2 <= MAX_RENDER_PIXELS) {
      assert.equal(ratio, Math.min(dpr, 2));
    } else {
      assert.ok(pixels > MAX_RENDER_PIXELS * .998, "use the available budget");
    }
  }
  assert.ok(stagePixelRatio(3840, 2160, 1, true) < 1);
  assert.equal(stagePixelRatio(3840, 2160, 1, false), 1, "full resolution can be restored");
  // Hidden stages are skipped by the caller; invalid inputs still stay finite.
  assert.equal(stagePixelRatio(0, 600, 2), 1);
  assert.equal(stagePixelRatio(800, NaN, 2), 1);
  assert.equal(stagePixelRatio(800, 600, NaN), 1);
});
