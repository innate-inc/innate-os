import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";
import { ClothVisualSurface } from "../src/clothVisualSurface.ts";

function patch() {
  const g = new THREE.BufferGeometry();
  g.setAttribute(
    "position",
    new THREE.Float32BufferAttribute([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0], 3),
  );
  g.setAttribute(
    "uv",
    new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2),
  );
  g.setIndex([0, 1, 2, 0, 2, 3]);
  return g;
}

test("wool thickness is bounded, independent and never accumulates", () => {
  const g = patch();
  const cotton = new ClothVisualSurface(g);
  const wool = new ClothVisualSurface(g, 2, 0.0008);
  const first = wool.geometry.attributes.position.array.slice();
  for (let i = 0; i < 10; i++) wool.update();
  assert.deepEqual(wool.geometry.attributes.position.array, first);
  assert.equal(cotton.geometry.attributes.position.getZ(0), 0);
  assert.ok(Math.abs(wool.geometry.attributes.position.getZ(0) - .0008) < 1e-8);
  assert.equal(g.attributes.position.getZ(0), 0);
  const n = wool.geometry.attributes.position.count / 2;
  assert.ok(Math.abs(wool.geometry.attributes.position.getZ(n) + .0008) < 1e-8);
  const edges = new Map();
  const idx = wool.geometry.index.array;
  for (let f = 0; f < idx.length; f += 3) {
    for (let j = 0; j < 3; j++) {
      const a = idx[f+j], b = idx[f+(j+1)%3];
      const key = [a,b].sort((x,y)=>x-y).join(":");
      const e = edges.get(key) ?? [0,0];
      e[0]++; e[1] += a < b ? 1 : -1;
      edges.set(key,e);
    }
  }
  assert.ok([...edges.values()].every(([count,orientation])=>count===2 && orientation===0));
  assert.ok(wool.geometry.attributes.normal.array.every(Number.isFinite));
  for (const offset of [-1, NaN, .003])
    assert.throws(() => new ClothVisualSurface(g, 2, offset));
});

test("invalid topology fails explicitly instead of building corrupt buffers", () => {
  const g = patch();
  g.setIndex([0, 1, 5]);
  assert.throws(() => new ClothVisualSurface(g), /indices/);
  g.setIndex([0, 1, 1]);
  assert.throws(() => new ClothVisualSurface(g), /Degenerate/);
  g.setIndex([0, 1, 2, 0, 1, 3, 0, 1, 2]);
  assert.throws(() => new ClothVisualSurface(g), /Non-manifold/);
  assert.throws(() => new ClothVisualSurface(patch(), 3), /levels/);
});

test("subdivision rounds boundary, keeps an open manifold and never changes controls", () => {
  const g = patch(),
    before = g.attributes.position.array.slice();
  const s = new ClothVisualSurface(g);
  assert.deepEqual(g.attributes.position.array, before);
  assert.equal(s.geometry.index.count, 2 * 16 * 3);
  assert.ok(s.geometry.attributes.position.getX(0) > 0);
  assert.ok(s.geometry.attributes.position.getY(0) > 0);
  const edges = new Map();
  const idx = s.geometry.index.array;
  for (let i = 0; i < idx.length; i += 3)
    for (let j = 0; j < 3; j++) {
      const a = idx[i + j],
        b = idx[i + ((j + 1) % 3)],
        k = [a, b].sort((a, b) => a - b).join(":");
      edges.set(k, (edges.get(k) ?? 0) + 1);
    }
  assert.equal([...edges.values()].filter((n) => n === 1).length, 16);
  assert.ok([...edges.values()].every((n) => n === 1 || n === 2));
  for (let i = 0; i < s.geometry.attributes.uv.count; i++) {
    assert.ok(
      Math.abs(
        s.geometry.attributes.uv.getX(i) -
          s.geometry.attributes.position.getX(i),
      ) < 1e-6,
    );
  }
});

test("folded updates, translation and reset reuse geometry with finite normals", () => {
  const g = patch(),
    s = new ClothVisualSurface(g),
    p = g.attributes.position;
  const initial = s.geometry.attributes.position.array.slice();
  const output = s.geometry.attributes.position.array;
  p.setZ(2, 0.8);
  s.update();
  assert.notDeepEqual(output, initial);
  assert.ok(s.geometry.attributes.normal.array.every(Number.isFinite));
  p.setZ(2, 0);
  for (let i = 0; i < p.count; i++)
    p.setXYZ(i, p.getX(i) + 7, p.getY(i) - 2, p.getZ(i) + 3);
  s.update();
  for (let i = 0; i < output.length; i++)
    assert.ok(Math.abs(output[i] - initial[i] - [7, -2, 3][i % 3]) < 1e-6);
  g.translate(-7, 2, -3);
  s.update();
  assert.deepEqual(output, initial);
  assert.equal(s.geometry.attributes.position.array, output);
});

test("coincident disconnected panels are not welded", () => {
  const g = new THREE.BufferGeometry();
  g.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(
      [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0],
      3,
    ),
  );
  g.setIndex([0, 1, 2, 3, 4, 5]);
  const s = new ClothVisualSurface(g),
    before = s.geometry.attributes.position.array.slice();
  for (let i = 3; i < 6; i++) g.attributes.position.setZ(i, 10);
  s.update();
  assert.equal(s.geometry.attributes.position.getZ(0), 0);
  assert.equal(s.geometry.attributes.position.getZ(3), 10);
  assert.notDeepEqual(s.geometry.attributes.position.array, before);
});
