import assert from "node:assert/strict";
import test from "node:test";

import { decodeDeformableFrame, IDF1_HEADER_BYTES } from "../src/physics/deformableFrame.ts";

function idf1({ id = 7, t = 1.25, positions = [1, 2, 3, 4, 5, 6], reserved = 0 } = {}) {
  const vertexCount = positions.length / 3;
  const buffer = new ArrayBuffer(IDF1_HEADER_BYTES + positions.length * 4);
  const bytes = new Uint8Array(buffer);
  bytes.set([0x49, 0x44, 0x46, 0x31]); // IDF1
  const view = new DataView(buffer);
  view.setUint32(4, id, true);
  view.setFloat64(8, t, true);
  view.setUint32(16, vertexCount, true);
  view.setUint32(20, reserved, true);
  positions.forEach((value, index) => view.setFloat32(IDF1_HEADER_BYTES + index * 4, value, true));
  return buffer;
}

test("decodes an IDF1 frame", () => {
  const decoded = decodeDeformableFrame(idf1());
  assert.equal(decoded.id, 7);
  assert.equal(decoded.t, 1.25);
  assert.equal(decoded.vertexCount, 2);
  assert.deepEqual(Array.from(decoded.positions), [1, 2, 3, 4, 5, 6]);
});

test("rejects malformed IDF1 headers and payloads", () => {
  const wrongMagic = idf1();
  new Uint8Array(wrongMagic)[0] = 0;
  assert.throws(() => decodeDeformableFrame(wrongMagic), /invalid IDF1 magic/);
  assert.throws(() => decodeDeformableFrame(idf1({ reserved: 1 })), /reserved value 1/);

  const truncated = idf1().slice(0, -4);
  assert.throws(() => decodeDeformableFrame(truncated), /expected exactly/);

  const notFinite = idf1();
  new DataView(notFinite).setFloat32(IDF1_HEADER_BYTES, Number.NaN, true);
  assert.throws(() => decodeDeformableFrame(notFinite), /position 0 is not finite/);
});
