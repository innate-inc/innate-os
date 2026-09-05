// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { createServer } from "node:http";
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { PropLibrary, type PropInfo } from "../src/props";

const output = mkdtempSync(resolve(tmpdir(), "resident-assets-"));
const ids = ["alex", "blake", "casey"];
let baseURL: string;
let releaseDownload: (() => void) | undefined;
let downloadStarted: (() => void) | undefined;
const server = createServer(async (req, res) => {
  const id = ids.find((id) => req.url?.split("?")[0] === `/resident_${id}.glb`);
  if (!id) {
    res.writeHead(404).end();
    return;
  }
  const data = readFileSync(`${output}/models/resident_${id}.glb`);
  if (req.url?.includes("hold"))
    await new Promise<void>((resolve) => {
      releaseDownload = resolve;
      downloadStarted?.();
    });
  res.writeHead(200, { "Content-Length": data.length });
  res.end(data);
});

before(async () => {
  // Three's fetch loader reports browser-style progress in Node too.
  globalThis.ProgressEvent = class extends Event {} as typeof ProgressEvent;
  execFileSync(process.execPath, ["tools/build-residents.mjs", output], {
    cwd: resolve(import.meta.dirname, ".."),
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  baseURL = `http://127.0.0.1:${address.port}`;
});
after(async () => {
  await new Promise<void>((done) => server.close(() => done()));
  rmSync(output, { recursive: true, force: true });
});

function info(id: string): PropInfo {
  return {
    name: `resident_${id}`,
    title: id,
    label: id[0],
    group: null,
    collision: "hull",
    size: [0.3, 0.2, 0.9],
    wall: 0.01,
    rgba: [0.5, 0.5, 0.5, 1],
    viewer: {
      glb: `${baseURL}/resident_${id}.glb`,
      preNormalized: true,
      idleAnimation: "Idle",
    },
  };
}
function matrices(root: THREE.Object3D): number[] {
  root.updateMatrixWorld(true);
  const values: number[] = [];
  root.traverse((node) => values.push(...node.matrixWorld.elements));
  return values;
}

test("generated residents share the MuJoCo frame and have seamless, planted-foot idle clips", async () => {
  for (const [i, id] of ids.entries()) {
    const { scene, animations } = await new GLTFLoader().loadAsync(
      info(id).viewer.glb!,
    );
    const box = new THREE.Box3().setFromObject(scene, true);
    assert.ok(Math.abs(box.min.z) < 1e-5, `${id} feet must start on the floor`);
    assert.ok(Math.abs(box.max.z - [1.68, 1.82, 1.7][i]) < 0.002);
    let head: THREE.Object3D | undefined;
    let hasSkinColor = false;
    const flatColors = new Set<string>();
    scene.traverse((node) => {
      if (node.userData.part === "head") head = node;
      if (node instanceof THREE.Mesh) {
        const materials = Array.isArray(node.material)
          ? node.material
          : [node.material];
        for (const material of materials)
          if ("color" in material)
            flatColors.add((material.color as THREE.Color).getHexString());
      }
      if (node instanceof THREE.Mesh && node.geometry.attributes.color)
        hasSkinColor = true;
    });
    assert.ok(head, "head must remain independently articulated");
    const headSize = new THREE.Box3()
      .setFromObject(head, true)
      .getSize(new THREE.Vector3());
    assert.ok(
      headSize.z > 0.22 && headSize.z < 0.29,
      "adult head proportions must survive export",
    );
    assert.ok(hasSkinColor, "subtle skin shading must survive GLB export");
    assert.equal(animations[0].name, "Idle");
    assert.equal(animations[0].duration, 24);
    const baseline = matrices(scene);
    const mixer = new THREE.AnimationMixer(scene);
    mixer.clipAction(animations[0]).play();
    mixer.setTime(1.3);
    assert.notDeepEqual(matrices(scene), baseline);
    assert.ok(matrices(scene).every(Number.isFinite));
    // Feet stay fixed: the lowest geometry vertices remain in the same place.
    const feet = (time: number) => {
      mixer.setTime(time);
      scene.updateMatrixWorld(true);
      const result: number[] = [];
      scene.traverse((node) => {
        if (!(node instanceof THREE.Mesh)) return;
        const p = node.geometry.attributes.position;
        for (let j = 0; j < p.count; j++) {
          const v = new THREE.Vector3()
            .fromBufferAttribute(p, j)
            .applyMatrix4(node.matrixWorld);
          if (v.z < 0.015) result.push(v.x, v.y, v.z);
        }
      });
      return result;
    };
    assert.deepEqual(feet(0), feet(3.7));
    for (const track of animations[0].tracks) {
      const size = track.getValueSize();
      for (let k = 0; k < size; k++)
        assert.ok(
          Math.abs(
            track.values[k] - track.values[track.values.length - size + k],
          ) < 1e-5,
        );
    }
    // The standing OBJ has the same bounds and valid palette texture.
    const vertices = readFileSync(`${output}/humans/resident_${id}.obj`, "utf8")
      .split("\n")
      .filter((line) => line.startsWith("v "))
      .map((line) => line.split(" ").slice(1).map(Number));
    assert.ok(vertices.length > 5000);
    assert.ok(
      Math.abs(Math.max(...vertices.map((v) => v[2])) - box.max.z) < 1e-5,
    );
    assert.equal(
      readFileSync(`${output}/humans/resident_${id}_basecolor.png`)
        .subarray(1, 4)
        .toString(),
      "PNG",
    );
    const obj = readFileSync(`${output}/humans/resident_${id}.obj`, "utf8");
    const uvs = obj.split("\n").filter((line) => line.startsWith("vt "));
    assert.ok(
      uvs.length > flatColors.size,
      "MuJoCo needs the skin-shading colors as well as flat materials",
    );
    assert.ok(
      uvs.every((line) =>
        line
          .split(" ")
          .slice(1)
          .every((v) => Number(v) > 0 && Number(v) < 1),
      ),
    );
  }
});

test("PropLibrary preserves world placement while idling, pauses on held time, and survives removal/re-add", async () => {
  const scene = new THREE.Scene();
  let ready!: () => void;
  const loaded = new Promise<void>((resolve) => {
    ready = resolve;
  });
  const library = new PropLibrary(
    scene,
    new THREE.MeshBasicMaterial(),
    () => {},
    () => ready(),
  );
  library.setManifest([info("alex")]);
  library.prefetchModels();
  await loaded;
  const pose = {
    resident_alex: [4, -2, 0.1, Math.SQRT1_2, 0, 0, Math.SQRT1_2],
  };
  library.setPoses(pose, 0);
  const root = library.visibleRoots[0];
  const initial = matrices(root);
  library.setPoses(pose, 2);
  assert.deepEqual(root.position.toArray(), [4, -2, 0.1]);
  assert.deepEqual(root.quaternion.toArray(), [
    0,
    0,
    Math.SQRT1_2,
    Math.SQRT1_2,
  ]);
  const moved = matrices(root);
  assert.notDeepEqual(moved, initial);
  library.setPoses(pose, 2);
  assert.deepEqual(matrices(root), moved);
  library.showPlacementPreview("resident_alex", 0, 0, 0);
  library.clearPlacementPreview();
  assert.deepEqual(matrices(root), moved);
  library.setPoses({}, 3);
  assert.equal(library.visibleRoots.length, 0);
  library.setPoses(pose, 0);
  assert.deepEqual(matrices(root), initial);
  library.setManifest([]);
  assert.equal(scene.children.length, 0);
  library.setManifest([info("alex")]);
  library.setPoses(pose, 0);
  assert.deepEqual(matrices(library.visibleRoots[0]), initial);
  library.dispose();
});

test("dropping before prefetch waits for the animated model; old bundles without a clip still render", async () => {
  const scene = new THREE.Scene();
  let ready!: () => void;
  const loaded = new Promise<void>((resolve) => {
    ready = resolve;
  });
  const library = new PropLibrary(
    scene,
    new THREE.MeshBasicMaterial(),
    () => {},
    () => ready(),
  );
  const resident = info("casey");
  resident.viewer.idleAnimation = "AbsentInOldBundle";
  library.setManifest([resident]);
  const poses = { resident_casey: [0, 0, 0, 1, 0, 0, 0] };
  library.setPoses(poses, 0);
  assert.equal(library.visibleRoots.length, 0);
  await loaded;
  library.setPoses(poses, 0);
  const initial = matrices(library.visibleRoots[0]);
  library.setPoses(poses, 10);
  assert.deepEqual(matrices(library.visibleRoots[0]), initial);
  library.dispose();
});

test("a late model animates its placeholder replacement but cannot revive a removed/disposed resident", async () => {
  for (const ending of ["active", "removed", "disposed"]) {
    const scene = new THREE.Scene();
    let ready!: () => void,
      changes = 0;
    const loaded = new Promise<void>((resolve) => {
      ready = resolve;
    });
    const library = new PropLibrary(
      scene,
      new THREE.MeshBasicMaterial(),
      () => {
        changes++;
      },
      () => ready(),
    );
    const resident = info("blake");
    resident.viewer.glb += "?hold";
    releaseDownload = undefined;
    const started = new Promise<void>((resolve) => {
      downloadStarted = resolve;
    });
    library.setManifest([resident]);
    const poses = { resident_blake: [0, 0, 0, 1, 0, 0, 0] };
    library.setPoses(poses, 0);
    await started;
    await new Promise((resolve) => setTimeout(resolve, 350));
    assert.ok(releaseDownload);
    library.setPoses(poses, 2);
    assert.equal(
      library.visibleRoots.length,
      1,
      "slow loading must show a placeholder",
    );
    if (ending === "removed") library.setManifest([]);
    if (ending === "disposed") library.dispose();
    const before = changes;
    releaseDownload!();
    await loaded;
    await new Promise((resolve) => setImmediate(resolve));
    if (ending === "active") {
      assert.equal(changes, before + 1);
      const root = library.visibleRoots[0],
        poseAtTwo = matrices(root);
      library.setPoses(poses, 4);
      assert.notDeepEqual(matrices(root), poseAtTwo);
    } else {
      assert.equal(
        changes,
        before,
        "a stale load must not install an animation",
      );
      if (ending === "removed") assert.equal(scene.children.length, 0);
    }
    library.dispose();
  }
});
