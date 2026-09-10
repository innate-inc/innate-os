// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// A real, self-contained victory diorama: procedural boot/plant, the shipped MARS
// URDF, and one bounded WebGL renderer. No robot commands or physics are involved.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import URDFLoader from "urdf-loader";
import type { URDFRobot } from "urdf-loader";

type Ring = [y: number, x: number, rx: number, rz: number];
const TAU = Math.PI * 2;
const vec = (p: number[]) => new THREE.Vector3(p[0], p[1], p[2]);
const smooth = (t: number) => {
  const u = THREE.MathUtils.clamp(t, 0, 1);
  return u * u * (3 - 2 * u);
};

/** Open, continuously shaded leather shell; its ankle really is hollow. */
function loft(rings: Ring[], material: THREE.Material): THREE.Mesh {
  const positions: number[] = [],
    uv: number[] = [],
    indices: number[] = [];
  const segments = 64;
  rings.forEach(([y, x, rx, rz], j) => {
    for (let i = 0; i <= segments; i++) {
      const a = (i / segments) * TAU;
      const wrinkle = j > 2 && j < rings.length - 2 ? Math.sin(a * 5 + j * 2) * 0.007 : 0;
      positions.push(x + (rx + wrinkle) * Math.cos(a), y, (rz + wrinkle) * Math.sin(a));
      uv.push((i / segments) * 3, y * 2);
      if (j && i < segments) {
        const n = j * (segments + 1) + i,
          b = n - segments - 1;
        indices.push(b, n, b + 1, n, n + 1, b + 1);
      }
    }
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return new THREE.Mesh(geometry, material);
}

function tube(points: number[][], radius: number, material: THREE.Material, closed = false) {
  return new THREE.Mesh(
    new THREE.TubeGeometry(
      new THREE.CatmullRomCurve3(points.map(vec), closed),
      Math.max(16, points.length * 6),
      radius,
      6,
      closed,
    ),
    material,
  );
}

function ellipse(y: number, x: number, rx: number, rz: number) {
  return Array.from({ length: 64 }, (_, i) => {
    const a = (i / 64) * TAU;
    return [x + rx * Math.cos(a), y, rz * Math.sin(a)];
  });
}

/** Geometry and material details remain readable during the full 360-degree spin. */
export function createBootPlant(): { group: THREE.Group; sprout: THREE.Group } {
  const group = new THREE.Group();
  group.name = "first-life-boot";
  const grain = new Uint8Array(128 * 128 * 4);
  let seed = 17;
  for (let i = 0; i < grain.length; i += 4) {
    seed = (1664525 * seed + 1013904223) >>> 0;
    const v = 90 + (seed % 100);
    grain.set([v, v, v, 255], i);
  }
  const bump = new THREE.DataTexture(grain, 128, 128);
  bump.wrapS = bump.wrapT = THREE.RepeatWrapping;
  bump.needsUpdate = true;
  const leather = new THREE.MeshStandardMaterial({
    color: 0x4e2c1c,
    roughness: 0.7,
    metalness: 0.04,
    bumpMap: bump,
    bumpScale: 0.014,
    side: THREE.DoubleSide,
  });
  const trim = new THREE.MeshStandardMaterial({ color: 0xa57444, roughness: 0.8 });
  const rubber = new THREE.MeshStandardMaterial({ color: 0x29261f, roughness: 0.92 });
  const lace = new THREE.MeshStandardMaterial({ color: 0xcfb389, roughness: 0.98 });
  const brass = new THREE.MeshStandardMaterial({ color: 0xb59550, metalness: 0.8, roughness: 0.32 });
  const body = loft(
    [
      [0.13, -0.12, 0.79, 0.335],
      [0.22, -0.15, 0.76, 0.35],
      [0.34, -0.1, 0.68, 0.345],
      [0.44, 0, 0.53, 0.32],
      [0.56, 0.16, 0.39, 0.3],
      [0.72, 0.27, 0.32, 0.29],
      [0.9, 0.29, 0.31, 0.285],
      [1.08, 0.29, 0.32, 0.3],
      [1.13, 0.29, 0.335, 0.31],
    ],
    leather,
  );
  group.add(body);
  group.add(
    loft(
      [
        [0.015, -0.12, 0.81, 0.35],
        [0.06, -0.12, 0.82, 0.36],
        [0.105, -0.12, 0.81, 0.355],
        [0.145, -0.12, 0.79, 0.345],
      ],
      rubber,
    ),
  );
  // Closed sole and heel, then individually visible rubber lugs.
  const sole = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 0.05, 64), rubber);
  sole.position.set(-0.12, 0.035, 0);
  sole.scale.set(0.81, 1, 0.35);
  group.add(sole);
  for (let i = 0; i < 12; i++)
    for (const side of [-1, 1]) {
      const x = -0.82 + i * 0.13;
      const z = Math.sqrt(Math.max(0, 1 - ((x + 0.12) / 0.81) ** 2)) * 0.345 * side;
      const lug = new THREE.Mesh(new THREE.BoxGeometry(0.075, 0.065, 0.055), rubber);
      lug.position.set(x, 0.035, z);
      group.add(lug);
    }
  group.add(tube(ellipse(0.155, -0.12, 0.796, 0.344), 0.012, trim, true));
  group.add(tube(ellipse(1.125, 0.29, 0.331, 0.306), 0.027, leather, true));
  group.add(
    loft(
      [
        [1.13, 0.29, 0.31, 0.285],
        [1.01, 0.29, 0.285, 0.26],
      ],
      new THREE.MeshStandardMaterial({ color: 0x2a1b14, roughness: 1, side: THREE.DoubleSide }),
    ),
  );
  const soilMat = new THREE.MeshStandardMaterial({ color: 0x24150c, roughness: 1 });
  const soil = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.28, 0.04, 48), soilMat);
  soil.position.set(0.29, 1.025, 0);
  soil.scale.z = 0.91;
  group.add(soil);
  for (let i = 0; i < 32; i++) {
    const pebble = new THREE.Mesh(new THREE.IcosahedronGeometry(0.016 + (i % 3) * 0.006), soilMat);
    const a = i * 2.39996,
      r = 0.25 * Math.sqrt((i + 0.5) / 32);
    pebble.position.set(0.29 + Math.cos(a) * r, 1.055, Math.sin(a) * r * 0.9);
    group.add(pebble);
  }
  // Toe-cap seam and double stitching around the welt.
  group.add(
    tube(
      Array.from({ length: 25 }, (_, i) => {
        const a = -Math.PI / 2 + (i / 24) * Math.PI;
        return [-0.5 - 0.14 * Math.cos(a), 0.23 + 0.15 * Math.cos(a), 0.325 * Math.sin(a)];
      }),
      0.008,
      trim,
    ),
  );
  for (let i = 0; i < 72; i++) {
    const a = (i / 72) * TAU,
      b = a + 0.024;
    group.add(
      tube(
        [
          [-0.12 + 0.803 * Math.cos(a), 0.165, 0.346 * Math.sin(a)],
          [-0.12 + 0.803 * Math.cos(b), 0.165, 0.346 * Math.sin(b)],
        ],
        0.0035,
        lace,
      ),
    );
  }
  const rows = [
    [-0.35, 0.48],
    [-0.17, 0.61],
    [-0.075, 0.76],
    [-0.045, 0.9],
    [-0.035, 1.035],
  ];
  for (const [x, y] of rows)
    for (const side of [-1, 1]) {
      const eye = new THREE.Mesh(new THREE.TorusGeometry(0.024, 0.006, 8, 16), brass);
      eye.position.set(x, y, side * 0.16);
      eye.quaternion.setFromUnitVectors(
        new THREE.Vector3(0, 0, 1),
        new THREE.Vector3(-1, 0.3, side * 0.6).normalize(),
      );
      group.add(eye);
    }
  for (let i = 0; i < rows.length - 1; i++)
    for (const side of [-1, 1]) {
      const [x, y] = rows[i],
        [nx, ny] = rows[i + 1];
      group.add(
        tube(
          [
            [x - 0.015, y, 0.16 * side],
            [(x + nx) / 2 - 0.085, (y + ny) / 2, 0],
            [nx - 0.015, ny, -0.16 * side],
          ],
          0.012,
          lace,
        ),
      );
    }
  group.add(
    tube(
      [
        [-0.05, 1.05, 0],
        [-0.13, 1.12, 0.12],
        [-0.13, 1.03, 0.21],
        [-0.05, 1.05, 0],
        [-0.15, 1.12, -0.15],
        [-0.17, 1.02, -0.2],
        [-0.05, 1.05, 0],
      ],
      0.011,
      lace,
    ),
  );
  group.add(
    tube(
      [
        [-0.05, 1.05, 0],
        [-0.16, 0.91, 0.06],
        [-0.2, 0.75, 0.1],
      ],
      0.011,
      lace,
    ),
  );
  group.add(
    tube(
      [
        [-0.05, 1.05, 0],
        [-0.18, 0.94, -0.08],
        [-0.15, 0.8, -0.14],
      ],
      0.011,
      lace,
    ),
  );
  for (const side of [-1, 1])
    group.add(
      tube(
        [
          [0.49, 0.23, side * 0.29],
          [0.55, 0.48, side * 0.265],
          [0.54, 0.82, side * 0.23],
          [0.52, 1.08, side * 0.24],
        ],
        0.008,
        trim,
      ),
    );

  const sprout = new THREE.Group();
  sprout.name = "living-sprout";
  sprout.position.set(0.29, 1.04, 0);
  group.add(sprout);
  const stemMaterial = new THREE.MeshStandardMaterial({ color: 0x66952d, roughness: 0.6 });
  sprout.add(
    tube(
      [
        [0, 0, 0],
        [0.08, 0.3, 0],
        [0.035, 0.62, 0.025],
        [-0.11, 0.96, 0],
        [-0.08, 1.18, -0.04],
      ],
      0.018,
      stemMaterial,
    ),
  );
  // Curved, folded leaf surfaces and raised midribs, not flat cutout planes.
  const leaves = [
    [0.06, 0.36, 0, 0.76, 0.3, 0.08],
    [0.015, 0.65, 0.02, -0.72, 0.4, 0.1],
    [-0.09, 0.94, 0, 0.48, 0.35, -0.23],
    [-0.08, 1.13, -0.03, -0.38, 0.35, -0.1],
  ];
  leaves.forEach(([x, y, z, dx, dy, dz], index) => {
    const leaf = new THREE.Group();
    leaf.position.set(x, y, z);
    leaf.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), new THREE.Vector3(dx, dy, dz).normalize());
    const length = Math.hypot(dx, dy, dz),
      width = length * 0.27;
    const positions: number[] = [],
      indices: number[] = [];
    for (let j = 0; j <= 16; j++)
      for (let k = 0; k <= 8; k++) {
        const t = j / 16,
          s = k / 4 - 1,
          span = Math.sin(Math.PI * t) ** 0.8;
        positions.push(s * width * span, t * length, 0.11 * Math.sin(t * Math.PI) - 0.09 * s * s * span);
        if (j < 16 && k < 8) {
          const a = j * 9 + k;
          indices.push(a, a + 1, a + 9, a + 1, a + 10, a + 9);
        }
      }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    geometry.computeVertexNormals();
    leaf.add(
      new THREE.Mesh(
        geometry,
        new THREE.MeshPhysicalMaterial({
          color: index % 2 ? 0x83b843 : 0x5b9d2c,
          roughness: 0.48,
          side: THREE.DoubleSide,
          clearcoat: 0.25,
          emissive: 0x244500,
          emissiveIntensity: 0.12,
        }),
      ),
    );
    leaf.add(
      tube(
        Array.from({ length: 12 }, (_, i) => {
          const t = i / 11;
          return [0, t * length, 0.11 * Math.sin(t * Math.PI) + 0.004];
        }),
        0.004,
        stemMaterial,
      ),
    );
    sprout.add(leaf);
  });
  group.traverse((o) => {
    if (o instanceof THREE.Mesh) {
      o.castShadow = true;
      o.receiveShadow = true;
    }
  });
  return { group, sprout };
}

