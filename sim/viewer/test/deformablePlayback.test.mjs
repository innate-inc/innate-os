import assert from "node:assert/strict";
import test from "node:test";
import { DeformablePlayback } from "../src/physics/deformablePlayback.ts";

const frame = (t, x, id = 1) => ({ id, t, vertexCount: 1, positions: new Float32Array([x, 0, 0]) });

test("cloth follows the delayed finger clock, not the newest arrival", () => {
  const history = new DeformablePlayback();
  history.push(frame(1, 0));
  history.push(frame(1.1, .1));
  const [out] = history.sample(1.025);
  assert.ok(Math.abs(out.positions[0] - .025) < 1e-7);
  assert.equal(out.t, 1.025);
  assert.equal(history.sample(3)[0].positions[0], Math.fround(.1));
  assert.equal(history.sample(.5)[0].positions[0], 0);
});

test("reset, duplicate timestamp, and topology changes discard incompatible history", () => {
  const history = new DeformablePlayback();
  history.push(frame(10, 10));
  history.push(frame(0, 1));
  history.push(frame(0, 2));
  assert.equal(history.sample(0)[0].positions[0], 2);
  history.push({ ...frame(1, 3), vertexCount: 2, positions: new Float32Array(6).fill(3) });
  assert.equal(history.sample(.5)[0].positions.length, 6);
  history.clear();
  assert.deepEqual(history.sample(1), []);
});

test("independent props and bounded long-running history", () => {
  const history = new DeformablePlayback();
  for (let t = 0; t < 1000; t++) history.push(frame(t, t));
  history.push(frame(0, 8, 2));
  const out = history.sample(998.5);
  assert.equal(out[0].positions[0], 998.5);
  assert.equal(out[1].positions[0], 8);
  history.delete(1);
  history.push(frame(1001, 20));
  assert.equal(history.sample(1000).find((f) => f.id === 1).positions[0], 20);
});
