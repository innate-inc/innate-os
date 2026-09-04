// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Authored household residents. No scans, external textures, or random geometry.
import * as THREE from "three";

export const RESIDENTS = ["alex", "blake", "casey"];
export const IDLE_DURATION = 24;
const PALETTE = {
  "alex-skin": "#d6a080",
  "blake-skin": "#80503a",
  "casey-skin": "#e3bd9e",
  "alex-hair": "#623626",
  "blake-hair": "#211b18",
  "casey-hair": "#9a9993",
  denim: "#384657",
  shoe: "#393630",
  sole: "#ddd5c9",
  "eye-white": "#ece7da",
  pupil: "#211c18",
  lip: "#a36960",
  iris: "#516557",
  blue: "#398dc9",
  green: "#368764",
  purple: "#8856b2",
};
const V = (x = 0, y = 0, z = 0) => new THREE.Vector3(x, y, z),
  up = V(0, 1, 0);

export function createResident(index) {
  if (!Number.isInteger(index) || !RESIDENTS[index])
    throw new Error("Unknown resident");
  const scene = new THREE.Group();
  function mat(token) {
    return new THREE.MeshStandardMaterial({
      color: PALETTE[token],
      roughness: 0.85,
    });
  }
  const trousers = mat("denim"),
    shoes = mat("shoe"),
    sole = mat("sole"),
    eyeWhite = mat("eye-white"),
    pupil = mat("pupil"),
    lip = mat("lip"),
    iris = mat("iris");
  const shirtMats = [mat("blue"), mat("green"), mat("purple")];
  function mesh(geo, m, parent = scene) {
    const o = new THREE.Mesh(geo, m);
    o.castShadow = true;
    o.receiveShadow = true;
    parent.add(o);
    return o;
  }
  function ball(x, y, z, rx, ry, rz, m, parent = scene) {
    const o = mesh(new THREE.SphereGeometry(1, 20, 14), m, parent);
    o.position.set(x, y, z);
    o.scale.set(rx, ry, rz);
    return o;
  }
  function capsule(radius, m) {
    return mesh(new THREE.CapsuleGeometry(radius, 1, 5, 12), m);
  }
  function segment(o, a, b, r) {
    o.position.copy(a).add(b).multiplyScalar(0.5);
    o.quaternion.setFromUnitVectors(up, b.clone().sub(a).normalize());
    o.scale.set(1, Math.max(0.001, a.distanceTo(b)) / (1 + 2 * r), 1);
  }
  function createPerson(index) {
    const id = ["alex", "blake", "casey"][index],
      skin = mat(id + "-skin"),
      hair = mat(id + "-hair");
    const shirt = shirtMats[index],
      torso = new THREE.Group();
    scene.add(torso);
    const width = [0.197, 0.235, 0.212][index];
    const chest = mesh(
      new THREE.CylinderGeometry(width, width * 0.78, 0.44, 32),
      shirt,
      torso,
    );
    chest.position.y = 0.3;
    chest.scale.z = 0.64;
    ball(0, 0.04, 0, width * 0.88, 0.12, 0.12, trousers, torso);
    ball(0, 0.6, 0, 0.068, 0.095, 0.067, skin, torso);
    const collar = mesh(
      new THREE.TorusGeometry(0.077, 0.016, 8, 40),
      shirt,
      torso,
    );
    collar.rotation.x = Math.PI / 2;
    collar.position.y = 0.535;
    for (let y of [0.36, 0.43, 0.5])
      ball(0.015, y, 0.134, 0.009, 0.009, 0.005, sole, torso);
    const head = new THREE.Group();
    head.position.y = 0.79;
    torso.add(head);
    const hw = [0.137, 0.153, 0.142][index],
      hh = [0.186, 0.19, 0.205][index];
    // A continuous sculpted face: tapered jaw, cheek volume and a shaped chin.
    const faceGeo = new THREE.SphereGeometry(1, 64, 48),
      pos = faceGeo.attributes.position;
    for (let i = 0; i < pos.count; i++) {
      let x = pos.getX(i),
        y = pos.getY(i),
        z = pos.getZ(i);
      const jaw =
        y < -0.2
          ? 1 - (index === 0 ? 0.23 : 0.14) * Math.min(1, (-y - 0.2) / 0.8)
          : 1;
      let zz = z * 0.132;
      if (z > 0) {
        zz +=
          0.014 *
          Math.exp(-Math.pow((y + 0.19) / 0.27, 2)) *
          Math.pow(Math.abs(x), 0.6);
        zz += 0.01 * Math.exp(-Math.pow((y + 0.73) / 0.19, 2));
      }
      pos.setXYZ(i, x * hw * jaw, y * hh, zz);
    }
    faceGeo.computeVertexNormals();
    mesh(faceGeo, skin, head);
    // Ears, tragus and inset concha.
    for (let side of [-1, 1]) {
      ball(
        side * (hw + 0.004),
        -0.007,
        -0.003,
        0.027,
        0.044,
        0.023,
        skin,
        head,
      );
      ball(side * (hw + 0.013), -0.007, 0.016, 0.011, 0.024, 0.006, lip, head);
      ball(side * (hw + 0.005), -0.015, 0.021, 0.01, 0.012, 0.007, skin, head);
    }
    function stroke(points, r, m, parent = head) {
      return mesh(
        new THREE.TubeGeometry(
          new THREE.CatmullRomCurve3(points.map((p) => V(...p))),
          24,
          r,
          8,
          false,
        ),
        m,
        parent,
      );
    }
    const eyes = [];
    for (let side of [-1, 1]) {
      const eye = new THREE.Group();
      eye.position.set(side * [0.057, 0.063, 0.058][index], 0.032, 0.12);
      head.add(eye);
      ball(0, 0, 0, 0.032, 0.016, 0.015, eyeWhite, eye);
      const gaze = new THREE.Group();
      eye.add(gaze);
      ball(0, 0, 0.014, 0.012, 0.012, 0.004, iris, gaze);
      ball(0, 0, 0.018, 0.0055, 0.0065, 0.002, pupil, gaze);
      ball(-0.003, 0.004, 0.02, 0.0022, 0.0022, 0.001, eyeWhite, gaze);
      stroke(
        [
          [-0.033, 0, 0],
          [-0.019, 0.013, 0.008],
          [0, 0.018, 0.01],
          [0.021, 0.011, 0.007],
          [0.033, 0, 0],
        ],
        0.004,
        skin,
        eye,
      );
      stroke(
        [
          [-0.032, 0, 0],
          [-0.017, -0.011, 0.008],
          [0.004, -0.013, 0.009],
          [0.022, -0.008, 0.005],
          [0.032, 0, 0],
        ],
        0.0035,
        skin,
        eye,
      );
      eyes.push({ eye, gaze });
      const x = side * 0.059;
      stroke(
        [
          [x - 0.034, 0.066, 0.118],
          [x - 0.016, 0.074 + (index === 0 ? 0.007 : 0), 0.126],
          [x + 0.01, 0.075, 0.125],
          [x + 0.031, 0.067, 0.114],
        ],
        index === 1 ? 0.006 : 0.0045,
        hair,
      );
      if (index === 2)
        stroke(
          [
            [x - 0.026, 0.006, 0.126],
            [x, 0.002, 0.131],
            [x + 0.028, 0.008, 0.122],
          ],
          0.0018,
          lip,
        );
    }
    // Nose bridge, tip, nostril wings, philtrum, lips and lower-lip shadow.
    ball(0, 0.007, 0.13, index === 1 ? 0.022 : 0.017, 0.044, 0.024, skin, head);
    ball(
      0,
      -0.025,
      0.156,
      index === 1 ? 0.028 : 0.02,
      0.018,
      index === 2 ? 0.03 : 0.024,
      skin,
      head,
    );
    for (let side of [-1, 1]) {
      ball(side * 0.02, -0.033, 0.144, 0.014, 0.011, 0.014, skin, head);
      ball(side * 0.015, -0.039, 0.157, 0.007, 0.0035, 0.005, lip, head);
    }
    stroke(
      [
        [-0.041, -0.076, 0.117],
        [-0.019, -0.071, 0.134],
        [0, -0.074, 0.139],
        [0.019, -0.071, 0.134],
        [0.041, -0.076, 0.117],
      ],
      0.003,
      lip,
    );
    stroke(
      [
        [-0.036, -0.078, 0.123],
        [0, -0.087, 0.138],
        [0.036, -0.078, 0.123],
      ],
      0.004,
      lip,
    );
    if (index === 0) {
      const cap = mesh(
        new THREE.SphereGeometry(1, 40, 28, 0, Math.PI * 2, 0, 1.43),
        hair,
        head,
      );
      cap.scale.set(0.145, 0.197, 0.139);
      cap.position.set(0, 0.004, -0.014);
      for (let side of [-1, 1]) {
        ball(side * 0.123, -0.009, -0.049, 0.045, 0.16, 0.078, hair, head);
        stroke(
          [
            [side * 0.117, 0.114, 0.052],
            [side * 0.138, 0.042, 0.024],
            [side * 0.146, -0.063, -0.014],
            [side * 0.119, -0.146, -0.039],
          ],
          0.013,
          hair,
        );
      }
      stroke(
        [
          [-0.127, 0.115, 0.061],
          [-0.064, 0.16, 0.105],
          [0.011, 0.175, 0.092],
          [0.084, 0.143, 0.085],
        ],
        0.023,
        hair,
      );
      for (let side of [-1, 1])
        for (let j = 0; j < 8; j++) {
          const x = side * (0.056 + (j % 4) * 0.012),
            y = -0.007 - Math.floor(j / 4) * 0.014;
          const z =
            0.132 *
              Math.sqrt(Math.max(0.1, 1 - (x / hw) ** 2 - (y / hh) ** 2)) +
            0.007;
          ball(x, y, z, 0.0017, 0.0015, 0.001, lip, head);
        }
    } else if (index === 1) {
      for (let row = 0; row < 6; row++) {
        const theta = 0.16 + row * 0.205;
        for (let j = 0; j < 18; j++) {
          const a = (j * Math.PI * 2) / 18 + row * 0.27;
          ball(
            0.151 * Math.sin(theta) * Math.cos(a),
            0.192 * Math.cos(theta),
            0.139 * Math.sin(theta) * Math.sin(a) - 0.012,
            0.028,
            0.026,
            0.027,
            hair,
            head,
          );
        }
      }
      // Short, fitted beard follows the lower face, leaving the mouth visible.
      for (let j = 0; j < 17; j++) {
        const a = -1.3 + (j * 2.6) / 16;
        ball(
          0.106 * Math.sin(a),
          -0.113 + 0.029 * Math.abs(Math.sin(a)),
          0.1 * Math.cos(a),
          0.023,
          0.032,
          0.02,
          hair,
          head,
        );
      }
      for (let side of [-1, 1])
        stroke(
          [
            [side * 0.016, -0.057, 0.141],
            [side * 0.033, -0.06, 0.137],
            [side * 0.049, -0.066, 0.122],
          ],
          0.006,
          hair,
        );
    } else {
      const cap = mesh(
        new THREE.SphereGeometry(1, 40, 28, 0, Math.PI * 2, 0, 1.24),
        hair,
        head,
      );
      cap.scale.set(0.148, 0.21, 0.139);
      cap.position.z = -0.02;
      for (let j = 0; j < 9; j++)
        stroke(
          [
            [-0.129 + j * 0.008, 0.1 + j * 0.006, 0.069],
            [-0.08 + j * 0.013, 0.189, 0.085],
            [0.035 + j * 0.01, 0.188 - j * 0.004, 0.074],
            [0.123, 0.106 + j * 0.004, 0.025],
          ],
          0.009,
          hair,
        );
      for (let side of [-1, 1]) {
        const ring = mesh(
          new THREE.TorusGeometry(0.038, 0.003, 10, 48),
          shoes,
          head,
        );
        ring.position.set(side * 0.061, 0.033, 0.148);
        ring.scale.y = 0.82;
        stroke(
          [
            [side * 0.101, 0.036, 0.147],
            [side * 0.135, 0.038, 0.099],
            [side * 0.146, 0.032, -0.011],
          ],
          0.003,
          shoes,
        );
      }
      stroke(
        [
          [-0.022, 0.034, 0.151],
          [0, 0.043, 0.154],
          [0.022, 0.034, 0.151],
        ],
        0.0028,
        shoes,
      );
      for (let y of [0.099, 0.117])
        stroke(
          [
            [-0.055, y, 0.111],
            [0, y + 0.004, 0.126],
            [0.055, y, 0.111],
          ],
          0.0012,
          lip,
        );
    }
    const arms = [-1, 1].map((side) => ({
      side,
      upper: capsule(0.06, shirt),
      lower: capsule(0.042, skin),
      joint: ball(0, 0, 0, 0.044, 0.044, 0.044, skin),
      shoulder: ball(0, 0, 0, 0.062, 0.062, 0.062, shirt),
      hand: ball(0, 0, 0, 0.042, 0.068, 0.035, skin),
    }));
    const legs = [-1, 1].map((side) => ({
      side,
      upper: capsule(0.075, trousers),
      lower: capsule(0.057, trousers),
      joint: ball(0, 0, 0, 0.06, 0.06, 0.06, trousers),
      shoe: ball(0, 0, 0, 0.075, 0.059, 0.14, shoes),
    }));
    return {
      torso,
      chest,
      head,
      eyes,
      arms,
      legs,
      width,
      index,
      id,
      phase: index * 2.17,
      home: V(),
    };
  }
  function faceMotion(p, t) {
    const phase = t + p.index * 1.3;
    // Brief, staggered blinks; eye gaze changes smoothly without a fixed stare.
    const period = [4.8, 6, 8][p.index],
      b = phase % period,
      blink = b < 0.16 ? Math.sin((Math.PI * b) / 0.16) : 0;
    for (const { eye, gaze } of p.eyes) {
      eye.scale.y = 1 - blink * 0.96;
      gaze.position.x =
        0.003 * Math.sin(((t / IDLE_DURATION) * Math.PI * 2 + p.phase) * 2);
      gaze.position.y =
        0.0015 * Math.sin(((t / IDLE_DURATION) * Math.PI * 2 + p.phase) * 3);
    }
  }
  function idlePerson(p, t) {
    const q = (t / IDLE_DURATION) * Math.PI * 2 + p.phase,
      breath = Math.sin(q * (5 + p.index)),
      shift = 0.024 * Math.sin(q) + 0.008 * Math.sin(q * 2);
    const yaw = 0,
      f = V(Math.sin(yaw), 0, Math.cos(yaw)),
      r = V(f.z, 0, -f.x);
    const base = p.home.clone().addScaledVector(r, shift);
    base.y = 0.972 + 0.003 * breath - 0.004 * Math.abs(Math.sin(q));
    p.torso.position.copy(base);
    p.torso.rotation.set(
      0.012 + breath * 0.003,
      yaw + 0.014 * Math.sin(q * 2),
      -shift * 0.28,
    );
    p.chest.scale.set(
      1 + 0.009 * breath,
      1 + 0.006 * breath,
      0.64 * (1 + 0.02 * breath),
    );
    p.head.rotation.set(
      0.017 * Math.sin(q * 3),
      0.075 * Math.sin(q) + 0.026 * Math.sin(q * 4),
      0.018 * Math.sin(q * 2),
    );
    faceMotion(p, t);
    const local = (x, y, z) =>
      base
        .clone()
        .addScaledVector(r, x)
        .add(V(0, y, 0))
        .addScaledVector(f, z);
    for (const leg of p.legs) {
      const foot = p.home
        .clone()
        .addScaledVector(r, leg.side * 0.125)
        .addScaledVector(f, leg.side * 0.035);
      foot.y = 0.085;
      const hip = local(leg.side * 0.1, 0, 0),
        delta = foot.clone().sub(hip),
        d = delta.length(),
        axis = delta.clone().normalize(),
        bend = f.clone().addScaledVector(axis, -f.dot(axis)).normalize();
      const knee = hip
        .clone()
        .addScaledVector(axis, d * 0.5)
        .addScaledVector(
          bend,
          Math.sqrt(Math.max(0, 0.45 * 0.45 - d * d * 0.25)),
        );
      segment(leg.upper, hip, knee, 0.075);
      segment(leg.lower, knee, foot, 0.057);
      leg.joint.position.copy(knee);
      leg.shoe.position
        .copy(foot)
        .addScaledVector(f, 0.04)
        .add(V(0, -0.025, 0));
      leg.shoe.rotation.set(0, yaw + leg.side * 0.08, 0);
    }
    for (const arm of p.arms) {
      const side = arm.side,
        sway = 0.01 * Math.sin(q * 3 + side);
      const shoulder = local(side * (p.width + 0.01), 0.44 + breath * 0.002, 0),
        elbow = local(side * (p.width + 0.025), 0.16, 0.008 + sway),
        hand = local(side * (p.width + 0.01), -0.095, 0.05 + sway);
      segment(arm.upper, shoulder, elbow, 0.06);
      segment(arm.lower, elbow, hand, 0.042);
      arm.hand.position.copy(hand);
      arm.hand.rotation.z = side * 0.1;
      arm.joint.position.copy(elbow);
      arm.shoulder.position.copy(shoulder);
    }
  }

  const person = createPerson(index);
  // Convert Y-up/+Z-facing authoring space to the existing Z-up/+Y-facing props.
  scene.rotation.set(Math.PI / 2, 0, Math.PI, "ZXY");
  const height = [1.68, 1.82, 1.7][index];

  let serial = 0;
  scene.traverse((o) => {
    o.name = `${person.id}_${serial++}`;
  });
  function pose(seconds) {
    idlePerson(person, seconds);
    scene.updateMatrixWorld(true);
  }
  pose(0);
  const bounds = new THREE.Box3().setFromObject(scene, true);
  scene.scale.setScalar(height / (bounds.max.z - bounds.min.z));
  scene.position.z = -bounds.min.z * scene.scale.x;
  pose(0);
  return { root: scene, person, pose };
}
