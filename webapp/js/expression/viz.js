// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Whole-robot 3D view for the Expression Studio. Same URDF + meshes as the
// Arm SDK page (served at /armsdk/model/), same ~100-line parser — but the
// entire robot rides a movable root (base advance/sidestep/turn are pose
// variables here), the head joint animates, and a person-scale observer
// figure stands where the expression is aimed, so approach/turn/attend read
// in the frame. Also renders the blind judge's fixed viewpoints offscreen.
//
// three.js is vendored (pinned r160) under /public/vendor/ — no CDN.

import * as THREE from "../../public/vendor/three.module.min.js";
import { OrbitControls } from "../../public/vendor/OrbitControls.js";
import { STLLoader } from "../../public/vendor/STLLoader.js";
import { PERSON } from "./model.js";

const MODEL = "/armsdk/model";
const ACCENT_LINKS = new Set(["link61", "link62"]);

// The judge's cameras, fixed in the world frame — the robot moving inside a
// static frame is how base offset becomes visible in a still image, so every
// view must cover the full ±0.3 m the base can roam. The person view is
// literally the observer's eyes (their own ghost body hidden); the profile
// view holds both bodies full-height so distance reads directly.
export const SNAPSHOT_VIEWS = [
  { key: "person", label: "person's eyes", pos: [PERSON.x - 0.06, PERSON.y + 0.03, PERSON.height - 0.13], look: [-0.02, 0, 0.15], fov: 26, hidePerson: true },
  { key: "quarter", label: "three-quarter", pos: [0.95, -1.15, 0.65], look: [0.02, 0, 0.2], fov: 42, hidePerson: false },
  { key: "profile", label: "profile", pos: [0.35, -2.3, 0.85], look: [0.35, 0, 0.7], fov: 48, hidePerson: false },
];

/** URDF rpy is fixed-axis XYZ = Rz(y)·Ry(p)·Rx(r) = three's 'ZYX' Euler.
 * @param {string | null} s */
function rpyQuat(s) {
  const [r, p, y] = (s || "0 0 0").trim().split(/\s+/).map(Number);
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(r, p, y, "ZYX"));
}

/** @param {string | null} s */
function vec3(s) {
  const [x, y, z] = (s || "0 0 0").trim().split(/\s+/).map(Number);
  return new THREE.Vector3(x, y, z);
}

/**
 * @param {HTMLElement} container
 * @returns {Promise<{
 *   setPose: (pose: import("./model.js").Pose) => void,
 *   snapshot: (size?: number, viewKeys?: string[]) => { key: string, label: string, dataUrl: string }[],
 *   destroy: () => void }>}
 */
