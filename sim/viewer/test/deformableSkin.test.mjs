import assert from "node:assert/strict";
import test from "node:test";

import {
  decodeDeformableSkin,
  ISK1_HEADER_BYTES,
  skinDeformablePositions,
} from "../src/deformableSkin.ts";

function isk1({
  renderCount = 1,
  controlCount = 3,
  indices = [0, 1, 2],
  weights = [0.2, 0.3, 0.5],
  offsets = [1, 2, 3],
  reserved = 0,
} = {}) {
  const tupleCount = renderCount * 3;
  const weightsOffset = (ISK1_HEADER_BYTES + tupleCount * 2 + 3) & ~3;
  const offsetsOffset = weightsOffset + tupleCount * 4;
  const buffer = new ArrayBuffer(offsetsOffset + tupleCount * 4);
  new Uint8Array(buffer).set([0x49, 0x53, 0x4b, 0x31]); // ISK1
  const view = new DataView(buffer);
  view.setUint32(4, renderCount, true);
  view.setUint32(8, controlCount, true);
  view.setUint32(12, reserved, true);
  indices.forEach((value, index) => view.setUint16(ISK1_HEADER_BYTES + index * 2, value, true));
  weights.forEach((value, index) => view.setFloat32(weightsOffset + index * 4, value, true));
  offsets.forEach((value, index) => view.setFloat32(offsetsOffset + index * 4, value, true));
  return buffer;
}

test("decodes aligned ISK1 indices, weights, and local offsets", () => {
  const decoded = decodeDeformableSkin(isk1());
  assert.equal(decoded.renderCount, 1);
  assert.equal(decoded.controlCount, 3);
  assert.deepEqual(Array.from(decoded.indices), [0, 1, 2]);
  assert.deepEqual(Array.from(decoded.weights).map((v) => Number(v.toFixed(6))), [0.2, 0.3, 0.5]);
  assert.deepEqual(Array.from(decoded.localOffsets), [1, 2, 3]);
});

test("rejects malformed ISK1 data", () => {
  const wrongIndex = isk1({ indices: [0, 1, 3] });
  assert.throws(() => decodeDeformableSkin(wrongIndex), /exceeds control count 3/);
  assert.throws(() => decodeDeformableSkin(isk1({ reserved: 2 })), /reserved value 2/);
  assert.throws(() => decodeDeformableSkin(isk1().slice(0, -4)), /expected exactly/);
});

test("skins a render vertex in the triangle tangent frame", () => {
  const skin = decodeDeformableSkin(isk1());
  const controls = new Float32Array([
    0, 0, 0,
    1, 0, 0,
    0, 1, 0,
  ]);
  // barycentric point (.3, .5, 0) + 1*tangent1 + 2*tangent2 + 3*normal
  assert.deepEqual(
    Array.from(skinDeformablePositions(skin, controls)).map((v) => Number(v.toFixed(6))),
    [1.3, 2.5, 3],
  );
});