/** URDF and STL requests are abortable when the modal is dismissed while loading. */
async function loadMars(signal: AbortSignal): Promise<URDFRobot> {
  const text = await fetch("/robot/urdf/mars.urdf", { signal }).then((r) => {
    if (!r.ok) throw new Error("MARS model unavailable");
    return r.text();
  });
  const loader = new URDFLoader();
  loader.packages = { mars_sim: "/robot" };
  loader.parseCollision = false;
  const jobs: Promise<void>[] = [];
  const materials = new Set<THREE.Material>();
  const geometries = new Set<THREE.BufferGeometry>();
  loader.loadMeshCb = ((
    path: string,
    _manager: THREE.LoadingManager,
    material: THREE.Material,
    done: (o: THREE.Object3D | null, err?: unknown) => void,
  ) => {
    materials.add(material);
    jobs.push(
      fetch(path, { signal })
        .then((r) => {
          if (!r.ok) throw new Error(`MARS mesh unavailable: ${path}`);
          return r.arrayBuffer();
        })
        .then((data) => {
          const geometry = new STLLoader().parse(data);
          geometries.add(geometry);
          done(new THREE.Mesh(geometry, material));
        })
        .catch((error) => {
          done(null, error);
          throw error;
        }),
    );
  }) as unknown as typeof loader.loadMeshCb;
  const robot = loader.parse(text);
  const results = await Promise.allSettled(jobs);
  if (signal.aborted || results.some((r) => r.status === "rejected")) {
    robot.traverse((o) => {
      if (o instanceof THREE.Mesh) {
        geometries.add(o.geometry);
        for (const m of Array.isArray(o.material) ? o.material : [o.material]) materials.add(m);
      }
    });
    geometries.forEach((g) => g.dispose());
    materials.forEach((m) => m?.dispose());
    throw new Error(signal.aborted ? "MARS loading cancelled" : "MARS meshes could not load");
  }
  for (const name of ["ee_link", "head_camera_left", "head_camera_right"])
    if (robot.links[name]) robot.links[name].visible = false;
  const dark = new THREE.MeshStandardMaterial({
    color: 0x202828,
    metalness: 0.4,
    roughness: 0.36,
    side: THREE.DoubleSide,
  });
  const orange = new THREE.MeshStandardMaterial({
    color: 0xe56b1c,
    metalness: 0.2,
    roughness: 0.42,
    side: THREE.DoubleSide,
  });
  robot.traverse((o) => {
    if (!(o instanceof THREE.Mesh)) return;
    let p: THREE.Object3D | null = o;
    while (p && !("isURDFLink" in p)) p = p.parent;
    o.material = p && ["link1", "link3", "link5"].includes(p.name) ? orange : dark;
    o.castShadow = true;
    o.receiveShadow = true;
  });
  materials.forEach((m) => m?.dispose());
  // The STL has no glass materials. Put the two lens highlights on the same measured
  // camera centres used by SimScene, so MARS's gaze reads during its upward nod.
  const head = robot.links.head;
  if (head)
    for (const y of [0.0297, -0.0303]) {
      const lens = new THREE.Mesh(
        new THREE.SphereGeometry(1, 16, 12),
        new THREE.MeshPhysicalMaterial({
          color: 0x267c7d,
          metalness: 0.45,
          roughness: 0.12,
          clearcoat: 1,
          emissive: 0x1c6b58,
          emissiveIntensity: 0.45,
        }),
      );
      lens.position.set(0.0635, y, -0.000275);
      lens.scale.set(0.002, 0.006, 0.006);
      head.add(lens);
    }
  return robot;
}

