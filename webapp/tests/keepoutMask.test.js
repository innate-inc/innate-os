import assert from "node:assert/strict";
import { blankKeepoutGrid, isKeepout, keepoutGridFromMessage, keepoutMessage, paintKeepout } from "../js/map/keepoutMask.js";

function message(width = 8, height = 6, resolution = 0.5) {
  return {
    header: { frame_id: "map" },
    info: {
      width,
      height,
      resolution,
      origin: { position: { x: -2, y: -1 } },
    },
    data: new Array(width * height).fill(0),
  };
}

const grid = blankKeepoutGrid(message());
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

console.log("keepoutMask tests passed");
