import assert from "node:assert/strict";
import {
  isKeepout,
  keepoutGridForMap,
  keepoutGridFromMessage,
  keepoutMessage,
  keepoutUpdateMatches,
  isRobotSelectionCatchup,
  mapFingerprintFromMessage,
  paintKeepout,
  sha256Hex,
  shouldActivateMapFingerprint,
} from "../js/map/keepoutMask.js";

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

const localizationMap = {
  header: { frame_id: "map" },
  info: {
    width: 2,
    height: 2,
    resolution: 0.5,
    origin: { position: { x: -2, y: -1 }, orientation: { x: 0, y: 0, z: 0, w: 1 } },
  },
  data: [0, 100, -1, 42],
};
const localizationHash = await mapFingerprintFromMessage(localizationMap);
assert.equal(localizationHash, "198e412bfd86ca78faf30d9ff7491cce2f9a0b25023bcd593b3024f44d7d7dc0");
const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
try {
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: undefined });
  assert.equal(await mapFingerprintFromMessage(localizationMap), localizationHash, "plain HTTP produces the same map identity");
} finally {
  if (cryptoDescriptor) Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
  else delete globalThis.crypto;
}
assert.equal(
  sha256Hex(new TextEncoder().encode("abc")),
  "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  "the HTTP-safe fallback implements SHA-256",
);
assert.equal(keepoutGridForMap(grid, localizationHash), null, "a new /map immediately rejects stale editor state");
grid.mapHash = localizationHash;
assert.equal(keepoutGridForMap(grid, localizationHash), grid, "editor state becomes usable only after its exact map arrives");
assert.equal(keepoutGridForMap(grid, null), null, "clearing the active map identity disables editing during a map switch");
assert.equal(shouldActivateMapFingerprint("new", "old", false, false), false, "an early /map waits for its selection notification");
assert.equal(shouldActivateMapFingerprint("new", "old", true, false), true, "an early new /map activates when selection catches up");
assert.equal(shouldActivateMapFingerprint("old", "old", true, false), false, "a stale latched map cannot reopen the editor");
assert.equal(shouldActivateMapFingerprint("new", "old", true, true), true, "a post-selection /map activates normally");
assert.equal(isRobotSelectionCatchup("new", "new", "new", true), true, "a new robot's late map name preserves its active map");
assert.equal(isRobotSelectionCatchup("old", "old", null, true), false, "a map name cannot preserve a disabled stale editor");
const pendingUpdate = { mapHash: localizationHash, data: grid.data.slice() };
assert.equal(keepoutUpdateMatches(grid, pendingUpdate), true, "the exact republished cells acknowledge an accepted edit");
grid.data[0] = grid.data[0] === 100 ? 0 : 100;
assert.equal(keepoutUpdateMatches(grid, pendingUpdate), false, "a different retained state cannot falsely acknowledge an edit");

console.log("keepoutMask tests passed");
