// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pure known-free destination checks for js/map/goalValidation.js.

import assert from "node:assert/strict";
import { goalCellError } from "../js/map/goalValidation.js";

const grid = { width: 3, height: 2, resolution: 0.5, originX: -1, originY: 2 };
// Bottom row: free, unknown, occupied. Top row: all free.
const cells = [0, -1, 100, 0, 0, 0];

assert.equal(goalCellError(grid, cells, -0.75, 2.25, 65), null);
assert.match(goalCellError(grid, cells, -0.25, 2.25, 65) ?? "", /explored floor/);
assert.match(goalCellError(grid, cells, 0.25, 2.25, 65) ?? "", /free floor/);

for (const [x, y] of [
  [-1.01, 2.25],
  [0.5, 2.25],
  [-0.75, 1.99],
  [-0.75, 3.0],
]) {
  assert.match(goalCellError(grid, cells, x, y, 65) ?? "", /inside the map/);
}

assert.match(goalCellError(null, null, 0, 0, 65) ?? "", /No navigation map/);
assert.match(goalCellError(grid, cells, Number.NaN, 0, 65) ?? "", /valid destination/);

// A +90° origin rotates the bottom row so its cell centers run up the world-y
// axis; validation must index in grid-local coordinates, not world axes.
const rotated = { ...grid, originX: 10, originY: 20, originYaw: Math.PI / 2 };
assert.equal(goalCellError(rotated, cells, 9.75, 20.25, 65), null);
assert.match(goalCellError(rotated, cells, 9.75, 20.75, 65) ?? "", /explored floor/);
assert.match(goalCellError(rotated, cells, 9.75, 21.25, 65) ?? "", /free floor/);

console.log("12 passed");
