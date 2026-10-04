// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Whole-robot 3D stage for the Expression Studio: the same mars.urdf + meshes
// as the Arm SDK page, but the entire robot rides a root that applies the
// base offsets (base_yaw, base_x), the head joint animates, and a ghost person
// stands where the expression is aimed. Poses are applied exactly — playback
// interpolation belongs to the caller. Also renders offscreen stills (judge key
// frames, gallery thumbnails) and records the stage to webm.

import * as THREE from "../../public/vendor/three.module.min.r160.js";
import { OrbitControls } from "../../public/vendor/OrbitControls.r160.js";
import { STLLoader } from "../../public/vendor/STLLoader.r160.js";

const MODEL = "/armsdk/model";
const ACCENT_LINKS = new Set(["link61", "link62"]);

/** The person MARS expresses to, in the anchor frame (robot faces +x). */
const PERSON = { x: 1.0, y: 0, eyes: 1.6 };

/** @typedef {{ key: string, label: string, pos: [number, number, number], look: [number, number, number], fov: number, hidePerson: boolean }} View */
/** @type {View[]} */
export const VIEWS = [
  {
    key: "person",
    label: "person's eyes",
    pos: [PERSON.x - 0.04, 0, PERSON.eyes],
    look: [0.04, 0, 0.2],
    fov: 25,
    hidePerson: true,
  },
  { key: "quarter", label: "three-quarter", pos: [1.05, -1.0, 0.7], look: [0.18, 0, 0.24], fov: 38, hidePerson: false },
  { key: "profile", label: "profile", pos: [0.5, -3.1, 0.9], look: [0.5, 0, 0.8], fov: 40, hidePerson: false },
];
/** Gallery thumbnails: the three-quarter idea, framed on the robot alone. @type {View} */
export const THUMB_VIEW = {
  key: "thumb",
  label: "thumbnail",
  pos: [0.78, -0.72, 0.52],
  look: [0.12, 0, 0.21],
  fov: 34,
  hidePerson: true,
};

/** @param {string | null} s URDF rpy is fixed-axis XYZ = three's 'ZYX' Euler. */
function rpyQuat(s) {
  const [r, p, y] = (s || "0 0 0").trim().split(/\s+/).map(Number);
  return new THREE.Quaternion().setFromEuler(new THREE.Euler(r, p, y, "ZYX"));
}

/** @param {string | null} s */
function vec3(s) {
  const [x, y, z] = (s || "0 0 0").trim().split(/\s+/).map(Number);
  return new THREE.Vector3(x, y, z);
}

/** @typedef {import("./pipeline.js").ActuatorPose} ActuatorPose */

/**
 * @param {HTMLElement} container
 * @param {{ onFrame?: (dt: number) => void, onOrbit?: () => void }} [hooks] onFrame runs before
 *   every render; onOrbit when the user takes the camera off its preset
 */