function disposeTree(root: THREE.Object3D): void {
  const geometries = new Set<THREE.BufferGeometry>(),
    materials = new Set<THREE.Material>(),
    textures = new Set<THREE.Texture>();
  root.traverse((o) => {
    if (o instanceof THREE.Mesh || o instanceof THREE.Points) {
      geometries.add(o.geometry);
      for (const m of Array.isArray(o.material) ? o.material : [o.material]) materials.add(m);
    }
  });
  for (const m of materials)
    for (const value of Object.values(m)) if (value instanceof THREE.Texture) textures.add(value);
  geometries.forEach((g) => g.dispose());
  materials.forEach((m) => m.dispose());
  textures.forEach((t) => t.dispose());
}

export interface PlantRewardScene {
  ready: Promise<void>;
  start(): void;
  collect(): void;
  dispose(): void;
}

export function createPlantRewardScene(host: HTMLElement): PlantRewardScene {
  const scene = new THREE.Scene();
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.domElement.className = "plant-reward-webgl";
  renderer.domElement.setAttribute(
    "aria-label",
    "MARS celebrates beneath a floating plant in a boot. Drag to look around.",
  );
  renderer.domElement.setAttribute("role", "img");
  renderer.domElement.tabIndex = 0;
  host.append(renderer.domElement);
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 40);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 1.22, 0);
  controls.enablePan = false;
  controls.enableZoom = false;
  controls.enabled = false;
  controls.enableDamping = true;
  controls.minPolarAngle = 0.45;
  controls.maxPolarAngle = 1.5;
  const pmrem = new THREE.PMREMGenerator(renderer),
    room = new RoomEnvironment();
  const environment = pmrem.fromScene(room, 0.04);
  scene.environment = environment.texture;
  room.dispose();
  pmrem.dispose();
  scene.add(new THREE.HemisphereLight(0xe6f5ce, 0x274c40, 1.2));
  const key = new THREE.DirectionalLight(0xffdfa0, 2.8);
  key.position.set(-3, 6, 4);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -3;
  key.shadow.camera.right = 3;
  key.shadow.camera.top = 4;
  key.shadow.camera.bottom = -2;
  key.shadow.normalBias = 0.025;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xe0fff2, 1.4);
  fill.position.set(4, 2.5, 4);
  scene.add(fill);
  const rim = new THREE.PointLight(0x9effbc, 6, 12);
  rim.position.set(2, 3, -2);
  scene.add(rim);
  const glow = new THREE.PointLight(0xffeab2, 0, 5);
  glow.position.set(0, 1.8, 0.2);
  scene.add(glow);
  const platform = new THREE.Mesh(
    new THREE.CylinderGeometry(1.13, 1.23, 0.12, 96),
    new THREE.MeshStandardMaterial({ color: 0x152e26, roughness: 0.3, metalness: 0.35 }),
  );
  platform.position.y = -0.09;
  platform.receiveShadow = true;
  scene.add(platform);
  const border = new THREE.Mesh(
    new THREE.TorusGeometry(1.14, 0.012, 8, 96),
    new THREE.MeshBasicMaterial({ color: 0xe3d4a0 }),
  );
  border.rotation.x = Math.PI / 2;
  border.position.y = -0.025;
  scene.add(border);
  const { group: boot, sprout } = createBootPlant();
  boot.scale.setScalar(0.5);
  boot.position.set(0.26, 1.4, -0.25);
  scene.add(boot);
  const actor = new THREE.Group();
  actor.rotation.x = -Math.PI / 2;
  actor.scale.setScalar(3.8);
  actor.position.set(-0.13, 0, 0.12);
  scene.add(actor);
  // Orbiting motes live in the same depth buffer as the robot and reward.
  const particlePositions = new Float32Array(120 * 3);
  const particleGeometry = new THREE.BufferGeometry();
  particleGeometry.setAttribute("position", new THREE.BufferAttribute(particlePositions, 3));
  const particleMaterial = new THREE.PointsMaterial({
    color: 0xffe4a0,
    size: 0.024,
    transparent: true,
    opacity: 0.8,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const particles = new THREE.Points(particleGeometry, particleMaterial);
  scene.add(particles);
  const rings = new THREE.Group();
  rings.position.set(0.26, 1.8, -0.25);
  scene.add(rings);
  for (let i = 0; i < 2; i++) {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(0.7 + i * 0.16, 0.005, 6, 96),
      new THREE.MeshBasicMaterial({ color: 0xf5e6ad, transparent: true, opacity: 0.32 }),
    );
    ring.rotation.set(1.2 + i * 0.5, 0.3 + i * 0.8, 0.4);
    rings.add(ring);
  }
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  controls.enableDamping = !reduced.matches;
  const abort = new AbortController();
  let robot: URDFRobot | null = null,
    disposed = false,
    frame = 0,
    startTime = 0,
    collectTime = 0,
    started = false,
    dragged = false;
  controls.addEventListener("start", () => {
    dragged = true;
  });
  const onKey = (event: KeyboardEvent) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    dragged = true;
    const offset = camera.position.clone().sub(controls.target);
    const spherical = new THREE.Spherical().setFromVector3(offset);
    if (event.key === "ArrowLeft") spherical.theta -= 0.16;
    if (event.key === "ArrowRight") spherical.theta += 0.16;
    if (event.key === "ArrowUp") spherical.phi -= 0.12;
    if (event.key === "ArrowDown") spherical.phi += 0.12;
    spherical.phi = THREE.MathUtils.clamp(spherical.phi, 0.45, 1.5);
    camera.position.copy(controls.target).add(new THREE.Vector3().setFromSpherical(spherical));
    camera.lookAt(controls.target);
    controls.update();
    renderer.render(scene, camera);
  };
  renderer.domElement.addEventListener("keydown", onKey);
  const pose = {
    joint1: 1.15,
    joint2: 0.1,
    joint3: -1.2,
    joint4: 0.65,
    joint5: 0,
    joint6: 0.45,
    joint6M: -0.45,
    joint_head: 0.28,
  };
  const ready = loadMars(abort.signal).then((model) => {
    if (disposed) {
      disposeTree(model);
      throw new DOMException("Celebration closed", "AbortError");
    }
    robot = model;
    actor.add(robot);
    robot.setJointValues(pose);
    render(performance.now());
  });
  let lastWidth = 0,
    lastHeight = 0;
  function render(now: number) {
    if (disposed) return;
    const width = host.clientWidth,
      height = host.clientHeight;
    if (width && height && (width !== lastWidth || height !== lastHeight)) {
      lastWidth = width;
      lastHeight = height;
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }
    const t = reduced.matches ? 5 : started ? (now - startTime) / 1000 : 0;
    const entrance = smooth(t / 2.1);
    if (!dragged) {
      const angle = 1.05 + (1 - entrance) * 0.72 + (t > 3 ? Math.sin((t - 3) * 0.17) * 0.08 : 0);
      const radius = 5.5 + (1 - entrance) * 1.2;
      camera.position.set(Math.sin(angle) * radius, 2.75, Math.cos(angle) * radius);
      camera.lookAt(controls.target);
    }
    boot.rotation.y = 1.55 + (1 - entrance) * TAU;
    boot.rotation.z = reduced.matches ? -0.08 : Math.sin(t * 1.3) * 0.04 - 0.08;
    boot.position.y = 1.4 + (reduced.matches ? 0 : Math.sin(t * 1.6) * 0.045) + (1 - entrance) * 0.4;
    boot.scale.setScalar(0.5 * (0.15 + 0.85 * entrance));
    sprout.rotation.z = reduced.matches ? 0 : Math.sin(t * 2) * 0.035;
    actor.position.y = reduced.matches ? 0 : Math.max(0, Math.sin(Math.min(t / 2, 1) * Math.PI)) * 0.12;
    robot?.setJointValues({
      ...pose,
      joint5: reduced.matches ? 0 : Math.sin(t * 5) * 0.3 * smooth((t - 1.6) / 0.5),
      joint_head: 0.28 + Math.sin(t * 1.4) * 0.03,
    });
    glow.intensity = 2 + 10 * Math.exp(-(((t - 1.65) / 0.55) ** 2));
    rings.rotation.y = t * 0.3;
    rings.scale.setScalar(0.3 + 0.7 * entrance);
    for (let i = 0; i < 120; i++) {
      const a = i * 2.39996 + t * 0.12,
        r = 0.55 + ((i % 17) / 17) * 1.15;
      particlePositions[i * 3] = Math.cos(a) * r;
      particlePositions[i * 3 + 1] = 0.1 + (((i / 120) * 2.8 + t * 0.06) % 2.8);
      particlePositions[i * 3 + 2] = Math.sin(a) * r;
    }
    particleGeometry.attributes.position.needsUpdate = true;
    particles.visible = entrance > 0.5;
    if (collectTime) {
      const u = smooth((now - collectTime) / 800);
      boot.position.y += u * 2;
      boot.scale.multiplyScalar(1 - u * 0.95);
      boot.rotation.y += u * TAU;
      glow.intensity = 4 + u * 5;
    }
    controls.update();
    renderer.render(scene, camera);
    host.dataset.rendered = "true";
  }
  function tick(now: number) {
    render(now);
    if (!disposed && !reduced.matches) frame = requestAnimationFrame(tick);
  }
  const resize = new ResizeObserver(() => render(performance.now()));
  resize.observe(host);
  const onMotion = () => {
    controls.enableDamping = !reduced.matches;
    cancelAnimationFrame(frame);
    tick(performance.now());
  };
  reduced.addEventListener("change", onMotion);
  controls.addEventListener("change", () => {
    if (reduced.matches) renderer.render(scene, camera);
  });
  return {
    ready,
    start() {
      if (disposed) return;
      controls.enabled = true;
      started = true;
      startTime = performance.now();
      tick(startTime);
    },
    collect() {
      collectTime = performance.now();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      abort.abort();
      cancelAnimationFrame(frame);
      resize.disconnect();
      reduced.removeEventListener("change", onMotion);
      renderer.domElement.removeEventListener("keydown", onKey);
      controls.dispose();
      disposeTree(scene);
      key.shadow.dispose();
      environment.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    },
  };
}
