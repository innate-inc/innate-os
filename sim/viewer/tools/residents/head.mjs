// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Anatomical head surfaces, rather than separate balls for the nose and jaw.
import * as THREE from "three";

const V = (x, y, z) => new THREE.Vector3(x, y, z);
const gaussian = (value, center, width) =>
  Math.exp(-(((value - center) / width) ** 2));
const profiles = [
  {
    width: 0.101,
    height: 0.149,
    depth: 0.088,
    jaw: 0.2,
    eye: 0.035,
    nose: 0.03,
    bridge: 0.01,
    iris: "#596e59",
  },
  {
    width: 0.108,
    height: 0.153,
    depth: 0.094,
    jaw: 0.1,
    eye: 0.038,
    nose: 0.031,
    bridge: 0.014,
    iris: "#66503b",
  },
  {
    width: 0.102,
    height: 0.158,
    depth: 0.09,
    jaw: 0.15,
    eye: 0.036,
    nose: 0.036,
    bridge: 0.011,
    iris: "#72796e",
  },
];

export function createHead(index, skin, hair, parent) {
  const p = profiles[index],
    head = new THREE.Group();
  head.position.y = 0.75;
  head.scale.setScalar(0.86);
  head.userData.part = "head";
  parent.add(head);
  const surfaceSkin = skin.clone();
  surfaceSkin.roughness = 0.62;
  const faceSkin = surfaceSkin.clone();
  const tinted = (base, tint, amount, roughness = 0.8) => {
    const material = base.clone();
    material.color.lerp(new THREE.Color(tint), amount);
    material.roughness = roughness;
    return material;
  };
  const lips = tinted(skin, index === 1 ? "#875950" : "#b57872", 0.4, 0.62);
  const crease = tinted(skin, "#49342e", 0.34);
  const brow = tinted(hair, "#65564b", 0.12);
  const eyeWhite = new THREE.MeshStandardMaterial({
    color: "#d9d7ce",
    roughness: 0.35,
  });
  const iris = new THREE.MeshStandardMaterial({
    color: p.iris,
    roughness: 0.38,
  });
  const pupil = new THREE.MeshStandardMaterial({
    color: "#151914",
    roughness: 0.3,
  });
  const catchlight = new THREE.MeshBasicMaterial({ color: "#e9e8e1" });
  function mesh(geometry, material, group = head) {
    const object = new THREE.Mesh(geometry, material);
    object.castShadow = true;
    object.receiveShadow = true;
    group.add(object);
    return object;
  }
  function ellipsoid(x, y, z, rx, ry, rz, material, group = head) {
    const object = mesh(new THREE.SphereGeometry(1, 32, 24), material, group);
    object.position.set(x, y, z);
    object.scale.set(rx, ry, rz);
    return object;
  }
  function curve(points, radius, material, group = head) {
    return mesh(
      new THREE.TubeGeometry(
        new THREE.CatmullRomCurve3(points.map((a) => V(...a))),
        32,
        radius,
        6,
        false,
      ),
      material,
      group,
    );
  }
  const jaw = (y) =>
    (1 - p.jaw * Math.max(0, (-y / p.height - 0.18) / 0.82)) *
    (1 + 0.06 * gaussian(y, 0.055, 0.065));
  // The same depth function positions every feature on the cheek/jaw surface.
  // Recessed eye sockets, brow ridge, nasal bridge/alae and lips blend into one mesh.
  function depth(x, y) {
    const radial = Math.sqrt(
      Math.max(0, 1 - (x / (p.width * jaw(y))) ** 2 - (y / p.height) ** 2),
    );
    let relief = 0.01 * gaussian(x, 0, p.bridge) * gaussian(y, 0.014, 0.043);
    relief +=
      p.nose * gaussian(x, 0, p.bridge * 1.12) * gaussian(y, -0.02, 0.016);
    relief +=
      0.01 * gaussian(Math.abs(x), 0.012, 0.007) * gaussian(y, -0.03, 0.009);
    relief -=
      0.004 * gaussian(Math.abs(x), 0.012, 0.004) * gaussian(y, -0.033, 0.003);
    relief +=
      0.006 * gaussian(Math.abs(x), 0.046, 0.025) * gaussian(y, -0.02, 0.029);
    relief -=
      0.012 * gaussian(Math.abs(x), p.eye, 0.024) * gaussian(y, 0.013, 0.016);
    relief +=
      0.007 * gaussian(Math.abs(x), p.eye, 0.028) * gaussian(y, 0.038, 0.014);
    relief += 0.01 * gaussian(x, 0, 0.035) * gaussian(y, -0.062, 0.025);
    relief += 0.011 * gaussian(x, 0, 0.032) * gaussian(y, -0.108, 0.019);
    relief -= 0.003 * gaussian(x, 0, 0.02) * gaussian(y, -0.089, 0.006);
    if (index === 2) {
      for (const line of [0.068, 0.087])
        relief -=
          0.00065 *
          gaussian(y, line + 0.009 * (x / 0.06) ** 2, 0.0013) *
          gaussian(x, 0, 0.057);
      relief -=
        0.0012 *
        gaussian(Math.abs(x), 0.022 + Math.max(0, -y - 0.032) * 0.4, 0.003) *
        gaussian(y, -0.056, 0.025);
    }
    return p.depth * Math.pow(radial, 0.72) + relief * Math.min(1, radial * 5);
  }
  const face = new THREE.SphereGeometry(1, 128, 96),
    positions = face.attributes.position;
  for (let i = 0; i < positions.count; i++) {
    const yn = positions.getY(i),
      y = yn * p.height,
      x = positions.getX(i) * p.width * jaw(y),
      front = positions.getZ(i);
    const z =
      front >= 0
        ? depth(x, y)
        : front * p.depth * (1 + 0.06 * gaussian(yn, 0.28, 0.5));
    positions.setXYZ(i, x, y, z);
  }
  const skinColors = [];
  for (let i = 0; i < positions.count; i++) {
    const x = positions.getX(i),
      y = positions.getY(i),
      z = positions.getZ(i),
      warm =
        z > 0
          ? gaussian(Math.abs(x), 0.043, 0.027) * gaussian(y, -0.022, 0.037)
          : 0;
    skinColors.push(1 - 0.005 * warm, 1 - 0.04 * warm, 1 - 0.055 * warm);
  }
  face.setAttribute("color", new THREE.Float32BufferAttribute(skinColors, 3));
  faceSkin.vertexColors = true;
  face.computeVertexNormals();
  mesh(face, faceSkin);

  // Small ears with a shaped helix and an inset concha, tucked behind the jaw.
  for (const side of [-1, 1]) {
    ellipsoid(
      side * (p.width - 0.001),
      -0.011,
      -0.005,
      0.012,
      0.026,
      0.017,
      surfaceSkin,
    );
    ellipsoid(
      side * (p.width + 0.007),
      -0.01,
      0.006,
      0.0045,
      0.014,
      0.005,
      crease,
    );
    const points = [];
    for (let i = 0; i <= 24; i++) {
      const a = -1.1 + (i * 5.6) / 24;
      points.push([
        side * (p.width + 0.006 + Math.cos(a) * 0.007),
        -0.009 + Math.sin(a) * 0.022,
        0.01,
      ]);
    }
    curve(points, 0.0018, surfaceSkin);
    ellipsoid(
      side * (p.width + 0.001),
      -0.014,
      0.012,
      0.005,
      0.008,
      0.005,
      surfaceSkin,
    );
  }

  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group(),
      ex = side * p.eye,
      ey = 0.013,
      ez = depth(ex, ey) + 0.002;
    eye.position.set(ex, ey, ez);
    head.add(eye);
    const width = 0.02,
      upper = 0.007,
      lower = 0.005,
      radius = 0.022;
    const shell = (x, y) =>
      Math.sqrt(Math.max(0, radius * radius - x * x - y * y)) - 0.016;
    const vertices = [],
      indices = [];
    const rows = 8,
      cols = 48;
    for (let u = 0; u <= cols; u++) {
      const f = u / cols,
        x = (f * 2 - 1) * width,
        arch = Math.sin(Math.PI * f),
        tilt = side * x * 0.06;
      for (let v = 0; v <= rows; v++) {
        const y = (-lower + ((upper + lower) * v) / rows) * arch + tilt;
        vertices.push(x, y, shell(x, y));
      }
    }
    for (let u = 0; u < cols; u++)
      for (let v = 0; v < rows; v++) {
        const a = u * (rows + 1) + v,
          b = a + rows + 1;
        indices.push(a, b, a + 1, b, b + 1, a + 1);
      }
    const whiteGeometry = new THREE.BufferGeometry();
    whiteGeometry.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(vertices, 3),
    );
    whiteGeometry.setIndex(indices);
    whiteGeometry.computeVertexNormals();
    mesh(whiteGeometry, eyeWhite, eye);
    const gaze = new THREE.Group();
    eye.add(gaze);
    ellipsoid(0, 0, 0.0066, 0.0068, 0.0068, 0.001, pupil, gaze);
    ellipsoid(0, 0, 0.0074, 0.0061, 0.0061, 0.0008, iris, gaze);
    ellipsoid(0, 0, 0.0082, 0.0026, 0.0028, 0.0005, pupil, gaze);
    ellipsoid(
      -0.0015,
      0.0023,
      0.0088,
      0.0007,
      0.0007,
      0.0003,
      catchlight,
      gaze,
    );
    for (const sign of [-1, 1]) {
      const points = [];
      for (let j = 0; j <= 32; j++) {
        const f = j / 32,
          x = (f * 2 - 1) * width,
          y =
            Math.sin(Math.PI * f) * (sign === 1 ? upper : -lower) +
            side * x * 0.06;
        points.push([x, y, shell(x, y) + 0.0005]);
      }
      curve(points, sign === 1 ? 0.0013 : 0.001, surfaceSkin, eye);
    }
    // Eyelid fold and fine tapered eyebrows conform to the head surface.
    const fold = [];
    for (let j = 0; j <= 16; j++) {
      const x = ex + ((j / 16) * 2 - 1) * 0.018,
        y = ey + 0.011 + 0.003 * Math.sin((Math.PI * j) / 16);
      fold.push([x, y, depth(x, y) + 0.0004]);
    }
    curve(fold, 0.00038, crease);
    const browVertices = [],
      browIndices = [];
    for (let j = 0; j <= 32; j++) {
      const u = j / 32,
        x = ex + (u * 2 - 1) * 0.026,
        y = 0.04 + 0.005 * Math.sin(Math.PI * u) + side * (u - 0.5) * -0.004,
        w = 0.001 + Math.sin(Math.PI * u) * [0.0011, 0.0018, 0.0008][index];
      for (const d of [-w, w])
        browVertices.push(x, y + d, depth(x, y + d) + 0.001);
      if (j < 32) {
        const k = j * 2;
        browIndices.push(k, k + 2, k + 1, k + 2, k + 3, k + 1);
      }
    }
    const browGeo = new THREE.BufferGeometry();
    browGeo.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(browVertices, 3),
    );
    browGeo.setIndex(browIndices);
    browGeo.computeVertexNormals();
    mesh(browGeo, brow);
    eyes.push({ eye, gaze });
  }
  // The nose is part of the face mesh. Only the nostril recesses are separate.
  for (const side of [-1, 1]) {
    const x = side * 0.011,
      y = -0.033;
    const nostril = ellipsoid(
      x,
      y,
      depth(x, y) + 0.0006,
      0.003,
      0.0012,
      0.0012,
      crease,
    );
    nostril.rotation.z = side * 0.25;
  }
  // Filled upper/lower lip surfaces, a subtle cupid's bow and mouth opening.
  for (const sign of [-1, 1]) {
    const verts = [],
      idx = [],
      cols = 48,
      rows = 6;
    for (let i = 0; i <= cols; i++) {
      const u = i / cols,
        x = (u * 2 - 1) * 0.027,
        envelope = Math.sin(Math.PI * u),
        center = -0.063 + 0.0015 * (Math.abs(x) / 0.027) ** 2;
      const thickness =
        sign === 1
          ? 0.0042 + 0.0012 * gaussian(Math.abs(x), 0.007, 0.004)
          : 0.0055;
      for (let j = 0; j <= rows; j++) {
        const v = j / rows,
          y = center + sign * thickness * envelope * v,
          z = depth(x, y) + 0.0006 + 0.0017 * Math.sin(Math.PI * v) * envelope;
        verts.push(x, y, z);
      }
    }
    for (let i = 0; i < cols; i++)
      for (let j = 0; j < rows; j++) {
        const a = i * (rows + 1) + j,
          b = a + rows + 1;
        const tri = [a, b, a + 1, b, b + 1, a + 1];
        idx.push(...(sign === 1 ? tri : [a, a + 1, b, b, a + 1, b + 1]));
      }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
    geo.setIndex(idx);
    geo.computeVertexNormals();
    mesh(geo, lips);
  }
  const mouth = [];
  for (let i = 0; i <= 32; i++) {
    const x = ((i / 32) * 2 - 1) * 0.026,
      y = -0.063 + 0.0015 * (Math.abs(x) / 0.027) ** 2;
    mouth.push([x, y, depth(x, y) + 0.0009]);
  }
  curve(mouth, 0.00045, crease);

  // One scalp-conforming hair surface: a bob, short textured curls, or a swept
  // silver cut. Fine ridges replace the original large balls/tubular locks.
  const hairVertices = [],
    hairIndices = [],
    around = 128,
    down = 64;
  const hairPoint = (phi, t) => {
    const front = Math.max(0, Math.sin(phi)),
      back = Math.max(0, -Math.sin(phi));
    let limit =
      index === 0
        ? 2.42 - 1.43 * Math.pow(front, 0.65)
        : 1.6 - 0.58 * Math.pow(front, 3) + 0.25 * back;
    if (index === 2)
      limit -= 0.13 * Math.pow(Math.abs(Math.cos(phi)) * front, 0.6);
    const theta = t * limit,
      radial =
        index === 0 && theta > Math.PI / 2
          ? Math.max(Math.sin(theta), 0.82)
          : Math.sin(theta);
    let y = Math.cos(theta) * p.height,
      x = Math.cos(phi) * radial * p.width * jaw(y);
    let z =
      Math.sin(phi) >= 0
        ? Math.max(Math.sin(phi) * radial * p.depth, depth(x, y))
        : Math.sin(phi) *
          radial *
          p.depth *
          (1 + 0.06 * gaussian(y / p.height, 0.28, 0.5));
    const noise =
      index === 1
        ? 0.0012 *
          Math.sin(phi * 59 + theta * 31) *
          Math.sin(theta * 73 - phi * 11)
        : 0.00055 * Math.sin(phi * 91 + theta * 13);
    const shell =
      0.0035 + (index === 0 ? 0.004 : index === 1 ? 0.003 : 0.002) + noise;
    x += Math.cos(phi) * Math.sin(theta) * shell;
    z += Math.sin(phi) * Math.sin(theta) * shell;
    y += Math.cos(theta) * shell;
    if (index === 2) y += 0.003 * Math.sin(theta * 2);
    return [x, y, z];
  };
  for (let i = 0; i <= around; i++)
    for (let j = 0; j <= down; j++)
      hairVertices.push(...hairPoint((i / around) * Math.PI * 2, j / down));
  for (let i = 0; i < around; i++)
    for (let j = 0; j < down; j++) {
      const a = i * (down + 1) + j,
        b = a + down + 1;
      hairIndices.push(a, b, a + 1, b, b + 1, a + 1);
    }
  const hairGeo = new THREE.BufferGeometry();
  hairGeo.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(hairVertices, 3),
  );
  hairGeo.setIndex(hairIndices);
  hairGeo.computeVertexNormals();
  mesh(hairGeo, hair);
  if (index === 0) {
    const freckles = tinted(skin, "#86604c", 0.35);
    for (const side of [-1, 1])
      for (let i = 0; i < 17; i++) {
        const x = side * (0.026 + (((i * 13) % 29) / 29) * 0.032),
          y = -0.004 - (((i * 19) % 31) / 31) * 0.023,
          r = 0.00035 + (i % 3) * 0.00013;
        ellipsoid(x, y, depth(x, y) + 0.00035, r, r * 0.8, 0.0003, freckles);
      }
  } else if (index === 1) {
    const verts = [],
      indices = [],
      colors = [],
      cols = 96,
      rows = 32;
    for (let i = 0; i <= cols; i++) {
      const x = ((i / cols) * 2 - 1) * p.width * 0.77,
        top = -0.041 - 0.052 * gaussian(x, 0, 0.039),
        bottom = -p.height * Math.sqrt(1 - (x / (p.width * 0.88)) ** 2) + 0.01;
      for (let j = 0; j <= rows; j++) {
        const y = bottom + ((top - bottom) * j) / rows;
        verts.push(
          x,
          y,
          depth(x, y) +
            0.0012 +
            0.00022 * Math.sin(x * 2200) * Math.cos(y * 1900),
        );
        const edge = Math.min(1, (j / rows) * 10, (1 - j / rows) * 10),
          blend = 0.86 * edge * Math.pow(Math.sin((Math.PI * i) / cols), 0.22);
        colors.push(...skin.color.clone().lerp(hair.color, blend).toArray());
      }
    }
    for (let i = 0; i < cols; i++)
      for (let j = 0; j < rows; j++) {
        const a = i * (rows + 1) + j,
          b = a + rows + 1;
        indices.push(a, b, a + 1, b, b + 1, a + 1);
      }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
    geo.setIndex(indices);
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    geo.computeVertexNormals();
    mesh(
      geo,
      new THREE.MeshStandardMaterial({
        color: 0xffffff,
        vertexColors: true,
        roughness: 1,
      }),
    );
    for (const side of [-1, 1]) {
      const points = [];
      for (let j = 0; j <= 16; j++) {
        const x = side * (0.004 + (j / 16) * 0.02),
          y = -0.052 - (0.005 * j) / 16;
        points.push([x, y, depth(x, y) + 0.0008]);
      }
      curve(points, 0.0015, brow);
    }
  } else {
    const frames = new THREE.MeshStandardMaterial({
      color: "#4d4941",
      metalness: 0.5,
      roughness: 0.4,
    });
    for (const side of [-1, 1]) {
      const ex = side * p.eye,
        points = [];
      for (let j = 0; j <= 64; j++) {
        const a = (j / 64) * Math.PI * 2;
        points.push([
          ex + 0.026 * Math.cos(a),
          0.014 + 0.02 * Math.sin(a),
          0.095 - 0.007 * Math.cos(a) * side,
        ]);
      }
      curve(points, 0.0013, frames);
      curve(
        [
          [side * 0.062, 0.014, 0.09],
          [side * 0.099, 0.01, 0.046],
          [side * 0.107, -0.002, -0.013],
        ],
        0.0012,
        frames,
      );
    }
    curve(
      [
        [-0.01, 0.017, 0.098],
        [0, 0.024, 0.105],
        [0.01, 0.017, 0.098],
      ],
      0.0012,
      frames,
    );
  }
  return { head, eyes };
}