export async function createStage(container, { onFrame, onOrbit } = {}) {
  // Fetch + parse before any GPU resource exists: the fetch is the likely
  // failure, and failing here leaks nothing.
  const urdfText = await fetch(`${MODEL}/urdf/mars.urdf`).then((r) => {
    if (!r.ok) throw new Error(`URDF fetch failed (${r.status})`);
    return r.text();
  });
  const doc = new DOMParser().parseFromString(urdfText, "text/xml");
  if (doc.querySelector("parsererror")) throw new Error("URDF parse failed");

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.style.display = "block";
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 0.01, 30);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 0.25;
  controls.maxDistance = 6;

  scene.add(new THREE.HemisphereLight(0xdde4ee, 0x1a1a20, 0.9));
  const key = new THREE.DirectionalLight(0xffffff, 1.7);
  key.position.set(1.2, -1.0, 2.2);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -0.8;
  key.shadow.camera.right = 1.6;
  key.shadow.camera.top = 1.2;
  key.shadow.camera.bottom = -1.2;
  key.shadow.camera.near = 0.5;
  key.shadow.camera.far = 6;
  key.shadow.radius = 4;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xa9b6cc, 0.55);
  fill.position.set(-1.2, 0.9, 0.6);
  scene.add(fill);

  const grid = new THREE.GridHelper(4, 40, 0x34373f, 0x1f2126);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(6, 6), new THREE.ShadowMaterial({ opacity: 0.32 }));
  floor.receiveShadow = true;
  floor.position.z = 0.001;
  scene.add(floor);

  const matLink = new THREE.MeshStandardMaterial({ color: 0x5d626e, metalness: 0.35, roughness: 0.45 });
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
  const joints = new Map();
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
    if (el.getAttribute("type") !== "revolute" && el.getAttribute("type") !== "continuous") return;
    const mimic = el.querySelector("mimic");
    joints.set(el.getAttribute("name") || "", {
      group: frame,
      originQuat,
      axis: vec3(el.querySelector("axis")?.getAttribute("xyz") ?? "0 0 1").normalize(),
      mimicOf: mimic?.getAttribute("joint") ?? null,
      mimicMul: mimic ? Number(mimic.getAttribute("multiplier") ?? 1) : 1,
      angle: 0,
    });
  });

  const robotRoot = new THREE.Group();
  scene.add(robotRoot);
  const urdfRoot = [...links.values()].find((l) => !childLinks.has(l.name));
  if (urdfRoot) robotRoot.add(urdfRoot);

  // Anchor marker: where the base stood when the clip started, with a tick
  // toward the person, so base turns and steps read against it.
  const matRing = new THREE.MeshBasicMaterial({
    color: 0xe8a33d,
    transparent: true,
    opacity: 0.5,
    side: THREE.DoubleSide,
  });
  const anchor = new THREE.Group();
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.15, 0.158, 64), matRing);
  const tick = new THREE.Mesh(new THREE.PlaneGeometry(0.05, 0.008), matRing);
  tick.position.x = 0.185;
  anchor.add(ring, tick);
  anchor.position.z = 0.002;
  scene.add(anchor);

  // ---- the person ----------------------------------------------------------
  const matPerson = new THREE.MeshStandardMaterial({
    color: 0x6b93d6,
    transparent: true,
    opacity: 0.38,
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
    if (upright) mesh.rotation.x = Math.PI / 2; // three's capsules extend along y; our up is z
    mesh.position.set(x, y, z);
    person.add(mesh);
  };
  addPart(new THREE.CapsuleGeometry(0.05, 0.82, 4, 10), 0, 0.08, 0.46); // legs
  addPart(new THREE.CapsuleGeometry(0.05, 0.82, 4, 10), 0, -0.08, 0.46);
  addPart(new THREE.CapsuleGeometry(0.13, 0.3, 4, 14), 0, 0, 1.17); // torso
  addPart(new THREE.CapsuleGeometry(0.045, 0.08, 4, 8), 0, 0, 1.47); // neck
  addPart(new THREE.CapsuleGeometry(0.036, 0.52, 4, 8), 0, 0.18, 1.1); // arms
  addPart(new THREE.CapsuleGeometry(0.036, 0.52, 4, 8), 0, -0.18, 1.1);
  addPart(new THREE.SphereGeometry(0.1, 20, 16), 0, 0, PERSON.eyes + 0.02, false); // head
  addPart(new THREE.SphereGeometry(0.022, 10, 8), -0.095, 0.035, PERSON.eyes, false); // eyes, toward the robot
  addPart(new THREE.SphereGeometry(0.022, 10, 8), -0.095, -0.035, PERSON.eyes, false);
  person.position.set(PERSON.x, PERSON.y, 0);
  scene.add(person);

  // ---- meshes ---------------------------------------------------------------
  const loader = new STLLoader();
  /** @type {THREE.BufferGeometry[]} */
  const geometries = [];
  const meshJobs = [];
  for (const el of doc.querySelectorAll("robot > link")) {
    const linkName = el.getAttribute("name") || "";
    for (const visual of el.querySelectorAll("visual")) {
      const file = visual.querySelector("geometry > mesh")?.getAttribute("filename");
      if (!file) continue;
      const url = file.replace(/^package:\/\/mars_[a-z]+/, MODEL);
      const origin = visual.querySelector("origin");
      meshJobs.push(
        loader.loadAsync(url).then((geometry) => {
          geometries.push(geometry);
          const material = ACCENT_LINKS.has(linkName) ? matAccent : linkName === "base_link" ? matBase : matLink;
          const mesh = new THREE.Mesh(geometry, material);
          mesh.castShadow = true;
          mesh.position.copy(vec3(origin?.getAttribute("xyz") ?? null));
          mesh.quaternion.copy(rpyQuat(origin?.getAttribute("rpy") ?? null));
          links.get(linkName)?.add(mesh);
        }),
      );
    }
  }
  const disposables = [grid, floor.geometry, ring.geometry, tick.geometry];
  const materials = [matLink, matBase, matAccent, matRing, matPerson, floor.material];
  function release() {
    controls.dispose();
    geometries.forEach((g) => g.dispose());
    personGeos.forEach((g) => g.dispose());
    disposables.forEach((d) => d.dispose());
    materials.forEach((m) => m.dispose());
    renderer.dispose();
    renderer.forceContextLoss(); // dispose() alone leaves the context to GC; revisits would hit the browser's cap
    renderer.domElement.remove();
  }
  try {
    await Promise.all(meshJobs);
  } catch (err) {
    release(); // a stranded WebGL context counts toward the browser's cap
    throw err;
  }

  // ---- pose -----------------------------------------------------------------
  const tmpQuat = new THREE.Quaternion();
  /** @param {ActuatorPose} pose */
  function setPose(pose) {
    /** @type {Record<string, number>} */
    const targets = {
      joint1: pose.j1,
      joint2: pose.j2,
      joint3: pose.j3,
      joint4: pose.j4,
      joint5: pose.j5,
      joint6: pose.j6,
      joint_head: (pose.head_deg * Math.PI) / 180,
    };
    for (const [name, joint] of joints) {
      joint.angle = joint.mimicOf ? (targets[joint.mimicOf] ?? 0) * joint.mimicMul : (targets[name] ?? 0);
      tmpQuat.setFromAxisAngle(joint.axis, joint.angle);
      joint.group.quaternion.copy(joint.originQuat).multiply(tmpQuat);
    }
    // Differential drive: base_x is travel along the anchor heading.
    robotRoot.position.set(pose.base_x, 0, 0);
    robotRoot.rotation.set(0, 0, pose.base_yaw);
  }

  // ---- camera presets ---------------------------------------------------------
  /** @type {{ from: THREE.Vector3, to: THREE.Vector3, lookFrom: THREE.Vector3, lookTo: THREE.Vector3, fovFrom: number, fovTo: number, t: number } | null} */
  let tween = null;
  /** @param {string} keyName @param {boolean} [instant] */
  function setView(keyName, instant = false) {
    const view = VIEWS.find((v) => v.key === keyName) ?? VIEWS[1];
    const to = new THREE.Vector3(...view.pos);
    const lookTo = new THREE.Vector3(...view.look);
    if (instant) {
      camera.position.copy(to);
      controls.target.copy(lookTo);
      camera.fov = view.fov;
      camera.updateProjectionMatrix();
      tween = null;
      return;
    }
    tween = {
      from: camera.position.clone(),
      to,
      lookFrom: controls.target.clone(),
      lookTo,
      fovFrom: camera.fov,
      fovTo: view.fov,
      t: 0,
    };
  }
  setView("quarter", true);

  /** @param {number} dt */
  function stepTween(dt) {
    if (!tween) return;
    tween.t = Math.min(1, tween.t + dt / 0.6);
    const e = tween.t < 0.5 ? 4 * tween.t ** 3 : 1 - (-2 * tween.t + 2) ** 3 / 2;
    camera.position.lerpVectors(tween.from, tween.to, e);
    controls.target.lerpVectors(tween.lookFrom, tween.lookTo, e);
    camera.fov = tween.fovFrom + (tween.fovTo - tween.fovFrom) * e;
    camera.updateProjectionMatrix();
    if (tween.t >= 1) tween = null;
  }
  controls.addEventListener("start", () => {
    tween = null;
    onOrbit?.();
  });

  // ---- recording ------------------------------------------------------------
  // The WebGL canvas is composited with a caption onto a 2D canvas right after
  // each render (the drawing buffer is only valid until the frame is presented).
  /** @type {{ canvas: HTMLCanvasElement, ctx: CanvasRenderingContext2D, caption: string, sub: string } | null} */
  let recording = null;
  function composite() {
    if (!recording) return;
    const { canvas, ctx, caption, sub } = recording;
    const { width, height } = canvas;
    const bg = ctx.createLinearGradient(0, 0, 0, height);
    bg.addColorStop(0, "#15161b");
    bg.addColorStop(1, "#0a0a0c");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(renderer.domElement, 0, 0, width, height);
    const pad = Math.round(height * 0.045);
    ctx.font = `600 ${Math.round(height * 0.042)}px system-ui, -apple-system, sans-serif`;
    ctx.fillStyle = "#e7e7ea";
    ctx.fillText(caption, pad, height - pad - Math.round(height * 0.04));
    ctx.font = `${Math.round(height * 0.026)}px ui-monospace, Menlo, monospace`;
    ctx.fillStyle = "#e8a33d";
    let line = sub;
    while (line.length > 4 && ctx.measureText(line).width > width - 2 * pad) line = `${line.slice(0, -2)}…`;
    ctx.fillText(line, pad, height - pad);
  }

  /**
   * Start capturing the stage (webm, or whatever this browser's MediaRecorder
   * makes). stop() resolves with the video; throws where recording is unsupported.
   * @param {{ caption: string, sub: string, fps?: number }} opts
   */
  function record({ caption, sub, fps = 30 }) {
    const canvas = document.createElement("canvas");
    const src = renderer.domElement;
    const scale = Math.min(1, 1280 / src.width);
    canvas.width = Math.round((src.width * scale) / 2) * 2;
    canvas.height = Math.round((src.height * scale) / 2) * 2;
    const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
    recording = { canvas, ctx, caption, sub };
    composite();
    const mime = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"].find((m) =>
      MediaRecorder.isTypeSupported(m),
    );
    const stream = canvas.captureStream(fps);
    const recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 6_000_000 });
    /** @type {Blob[]} */
    const chunks = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };
    recorder.start(250);
    return {
      /** @returns {Promise<Blob>} */
      stop: () =>
        new Promise((resolve) => {
          recorder.onstop = () => {
            stream.getTracks().forEach((track) => track.stop());
            recording = null;
            resolve(new Blob(chunks, { type: recorder.mimeType || "video/webm" }));
          };
          recorder.stop();
        }),
    };
  }

  // ---- loop -------------------------------------------------------------------
  let disposed = false;
  let raf = 0;
  let last = performance.now();
  function render() {
    renderer.render(scene, camera);
    composite();
  }
  /** @param {number} now */
  function frame(now) {
    if (disposed) return;
    raf = requestAnimationFrame(frame);
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    onFrame?.(dt);
    stepTween(dt);
    controls.update();
    render();
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

  // ---- stills ---------------------------------------------------------------
  // One offscreen renderer for every still, kept for the page's life (a WebGL
  // context is too costly to churn per shot).
  /** @type {THREE.WebGLRenderer | null} */
  let stillRenderer = null;
  const stillCamera = new THREE.PerspectiveCamera(36, 1, 0.01, 30);
  stillCamera.up.set(0, 0, 1);
  const judgeBackdrop = new THREE.Color(0xdfe3e8);
  const studioBackdrop = new THREE.Color(0x15161b);

  /**
   * Render `poses` from a fixed view to JPEG data URLs on an opaque backdrop,
   * then put the live pose back. `studio` keeps the dark stage look (thumbnails);
   * the judge gets a neutral light backdrop and no studio chrome.
   * @param {ActuatorPose[]} poses @param {ActuatorPose} restore
   * @param {{ view?: string | View, size?: number, studio?: boolean }} [opts]
   */
  function stills(poses, restore, { view = "person", size = 512, studio = false } = {}) {
    if (!stillRenderer) {
      stillRenderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
      stillRenderer.outputColorSpace = THREE.SRGBColorSpace;
      stillRenderer.shadowMap.enabled = true;
      stillRenderer.shadowMap.type = THREE.PCFSoftShadowMap;
    }
    const r = stillRenderer;
    const v = typeof view === "string" ? (VIEWS.find((x) => x.key === view) ?? VIEWS[0]) : view;
    r.setSize(size, size, false);
    stillCamera.fov = v.fov;
    stillCamera.aspect = 1;
    stillCamera.position.set(...v.pos);
    stillCamera.lookAt(...v.look);
    stillCamera.updateProjectionMatrix();
    scene.background = studio ? studioBackdrop : judgeBackdrop;
    anchor.visible = studio;
    grid.visible = studio;
    person.visible = !v.hidePerson;
    const shots = poses.map((pose) => {
      setPose(pose);
      r.render(scene, stillCamera);
      return r.domElement.toDataURL("image/jpeg", 0.85);
    });
    scene.background = null;
    anchor.visible = grid.visible = person.visible = true;
    setPose(restore);
    return shots;
  }

  return {
    setPose,
    setView,
    /** Render one frame now — for hidden tabs where requestAnimationFrame never fires. */
    render,
    stills,
    record,
    destroy() {
      disposed = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      stillRenderer?.dispose();
      stillRenderer?.forceContextLoss();
      release();
    },
  };
}
