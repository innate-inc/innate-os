import * as THREE from "three";
import URDFLoader from "urdf-loader";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

export async function createScene(canvas) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: true,
  });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.65;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 20);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.minDistance = 0.5;
  controls.maxDistance = 2.6;
  controls.maxPolarAngle = Math.PI / 2 - 0.08;
  let fittedView = true;
  const fittedDirection = new THREE.Vector3(0.75, -0.65, 0.45);
  const fitView = () => {
    fittedView = true;
    // Frame the robot and its arm workspace using the narrower field of view.
    // A study's orbit angle is recording metadata, not a live camera preset.
    const halfFov = THREE.MathUtils.degToRad(camera.fov / 2);
    const limitingFov = Math.min(
      halfFov,
      Math.atan(Math.tan(halfFov) * camera.aspect),
    );
    const distance = 0.32 / Math.sin(limitingFov);
    // Flush pending input before setting the new camera position.
    controls.enableDamping = false;
    controls.update();
    controls.target.set(0.15, -0.02, 0.15);
    camera.up.set(0, 0, 1);
    camera.position
      .copy(controls.target)
      .add(fittedDirection.clone().normalize().multiplyScalar(distance));
    controls.update();
    controls.enableDamping = true;
  };
  controls.addEventListener("start", () => {
    fittedView = false;
  });
  const resetView = () => {
    fittedDirection.set(0.75, -0.65, 0.45);
    fitView();
  };
  resetView();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8f9ea6, 3));
  const key = new THREE.DirectionalLight(0xffffff, 4.5);
  key.position.set(1, -0.6, 2.4);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  Object.assign(key.shadow.camera, {
    left: -0.7,
    right: 0.7,
    top: 0.7,
    bottom: -0.7,
    near: 0.1,
    far: 5,
  });
  key.shadow.bias = -0.0002;
  key.shadow.normalBias = 0.001;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xcaf2ee, 2);
  fill.position.set(-1, 1, 1);
  scene.add(fill);
  const floor = new THREE.Mesh(
    new THREE.CircleGeometry(1.7, 96),
    new THREE.ShadowMaterial({ opacity: 0.13 }),
  );
  floor.position.z = 0;
  floor.receiveShadow = true;
  scene.add(floor);
  const grid = new THREE.GridHelper(1.6, 32, 0xc8d2cf, 0xe1e6e0);
  grid.rotation.x = Math.PI / 2;
  grid.position.z = 0.001;
  grid.material.transparent = true;
  grid.material.opacity = 0.6;
  scene.add(grid);
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.35, 0.351, 100),
    new THREE.MeshBasicMaterial({ color: 0xc5d7d1, side: THREE.DoubleSide }),
  );
  ring.position.set(0.1, 0, 0.001);
  scene.add(ring);

  const manager = new THREE.LoadingManager();
  const loaded = new Promise((resolve, reject) => {
    manager.onLoad = resolve;
    manager.onError = (url) => reject(new Error(`Robot asset failed: ${url}`));
  });
  const loader = new URDFLoader(manager);
  loader.packages = { mars_sim: "/robot", mars_description: "/robot" };
  const robot = await loader.loadAsync("/robot/urdf/mars.urdf");
  await loaded;
  for (const name of ["ee_link", "head_camera_left", "head_camera_right"]) {
    if (robot.links[name])
      robot.links[name].children.forEach((child) => {
        if (child.isURDFVisual) child.visible = false;
      });
  }
  robot.traverse((obj) => {
    if (obj.isMesh) {
      obj.castShadow = true;
      obj.receiveShadow = true;
      const previous = obj.material;
      obj.material = new THREE.MeshStandardMaterial({
        color: previous.color || 0x303b3d,
        roughness: 0.62,
        metalness: 0.18,
      });
      previous.dispose?.();
    }
  });
  scene.add(robot);
  const target = new THREE.Mesh(
    new THREE.SphereGeometry(0.006, 24, 16),
    new THREE.MeshBasicMaterial({
      color: 0x008b73,
      transparent: true,
      opacity: 0.9,
    }),
  );
  const halo = new THREE.Mesh(
    new THREE.SphereGeometry(0.012, 20, 12),
    new THREE.MeshBasicMaterial({
      color: 0x008b73,
      wireframe: true,
      transparent: true,
      opacity: 0.18,
    }),
  );
  target.add(halo);
  scene.add(target);
  target.visible = false;
  const trailGeometry = new THREE.BufferGeometry();
  const trailPositions = new THREE.BufferAttribute(new Float32Array(75 * 3), 3);
  trailGeometry.setAttribute("position", trailPositions);
  trailGeometry.setDrawRange(0, 0);
  const trail = new THREE.Line(
    trailGeometry,
    new THREE.LineBasicMaterial({
      color: 0x32ac91,
      transparent: true,
      opacity: 0.48,
    }),
  );
  scene.add(trail);
  const points = [];
  let desired = {},
    pose = [0, 0, 0],
    targetPoint = null,
    active = false,
    disposed = false;
  let last = performance.now();
  const resize = new ResizeObserver(() => {
    const { width, height } = canvas.getBoundingClientRect();
    if (width <= 0 || height <= 0) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / Math.max(1, height);
    camera.updateProjectionMatrix();
    if (fittedView) fitView();
  });
  resize.observe(canvas);
  const render = (now) => {
    if (disposed) return;
    const alpha = 1 - Math.exp(-Math.min(0.1, (now - last) / 1000) / 0.035);
    last = now;
    for (const [name, value] of Object.entries(desired)) {
      const joint = robot.joints[name];
      if (joint)
        robot.setJointValue(name, joint.angle + (value - joint.angle) * alpha);
    }
    robot.position.set(pose[0], pose[1], 0);
    robot.rotation.z = pose[2];
    if (targetPoint) {
      target.visible = active;
      target.position.fromArray(targetPoint);
    }
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(render);
  };
  requestAnimationFrame(render);
  return {
    resetView,
    studyView(side = false, fit = false) {
      if (fit) {
        fittedDirection.set(...(side ? [0.05, -1, 0.16] : [0.75, -0.65, 0.45]));
        fitView();
        return;
      }
      fittedView = false;
      camera.position.set(...(side ? [0.3, -0.72, 0.23] : [0.78, -0.43, 0.39]));
      controls.target.set(0.25, -0.035, 0.14);
      controls.update();
    },
    viewState() {
      return {
        position: camera.position.toArray(),
        quaternion: camera.quaternion.toArray(),
        target: controls.target.toArray(),
        fov: camera.fov,
        up: camera.up.toArray(),
      };
    },
    update(state) {
      desired = state.joints;
      pose = state.pose;
      targetPoint = state.target;
      active = state.active;
      if (active && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
        points.push(new THREE.Vector3(...state.ee));
        if (points.length > 75) points.shift();
      } else if (points.length) points.shift();
      points.forEach((point, i) =>
        trailPositions.setXYZ(i, point.x, point.y, point.z),
      );
      trailPositions.needsUpdate = true;
      trailGeometry.setDrawRange(0, points.length);
      trailGeometry.computeBoundingSphere();
    },
    destroy() {
      disposed = true;
      resize.disconnect();
      controls.dispose();
      renderer.dispose();
      scene.traverse((o) => {
        o.geometry?.dispose();
        o.material?.dispose?.();
      });
    },
  };
}
