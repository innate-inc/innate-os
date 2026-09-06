import assert from "node:assert/strict";
import test from "node:test";
import { geomRadius, isValidRoomGeom, roomBounds, type RoomInfo } from "../src/roomManifest.ts";

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
