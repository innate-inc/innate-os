import assert from "node:assert/strict";
import test from "node:test";
import { interpolateTraffic, type TrafficState } from "../src/trafficState.ts";

test("interpolate motion, but never sweep a respawn across the map", () => {
  const a: TrafficState = { world_epoch: 1, signals: {}, cars: { car: { pose: [20, 0, 0], spawn_seq: 0 } } };
  const b: TrafficState = { ...a, cars: { car: { pose: [22, 0, 0], spawn_seq: 0 } } };
  assert.equal(interpolateTraffic(a, b, .5)!.cars.car.pose[0], 21);
  b.cars.car = { pose: [-23, 0, 0], spawn_seq: 1 };
  assert.deepEqual(interpolateTraffic(a, b, .5)!.cars.car, a.cars.car);
  assert.deepEqual(interpolateTraffic(a, b, 1)!.cars.car, b.cars.car);
  assert.equal(interpolateTraffic(a, { ...b, world_epoch: 2 }, .5), a);
});
