// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Live 3D view of the MARS robot for the Arm SDK page. Parses the same
// mars.urdf the IK node solves against (served at /armsdk/model/) and builds
// the link/joint hierarchy directly — the URDF is small and known, so a
// ~100-line parser beats vendoring a general-purpose loader. Joint angles fed
// via setJoints() are eased toward per-frame, so the 4 Hz state poll renders
// as continuous motion.
//
// three.js is vendored under /public/vendor/ — the robot serves everything
// itself, no CDN at runtime. The filenames carry the pinned version because the
// front door serves vendor files immutable: a bump must be a new URL (new file
// + these imports), or browsers keep the old bytes for up to a year.

import * as THREE from "../../public/vendor/three.module.min.r160.js";
import { OrbitControls } from "../../public/vendor/OrbitControls.r160.js";
import { STLLoader } from "../../public/vendor/STLLoader.r160.js";

const MODEL = "/armsdk/model";

// Anything attached below these gets the accent material (the claw).
const ACCENT_LINKS = new Set(["link61", "link62"]);

/** URDF rpy is fixed-axis XYZ = Rz(y)·Ry(p)·Rx(r), which is three's 'ZYX' Euler.
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

/** Write a 2-point line geometry in place — setFromPoints would allocate a
 * fresh GPU buffer per call and leak the old one (it runs per pointermove).
 * @param {THREE.BufferGeometry} geo @param {THREE.Vector3} a @param {THREE.Vector3} b */
function setLine(geo, a, b) {
  const pos = /** @type {THREE.BufferAttribute} */ (geo.getAttribute("position"));
  pos.setXYZ(0, a.x, a.y, a.z);
  pos.setXYZ(1, b.x, b.y, b.z);
  pos.needsUpdate = true;
}

/**
 * @param {HTMLElement} container
 * @param {{ onDragMove?: (p: {x:number,y:number,z:number}) => void,
 *           onDragEnd?: (p: {x:number,y:number,z:number}) => void }} [callbacks]
 *   Drag-to-move: grabbing the amber handle on the gripper disables orbit and
 *   drags a ghost target in the camera-parallel plane through the EE;
 *   onDragEnd fires with the released base_link position.
 * @returns {Promise<{ setJoints: (joints: number[]) => void, destroy: () => void }>}
 */
