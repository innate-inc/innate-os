// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Authored household residents. No scans, external textures, or random geometry.
import * as THREE from "three";
import { createHead } from "./head.mjs";

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
    sole = mat("sole");
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
    ball(0, 0.585, 0, 0.049, 0.08, 0.046, skin, torso);
    const collar = mesh(
      new THREE.TorusGeometry(0.057, 0.012, 8, 40),
      shirt,
      torso,
    );
    collar.rotation.x = Math.PI / 2;
    collar.position.y = 0.535;
    for (let y of [0.36, 0.43, 0.5])
      ball(0.015, y, 0.134, 0.009, 0.009, 0.005, sole, torso);
    const { head, eyes } = createHead(index, skin, hair, torso);
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
