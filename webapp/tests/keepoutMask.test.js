import assert from "node:assert/strict";
import { isKeepout, keepoutGridFromMessage, keepoutMessage, paintKeepout } from "../js/map/keepoutMask.js";

function message(width = 8, height = 6, resolution = 0.5) {
  return {
    header: { frame_id: `map#keepout-map=${"a".repeat(64)}` },
    info: {
      width,
      height,
      resolution,
      origin: { position: { x: -2, y: -1 } },
    },
    data: new Array(width * height).fill(0),
  };
}

const grid = keepoutGridFromMessage(message());
assert.ok(grid);
assert.equal(paintKeepout(grid, -1.5, 0, 0.5, 0, 0.25, true), true);
assert.equal(isKeepout(grid, -1.5, 0), true);
assert.equal(isKeepout(grid, -0.5, 0), true, "a continuous stroke fills between sparse pointer events");
assert.equal(isKeepout(grid, 1.5, 0), false);

assert.equal(paintKeepout(grid, -0.5, 0, -0.5, 0, 0.25, false), true);
assert.equal(isKeepout(grid, -0.5, 0), false);

const roundTrip = keepoutGridFromMessage(keepoutMessage(grid));
assert.deepEqual(roundTrip, grid);
assert.equal(keepoutGridFromMessage({ info: { width: 2, height: 2, resolution: 0.1 }, data: [0] }), null);

const rotatedMap = message(2, 2, 1);
rotatedMap.info.origin = {
  position: { x: 10, y: 20 },
  orientation: { x: 0, y: 0, z: Math.SQRT1_2, w: Math.SQRT1_2 },
};
rotatedMap.header.frame_id = `map#keepout-map=${"b".repeat(64)}`;
const rotated = keepoutGridFromMessage(rotatedMap);
assert.ok(rotated);
assert.equal(paintKeepout(rotated, 9.5, 20.5, 9.5, 20.5, 0, true), true);
assert.equal(isKeepout(rotated, 9.5, 20.5), true, "world-to-cell conversion honors a rotated map origin");
const rotatedRoundTrip = keepoutGridFromMessage(keepoutMessage(rotated));
assert.ok(rotatedRoundTrip);
assert.ok(Math.abs(rotatedRoundTrip.originYaw - rotated.originYaw) < 1e-12);
assert.deepEqual({ ...rotatedRoundTrip, originYaw: rotated.originYaw }, rotated);

console.log("keepoutMask tests passed");