export async function createArmViz(container, { onDragMove, onDragEnd } = {}) {
  // Fetch and parse the URDF before creating any GPU resources — the fetch is
  // the likely failure (server down), and failing here leaks nothing.
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
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 20);
  camera.up.set(0, 0, 1);
  camera.position.set(0.55, -0.6, 0.45);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0.08, 0, 0.16);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 0.15;
  controls.maxDistance = 3;

  scene.add(new THREE.HemisphereLight(0xdde4ee, 0x1a1a20, 0.85));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(0.8, -0.9, 1.1);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xa9b6cc, 0.5);
  fill.position.set(-0.9, 0.7, 0.4);
  scene.add(fill);

  // Floor grid in the XY plane (GridHelper natively lies in XZ).
  const grid = new THREE.GridHelper(1.6, 16, 0x3a3d45, 0x24262c);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);

  const matLink = new THREE.MeshStandardMaterial({ color: 0x565b66, metalness: 0.35, roughness: 0.45 });
  const matBase = new THREE.MeshStandardMaterial({ color: 0x33363e, metalness: 0.3, roughness: 0.6 });
  const matAccent = new THREE.MeshStandardMaterial({ color: 0xe8a33d, metalness: 0.4, roughness: 0.35 });

  // ---- build the kinematic tree from the URDF -----------------------------
  /** @type {Map<string, THREE.Group>} link name -> its frame */
  const links = new Map();
  doc.querySelectorAll("robot > link").forEach((el) => {
    const group = new THREE.Group();
    group.name = el.getAttribute("name") || "";
    links.set(group.name, group);
  });

  /** @type {{ group: THREE.Group, originQuat: THREE.Quaternion, axis: THREE.Vector3, mimicOf: string | null, mimicMul: number, angle: number }[]} */
  const movable = [];
  /** @type {Map<string, (typeof movable)[0]>} */
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
      const joint = {
        group: frame,
        originQuat,
        axis: vec3(el.querySelector("axis")?.getAttribute("xyz") ?? "0 0 1").normalize(),
        mimicOf: mimic?.getAttribute("joint") ?? null,
        mimicMul: mimic ? Number(mimic.getAttribute("multiplier") ?? 1) : 1,
        angle: 0,
      };
      movable.push(joint);
      byName.set(el.getAttribute("name") || "", joint);
    }
  });

  // Root = any link never appearing as a child.
  const root = [...links.values()].find((l) => !childLinks.has(l.name));
  if (root) scene.add(root);

  // Helpers own their geometry+material; tracked so destroy() can dispose them.
  /** @type {(THREE.GridHelper | THREE.AxesHelper)[]} */
  const helpers = [grid];

  // End-effector triad — the SDK's reported pose frame, made visible.
  const ee = links.get("ee_link");
  if (ee) {
    const triad = new THREE.AxesHelper(0.06);
    ee.add(triad);
    helpers.push(triad);
  }

  // Base reference marker — the (0,0,0) every SDK pose is measured from: a
  // larger triad plus an amber floor ring at the base_link origin.
  const matRing = new THREE.MeshBasicMaterial({
    color: 0xe8a33d,
    transparent: true,
    opacity: 0.55,
    side: THREE.DoubleSide,
  });
  const base = links.get("base_link");
  /** @type {THREE.BufferGeometry | null} */
  let ringGeo = null;
  if (base) {
    const triad = new THREE.AxesHelper(0.09);
    base.add(triad);
    helpers.push(triad);
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.055, 0.062, 48), matRing);
    ring.position.z = 0.002; // just above the floor grid, no z-fighting
    base.add(ring);
    ringGeo = ring.geometry;
  }

  // ---- drag-to-move handle ------------------------------------------------
  const matHandle = new THREE.MeshBasicMaterial({ color: 0xe8a33d, transparent: true, opacity: 0.35, depthTest: false });
  const matGhost = new THREE.MeshBasicMaterial({ color: 0xe8a33d, transparent: true, opacity: 0.9 });
  const matLine = new THREE.LineBasicMaterial({ color: 0xe8a33d, transparent: true, opacity: 0.5 });
  const handle = new THREE.Mesh(new THREE.SphereGeometry(0.018, 20, 14), matHandle);
  handle.renderOrder = 2;
  // A larger invisible-but-raycastable sphere so the grab doesn't demand
  // pixel accuracy (opacity 0, not material.visible=false — the raycaster
  // must still hit it).
  const grabZone = new THREE.Mesh(
    new THREE.SphereGeometry(0.042, 8, 6),
    new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
  );
  const ghost = new THREE.Mesh(new THREE.SphereGeometry(0.012, 16, 12), matGhost);
  ghost.visible = false;
  scene.add(ghost);
  const lineGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
  const dragLine = new THREE.Line(lineGeo, matLine);
  dragLine.visible = false;
  scene.add(dragLine);
  if (ee) {
    ee.add(handle);
    ee.add(grabZone);
  }

  // Command outcome markers: wire octahedron = commanded target, solid green
  // sphere = where the arm actually settled, red line = the miss between them.
  // Persist until the next cartesian command overwrites them.
  const matTarget = new THREE.MeshBasicMaterial({ color: 0xe8a33d, wireframe: true, transparent: true, opacity: 0.9 });
  const matSettled = new THREE.MeshBasicMaterial({ color: 0x56c28c, transparent: true, opacity: 0.95 });
  const matErr = new THREE.LineBasicMaterial({ color: 0xd96a5a, transparent: true, opacity: 0.85 });
  const targetMarker = new THREE.Mesh(new THREE.OctahedronGeometry(0.014), matTarget);
  const settledMarker = new THREE.Mesh(new THREE.SphereGeometry(0.009, 14, 10), matSettled);
  const errGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
  const errLine = new THREE.Line(errGeo, matErr);
  targetMarker.visible = settledMarker.visible = errLine.visible = false;
  scene.add(targetMarker, settledMarker, errLine);

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const dragPlane = new THREE.Plane();
  const eeWorld = new THREE.Vector3();
  const dragPoint = new THREE.Vector3();
  const dragStart = new THREE.Vector3();
  let dragging = false;
  // Only a real drag commits a move — a bare click on the handle must not
  // command the arm (5 mm threshold).
  let dragMoved = false;

  /** @param {PointerEvent} ev */
  function setRay(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
  }

  /** @param {PointerEvent} ev */
  function onPointerDown(ev) {
    if (!ee || ev.button !== 0) return;
    setRay(ev);
    if (raycaster.intersectObject(grabZone, false).length === 0) return;
    dragging = true;
    dragMoved = false;
    controls.enabled = false;
    ee.getWorldPosition(eeWorld);
    dragStart.copy(eeWorld);
    dragPlane.setFromNormalAndCoplanarPoint(camera.getWorldDirection(dragPoint), eeWorld);
    ghost.position.copy(eeWorld);
    ghost.visible = true;
    dragLine.visible = true;
    renderer.domElement.setPointerCapture(ev.pointerId);
    renderer.domElement.style.cursor = "grabbing";
    ev.preventDefault();
  }

  /** @param {PointerEvent} ev */
  function onPointerMove(ev) {
    if (!ee) return;
    setRay(ev);
    if (!dragging) {
      renderer.domElement.style.cursor = raycaster.intersectObject(grabZone, false).length ? "grab" : "";
      return;
    }
    if (raycaster.ray.intersectPlane(dragPlane, dragPoint)) {
      dragPoint.z = Math.max(0.005, dragPoint.z); // never below the floor
      if (!dragMoved && dragPoint.distanceTo(dragStart) > 0.005) dragMoved = true;
      ghost.position.copy(dragPoint);
      ee.getWorldPosition(eeWorld);
      setLine(lineGeo, eeWorld, ghost.position);
      onDragMove?.({ x: dragPoint.x, y: dragPoint.y, z: dragPoint.z });
    }
  }

  /** Shared drag teardown; commits nothing. */
  function endDrag() {
    dragging = false;
    controls.enabled = true;
    renderer.domElement.style.cursor = "";
    ghost.visible = false;
    dragLine.visible = false;
  }

  function onPointerUp() {
    if (!dragging) return;
    const commit = dragMoved;
    endDrag();
    if (commit) onDragEnd?.({ x: ghost.position.x, y: ghost.position.y, z: ghost.position.z });
  }

  // An OS gesture / palm rejection mid-drag aborts the move, never commits it.
  function onPointerCancel() {
    if (dragging) endDrag();
  }

  renderer.domElement.style.touchAction = "none";
  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("pointerup", onPointerUp);
  renderer.domElement.addEventListener("pointercancel", onPointerCancel);

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
      const url = file.replace("package://mars_description", MODEL);
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
    // A mesh failed to load — free the GPU context instead of stranding it
    // (repeated failed mounts would otherwise hit the browser's WebGL cap).
    controls.dispose();
    geometries.forEach((g) => g.dispose()); // the meshes that DID resolve
    renderer.dispose();
    renderer.domElement.remove();
    throw err;
  }

  // ---- animation ----------------------------------------------------------
  // Targets keyed by SDK joint order (Manipulation.JOINT_NAMES).
  const ORDER = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"];
  /** @type {Map<string, number>} */
  const targets = new Map(ORDER.map((n) => [n, 0]));

  const tmpQuat = new THREE.Quaternion();
  let disposed = false;
  let raf = 0;
  let last = performance.now();

  function frame(/** @type {number} */ now) {
    if (disposed) return;
    raf = requestAnimationFrame(frame);
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    const ease = 1 - Math.exp(-10 * dt);

    for (const [name, joint] of byName) {
      const target = joint.mimicOf
        ? (byName.get(joint.mimicOf)?.angle ?? 0) * joint.mimicMul
        : (targets.get(name) ?? joint.angle);
      joint.angle += (target - joint.angle) * ease;
      tmpQuat.setFromAxisAngle(joint.axis, joint.angle);
      joint.group.quaternion.copy(joint.originQuat).multiply(tmpQuat);
    }
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

  return {
    /** @param {number[]} joints Measured positions in SDK order (j1..j6). */
    setJoints(joints) {
      ORDER.forEach((name, i) => {
        if (typeof joints[i] === "number") targets.set(name, joints[i]);
      });
    },
    /**
     * Show where a cartesian command was aimed vs where the arm settled.
     * @param {{x:number,y:number,z:number}} target commanded position
     * @param {{x:number,y:number,z:number}} settled measured position after the move
     */
    showResult(target, settled) {
      targetMarker.position.set(target.x, target.y, target.z);
      settledMarker.position.set(settled.x, settled.y, settled.z);
      setLine(errGeo, targetMarker.position, settledMarker.position);
      targetMarker.visible = settledMarker.visible = errLine.visible = true;
    },
    destroy() {
      disposed = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      renderer.domElement.removeEventListener("pointercancel", onPointerCancel);
      controls.dispose();
      geometries.forEach((g) => g.dispose());
      helpers.forEach((h) => h.dispose());
      [handle, grabZone, ghost, targetMarker, settledMarker].forEach((m) => m.geometry.dispose());
      lineGeo.dispose();
      errGeo.dispose();
      ringGeo?.dispose();
      [matLink, matBase, matAccent, matHandle, matGhost, matLine, matRing, matTarget, matSettled, matErr, grabZone.material].forEach(
        (m) => /** @type {THREE.Material} */ (m).dispose(),
      );
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