export async function createExpressionViz(container) {
  // Fetch + parse before any GPU resources exist — the fetch is the likely
  // failure and failing here leaks nothing.
  const urdfText = await fetch(`${MODEL}/urdf/mars.urdf`).then((r) => {
    if (!r.ok) throw new Error(`URDF fetch failed (${r.status})`);
    return r.text();
  });
  const doc = new DOMParser().parseFromString(urdfText, "text/xml");
  if (doc.querySelector("parsererror")) throw new Error("URDF parse failed");

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.display = "block";
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();

  // Z-up world, matching base_link.
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 30);
  camera.up.set(0, 0, 1);
  camera.position.set(0.9, -1.1, 0.7);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0.35, 0, 0.25);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 0.2;
  controls.maxDistance = 6;

  scene.add(new THREE.HemisphereLight(0xdde4ee, 0x1a1a20, 0.85));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(0.8, -0.9, 1.1);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xa9b6cc, 0.5);
  fill.position.set(-0.9, 0.7, 0.4);
  scene.add(fill);

  // Floor grid in the XY plane (GridHelper natively lies in XZ).
  const grid = new THREE.GridHelper(3.2, 32, 0x3a3d45, 0x24262c);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);

  const matLink = new THREE.MeshStandardMaterial({ color: 0x565b66, metalness: 0.35, roughness: 0.45 });
  const matBase = new THREE.MeshStandardMaterial({ color: 0x33363e, metalness: 0.3, roughness: 0.6 });
  const matAccent = new THREE.MeshStandardMaterial({ color: 0xe8a33d, metalness: 0.4, roughness: 0.35 });

  // ---- kinematic tree from the URDF ---------------------------------------
  /** @type {Map<string, THREE.Group>} */
  const links = new Map();
  doc.querySelectorAll("robot > link").forEach((el) => {
    const group = new THREE.Group();
    group.name = el.getAttribute("name") || "";
    links.set(group.name, group);
  });

  /** @type {Map<string, { group: THREE.Group, originQuat: THREE.Quaternion, axis: THREE.Vector3, mimicOf: string | null, mimicMul: number, angle: number }>} */
  const byName = new Map();
  const childLinks = new Set();

  doc.querySelectorAll("robot > joint").forEach((el) => {
    const parent = links.get(el.querySelector("parent")?.getAttribute("link") || "");
    const child = links.get(el.querySelector("child")?.getAttribute("link") || "");
    if (!parent || !child) return;
    const origin = el.querySelector("origin");
    const frame = new THREE.Group();
    frame.position.copy(vec3(origin?.getAttribute("xyz") ?? null));
    const originQuat = rpyQuat(origin?.getAttribute("rpy") ?? null);
    frame.quaternion.copy(originQuat);
    parent.add(frame);
    frame.add(child);
    childLinks.add(child.name);

    if (el.getAttribute("type") === "revolute" || el.getAttribute("type") === "continuous") {
      const mimic = el.querySelector("mimic");
      byName.set(el.getAttribute("name") || "", {
        group: frame,
        originQuat,
        axis: vec3(el.querySelector("axis")?.getAttribute("xyz") ?? "0 0 1").normalize(),
        mimicOf: mimic?.getAttribute("joint") ?? null,
        mimicMul: mimic ? Number(mimic.getAttribute("multiplier") ?? 1) : 1,
        angle: 0,
      });
    }
  });

  // The whole robot rides one movable root: base offset/turn animate here,
  // exactly like a joint — the URDF tree hangs underneath untouched.
  const robotRoot = new THREE.Group();
  scene.add(robotRoot);
  const urdfRoot = [...links.values()].find((l) => !childLinks.has(l.name));
  if (urdfRoot) robotRoot.add(urdfRoot);

  // Home ring — where the neutral base sits, so displacement reads against it.
  const matRing = new THREE.MeshBasicMaterial({ color: 0xe8a33d, transparent: true, opacity: 0.55, side: THREE.DoubleSide });
  const homeRing = new THREE.Mesh(new THREE.RingGeometry(0.1, 0.108, 48), matRing);
  homeRing.position.z = 0.002; // just above the grid, no z-fighting
  scene.add(homeRing);

  // ---- observer figure ----------------------------------------------------
  // A ghosted person at real scale: the ~1.4 m face-height gap is the reason
  // "attend" runs to +70°, and the judge needs the same referent in frame.
  const matPerson = new THREE.MeshStandardMaterial({
    color: 0x6b93d6,
    transparent: true,
    opacity: 0.4,
    roughness: 0.8,
    depthWrite: false,
  });
  const person = new THREE.Group();
  /** @type {THREE.BufferGeometry[]} */
  const personGeos = [];
  /** @param {THREE.BufferGeometry} geo @param {number} x @param {number} y @param {number} z @param {boolean} [upright] */
  const addPart = (geo, x, y, z, upright = true) => {
    personGeos.push(geo);
    const mesh = new THREE.Mesh(geo, matPerson);
    if (upright) mesh.rotation.x = Math.PI / 2; // three's capsules/cylinders extend along y; our up is z
    mesh.position.set(x, y, z);
    person.add(mesh);
  };
  addPart(new THREE.CapsuleGeometry(0.045, 0.68, 4, 10), 0, 0.075, 0.42); // legs
  addPart(new THREE.CapsuleGeometry(0.045, 0.68, 4, 10), 0, -0.075, 0.42);
  addPart(new THREE.CapsuleGeometry(0.1, 0.42, 4, 14), 0, 0, 1.06); // torso
  addPart(new THREE.CapsuleGeometry(0.035, 0.52, 4, 8), 0, 0.155, 1.0); // arms
  addPart(new THREE.CapsuleGeometry(0.035, 0.52, 4, 8), 0, -0.155, 1.0);
  addPart(new THREE.SphereGeometry(0.096, 18, 14), 0, 0, PERSON.height - 0.09, false); // head
  addPart(new THREE.SphereGeometry(0.026, 10, 8), -0.09, 0, PERSON.height - 0.1, false); // gaze marker, toward the robot
  person.position.set(PERSON.x, PERSON.y, 0);
  scene.add(person);

  // ---- meshes -------------------------------------------------------------
  const loader = new STLLoader();
  /** @type {THREE.BufferGeometry[]} */
  const geometries = [];
  const meshJobs = [];
  for (const el of doc.querySelectorAll("robot > link")) {
    const linkName = el.getAttribute("name") || "";
    for (const visual of el.querySelectorAll("visual")) {
      const file = visual.querySelector("geometry > mesh")?.getAttribute("filename");
      if (!file) continue;
      const url = file.replace("package://mars_sim", MODEL);
      const origin = visual.querySelector("origin");
      meshJobs.push(
        loader.loadAsync(url).then((geometry) => {
          geometries.push(geometry);
          const material = ACCENT_LINKS.has(linkName) ? matAccent : linkName === "base_link" ? matBase : matLink;
          const mesh = new THREE.Mesh(geometry, material);
          mesh.position.copy(vec3(origin?.getAttribute("xyz") ?? null));
          mesh.quaternion.copy(rpyQuat(origin?.getAttribute("rpy") ?? null));
          links.get(linkName)?.add(mesh);
        }),
      );
    }
  }
  try {
    await Promise.all(meshJobs);
  } catch (err) {
    // A mesh failed — free the GPU context instead of stranding it (repeated
    // failed mounts would otherwise hit the browser's WebGL cap).
    controls.dispose();
    geometries.forEach((g) => g.dispose());
    personGeos.forEach((g) => g.dispose());
    renderer.dispose();
    renderer.domElement.remove();
    throw err;
  }

  // ---- animation ----------------------------------------------------------
  const JOINT_ORDER = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"];
  /** @type {Map<string, number>} */
  const jointTargets = new Map(JOINT_ORDER.map((n) => [n, 0]));
  const baseTarget = { x: 0, y: 0, yaw: 0 };

  const tmpQuat = new THREE.Quaternion();
  let disposed = false;
  let raf = 0;
  let last = performance.now();

  /** Advance every eased value by dt; shared by the live loop and snapshots. @param {number} ease */
  function stepTargets(ease) {
    for (const [name, joint] of byName) {
      const target = joint.mimicOf
        ? (byName.get(joint.mimicOf)?.angle ?? 0) * joint.mimicMul
        : (jointTargets.get(name) ?? joint.angle);
      joint.angle += (target - joint.angle) * ease;
      tmpQuat.setFromAxisAngle(joint.axis, joint.angle);
      joint.group.quaternion.copy(joint.originQuat).multiply(tmpQuat);
    }
    robotRoot.position.x += (baseTarget.x - robotRoot.position.x) * ease;
    robotRoot.position.y += (baseTarget.y - robotRoot.position.y) * ease;
    robotRoot.rotation.z += (baseTarget.yaw - robotRoot.rotation.z) * ease;
  }

  function frame(/** @type {number} */ now) {
    if (disposed) return;
    raf = requestAnimationFrame(frame);
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    stepTargets(1 - Math.exp(-10 * dt));
    controls.update();
    renderer.render(scene, camera);
  }

  function resize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();
  raf = requestAnimationFrame(frame);

  // ---- snapshots ----------------------------------------------------------
  // Offscreen renderer for the judge views, created on the first snapshot and
  // kept for the page's life (a WebGL context is too costly to churn per shot).
  /** @type {THREE.WebGLRenderer | null} */
  let snapRenderer = null;
  const snapCamera = new THREE.PerspectiveCamera(38, 1, 0.01, 30);
  snapCamera.up.set(0, 0, 1);
  const snapBackground = new THREE.Color(0xdfe3e8);

  /**
   * Render the judge's viewpoints to JPEG data URLs at the pose's *settled*
   * targets (easing snapped forward, restored after) on an opaque neutral
   * backdrop, home ring hidden — the judge sees a robot, not studio chrome.
   * @param {number} [size] @param {string[]} [viewKeys]
   */
  function snapshot(size = 640, viewKeys) {
    if (!snapRenderer) {
      snapRenderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
      snapRenderer.outputColorSpace = THREE.SRGBColorSpace;
    }
    snapRenderer.setSize(size, size, false);
    const views = SNAPSHOT_VIEWS.filter((v) => !viewKeys || viewKeys.includes(v.key));
    scene.background = snapBackground;
    homeRing.visible = false;
    stepTargets(1); // settle
    const shots = views.map((view) => {
      snapCamera.fov = view.fov;
      snapCamera.aspect = 1;
      snapCamera.position.set(view.pos[0], view.pos[1], view.pos[2]);
      snapCamera.lookAt(view.look[0], view.look[1], view.look[2]);
      snapCamera.updateProjectionMatrix();
      person.visible = !view.hidePerson; // the observer's own body stays out of their eye view
      /** @type {THREE.WebGLRenderer} */ (snapRenderer).render(scene, snapCamera);
      return { key: view.key, label: view.label, dataUrl: /** @type {THREE.WebGLRenderer} */ (snapRenderer).domElement.toDataURL("image/jpeg", 0.85) };
    });
    scene.background = null;
    homeRing.visible = true;
    person.visible = true;
    return shots;
  }

  return {
    /** @param {import("./model.js").Pose} pose */
    setPose(pose) {
      jointTargets.set("joint1", pose.j1);
      jointTargets.set("joint2", pose.j2);
      jointTargets.set("joint3", pose.j3);
      jointTargets.set("joint4", pose.j4);
      jointTargets.set("joint5", pose.j5);
      jointTargets.set("joint6", pose.j6);
      jointTargets.set("joint_head", (pose.head * Math.PI) / 180);
      baseTarget.x = pose.baseX;
      baseTarget.y = pose.baseY;
      baseTarget.yaw = pose.baseYaw;
    },
    snapshot,
    destroy() {
      disposed = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      controls.dispose();
      geometries.forEach((g) => g.dispose());
      personGeos.forEach((g) => g.dispose());
      grid.dispose();
      homeRing.geometry.dispose();
      [matLink, matBase, matAccent, matRing, matPerson].forEach((m) => m.dispose());
      snapRenderer?.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
