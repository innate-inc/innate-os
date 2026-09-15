import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";
import { FireEffect, flameTriangles, type FireState } from "../src/fire.ts";

test("fire animates on the playback clock and disappears on world changes", () => {
  const scene = new THREE.Scene();
  const effect = new FireEffect(scene);
  const state: FireState = {sources: [[-1.3, 1.96, .25, .38, 0]]};
  effect.update(state, 1);
  const group = scene.getObjectByName("blaze-fire")!;
  assert.equal(group.visible, true);
  const mesh = group.children.find(child => child instanceof THREE.Mesh)! as THREE.Mesh;
  const first = Array.from(mesh.geometry.getAttribute("position").array);
  effect.update(state, 1);
  assert.deepEqual(Array.from(mesh.geometry.getAttribute("position").array), first);
  effect.update(state, 1.2);
  assert.notDeepEqual(Array.from(mesh.geometry.getAttribute("position").array), first);
  assert.ok(mesh.geometry.drawRange.count > 0);
  const full: FireState = {sources: Array.from({length: 28}, (_, i) => [i % 7, Math.floor(i/7), 0, 1, i])};
  effect.update(full, 450);
  assert.ok(mesh.geometry.drawRange.count <= mesh.geometry.getAttribute("position").count);
  effect.update(null, 0);
  assert.equal(group.visible, false);
  effect.update(state, 0);
  assert.equal(group.visible, true);
  effect.dispose();
  assert.equal(scene.children.length, 0);
});


test("new fire patches grow from zero instead of appearing at full size", () => {
  const height = (strength: number) => Math.max(...Array.from(
    flameTriangles([0, 0, 0, strength, 0], 1), tri => Math.max(tri[0][2], tri[1][2], tri[2][2]),
  ));
  assert.ok(height(.00001) < .003);
  assert.ok(height(.01) < height(.25));
  assert.ok(height(.25) < height(1));
});
