// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Generate matching browser GLBs (with an Idle clip) and MuJoCo OBJ + palette
// textures (standing pose). Usage: node tools/build-residents.mjs [output-root]
// output-root contains humans/ and models/; defaults to /out in the asset image.
import * as THREE from "three";
import { GLTFExporter } from "three/addons/exporters/GLTFExporter.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { deflateSync } from "node:zlib";
import {
  createResident,
  RESIDENTS,
  IDLE_DURATION,
} from "./residents/model.mjs";

// GLTFExporter only needs this browser API to turn its binary Blob into bytes.
globalThis.FileReader = class {
  readAsArrayBuffer(blob) {
    blob.arrayBuffer().then((result) => {
      this.result = result;
      this.onloadend?.();
    });
  }
};

const out = resolve(process.argv[2] ?? "/out");
mkdirSync(`${out}/humans`, { recursive: true });
mkdirSync(`${out}/models`, { recursive: true });

function mergeStaticParts(root, person) {
  const dynamic = new Set([person.chest]);
  for (const limb of [...person.arms, ...person.legs]) {
    for (const value of Object.values(limb))
      if (value instanceof THREE.Object3D) dynamic.add(value);
  }
  // Merge the hundreds of static hair/face details by material, but preserve
  // head, eyelid and gaze groups and every animated limb as separate nodes.
  function visit(parent) {
    for (const child of [...parent.children]) visit(child);
    const buckets = new Map();
    for (const child of parent.children) {
      if (!child.isMesh || dynamic.has(child) || child.children.length)
        continue;
      const parts = buckets.get(child.material) ?? [];
      parts.push(child);
      buckets.set(child.material, parts);
    }
    for (const [material, parts] of buckets) {
      if (parts.length < 2) continue;
      const geometries = parts.map((part) => {
        part.updateMatrix();
        return part.geometry.clone().applyMatrix4(part.matrix);
      });
      const geometry = mergeGeometries(geometries);
      if (!geometry) throw new Error("Resident geometry could not be merged");
      parent.add(new THREE.Mesh(geometry, material));
      parts.forEach((part) => {
        parent.remove(part);
        part.geometry.dispose();
      });
      geometries.forEach((geo) => geo.dispose());
    }
  }
  visit(root);
  let i = 0;
  root.traverse((node) => {
    node.name = `${person.id}_${i++}`;
  });
}

function idleClip(root, pose) {
  const nodes = [];
  root.traverse((node) => nodes.push(node));
  const samples = nodes.map(() => ({
    position: [],
    quaternion: [],
    scale: [],
  }));
  const times = [];
  const fps = 30;
  for (let frame = 0; frame <= IDLE_DURATION * fps; frame++) {
    const time = frame / fps;
    times.push(time);
    pose(time);
    nodes.forEach((node, i) => {
      for (const key of ["position", "quaternion", "scale"])
        samples[i][key].push(...node[key].toArray());
    });
  }
  const tracks = [];
  nodes.forEach((node, i) => {
    for (const key of ["position", "quaternion", "scale"]) {
      const values = samples[i][key],
        size = key === "quaternion" ? 4 : 3;
      if (values.every((value, j) => Math.abs(value - values[j % size]) < 1e-8))
        continue;
      const Track =
        key === "quaternion"
          ? THREE.QuaternionKeyframeTrack
          : THREE.VectorKeyframeTrack;
      tracks.push(new Track(`${node.name}.${key}`, times, values).optimize());
    }
  });
  pose(0);
  return new THREE.AnimationClip("Idle", IDLE_DURATION, tracks);
}

// Tiny lossless sRGB palette PNG, one color per 8x8 swatch. The MuJoCo OBJ
// points each material's triangles at a swatch center, avoiding atlas bleed.
function palettePNG(colors) {
  const width = colors.length * 8,
    height = 8;
  const raw = Buffer.alloc(height * (1 + width * 3));
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++) {
      colors[Math.floor(x / 8)].forEach((v, c) => {
        raw[y * (1 + width * 3) + 1 + x * 3 + c] = v;
      });
    }
  function chunk(type, bytes) {
    const payload = Buffer.concat([Buffer.from(type), bytes]);
    let crc = 0xffffffff;
    for (const byte of payload) {
      crc ^= byte;
      for (let i = 0; i < 8; i++)
        crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
    const result = Buffer.alloc(bytes.length + 12);
    result.writeUInt32BE(bytes.length);
    payload.copy(result, 4);
    result.writeUInt32BE((crc ^ 0xffffffff) >>> 0, result.length - 4);
    return result;
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function writePhysicsModel(root, id) {
  root.updateMatrixWorld(true);
  const meshes = [],
    materials = [];
  root.traverse((node) => {
    if (!node.isMesh) return;
    meshes.push(node);
    if (!materials.includes(node.material)) materials.push(node.material);
  });
  const lines = [
    "# Generated by sim/viewer/tools/build-residents.mjs; metres, Z-up, facing +Y.",
  ];
  materials.forEach((_, i) =>
    lines.push(`vt ${(i + 0.5) / materials.length} 0.5`),
  );
  let offset = 1;
  for (const mesh of meshes) {
    const geo = mesh.geometry.clone().applyMatrix4(mesh.matrixWorld);
    const p = geo.attributes.position,
      n = geo.attributes.normal;
    for (let i = 0; i < p.count; i++) {
      lines.push(
        `v ${p.getX(i).toFixed(6)} ${p.getY(i).toFixed(6)} ${p.getZ(i).toFixed(6)}`,
      );
      lines.push(
        `vn ${n.getX(i).toFixed(6)} ${n.getY(i).toFixed(6)} ${n.getZ(i).toFixed(6)}`,
      );
    }
    const uv = materials.indexOf(mesh.material) + 1;
    for (let i = 0; i < (geo.index?.count ?? p.count); i += 3) {
      const indices = [0, 1, 2].map(
        (k) => (geo.index ? geo.index.getX(i + k) : i + k) + offset,
      );
      lines.push(`f ${indices.map((v) => `${v}/${uv}/${v}`).join(" ")}`);
    }
    offset += p.count;
    geo.dispose();
  }
  writeFileSync(`${out}/humans/resident_${id}.obj`, lines.join("\n") + "\n");
  const colors = materials.map((m) =>
    m.color
      .clone()
      .convertLinearToSRGB()
      .toArray()
      .map((v) => Math.round(v * 255)),
  );
  writeFileSync(
    `${out}/humans/resident_${id}_basecolor.png`,
    palettePNG(colors),
  );
}

for (const [index, id] of RESIDENTS.entries()) {
  const { root, person, pose } = createResident(index);
  mergeStaticParts(root, person);
  const clip = idleClip(root, pose);
  writePhysicsModel(root, id);
  const binary = await new GLTFExporter().parseAsync(root, {
    binary: true,
    animations: [clip],
    onlyVisible: false,
  });
  writeFileSync(`${out}/models/resident_${id}.glb`, Buffer.from(binary));
  console.log(
    `${id}: ${(binary.byteLength / 1e6).toFixed(2)} MB GLB, ${clip.tracks.length} idle tracks`,
  );
}
