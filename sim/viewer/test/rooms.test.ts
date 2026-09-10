import assert from "node:assert/strict";
import test from "node:test";
import { geomRadius, isCeiling, isValidRoomGeom, roomBounds, type RoomInfo } from "../src/roomManifest.ts";

const box = { type: "box", size: [1, 2, 0.5], pos: [10, 0, 0.5], quat: [1, 0, 0, 0], rgba: [1, 1, 1, 1], collide: true } as const;
const lamp = { type: "cylinder", size: [0.025, 0.015], pos: [0, 5, 1.2], quat: [0, 0, 0, 1], rgba: [1, 1, 1, 1], collide: false } as const;

test("bounds enclose every geom whatever its orientation", () => {
  const rooms: RoomInfo[] = [{ name: "r", title: "R", geoms: [box, lamp] }];
  const bounds = roomBounds(rooms)!;
  // The box's bounding radius covers it even rotated 45 degrees about z.
  const r = geomRadius(box);
  assert.equal(r, Math.hypot(1, 2, 0.5));
  // Each axis takes its extreme from whichever geom reaches furthest: the
  // lamp at x = 0 sets min x, the box at y = 0 sets min y and, at z = 0.5, min z.
  assert.deepEqual(bounds.min, [0 - geomRadius(lamp), 0 - r, 0.5 - r]);
  assert.equal(bounds.max[0], 10 + r);
  assert.equal(bounds.max[1], 5 + geomRadius(lamp));
  assert.equal(bounds.max[2], 0.5 + r);
  assert.equal(roomBounds([]), null);
});

test("a malformed geom is skipped rather than thrown on", () => {
  assert.ok(isValidRoomGeom(box));
  assert.ok(!isValidRoomGeom({ ...box, size: [1, 2] })); // a box needs three half-extents
  assert.ok(!isValidRoomGeom({ ...box, size: [1, 2, 0] })); // and each must be positive
  assert.ok(!isValidRoomGeom({ ...box, type: "mesh" }));
  assert.ok(!isValidRoomGeom({ ...box, pos: [1, NaN, 0] }));
  assert.ok(!isValidRoomGeom(null));
  // A bad geom does not poison the bounds of the good ones beside it.
  const rooms = [{ name: "r", title: "R", geoms: [box, { ...box, pos: [1, NaN, 0] }] }] as unknown as RoomInfo[];
  assert.deepEqual(roomBounds(rooms), roomBounds([{ name: "r", title: "R", geoms: [box] }]));
});

test("a ceiling is real in the sim but not drawn over the observer's view", () => {
  // Counter's lid: a decor box at z = 1.43 spanning the whole room. Drawn as a
  // closed box it hid the robot and every prop from the top camera.
  const lid = { ...box, name: "ceiling", collide: false, size: [2.3, 1.8, 0.03], pos: [0, 0, 1.43] } as const;
  assert.ok(isValidRoomGeom(lid)); // still a valid geom: it stays in the bounds
  assert.ok(isCeiling(lid));
  assert.ok(!isCeiling(box));
  assert.ok(!isCeiling({ ...box, name: "floor" }));
});
