// SimScene — Three.js scene: the apartment.glb environment (visual only)
// plus the real MARS robot from its ROS URDF. Convention: Z-up, X-forward
// (REP-103); the URDF loads unrotated, the Y-up glb is rotated on load.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import URDFLoader from "urdf-loader";
import type { URDFRobot } from "urdf-loader";

const APARTMENT_URL = "/models/appartement.glb";
const ROBOT_URDF_URL = "/robot/mars.urdf";

// These URDF links carry no real body geometry — just small marker spheres
// used to visualize the end-effector / camera optical frames (e.g. in
// RViz). Hide them here rather than in the URDF itself, since that file is
// shared with the rest of the ROS stack.
const HIDDEN_FRAME_LINKS: string[] = ["ee_link", "head_camera_left", "head_camera_right"];

// Arm links painted orange. Every URDF link actually shares the matt_black
// material, so we override by link name rather than by material.
const ORANGE_LINKS = new Set(["link1", "link3", "link5"]);

// Chase-cam framing used when a pose is snapped in (see spawnAt below).
const CHASE_DISTANCE = 1.8; // meters behind the robot
const CHASE_HEIGHT = 1.1; // meters above the ground

// Robot-mounted camera views: frames, axis conventions, FOV and near plane
// match the driver's cameras (mars_sim_driver.core's CAMERAS).
export type CameraView = "orbit" | "main" | "arm";
const ROBOT_CAMERA_VFOV = 80;
// Don't shrink to fix the near-clipped gripper: the origin sits inside the
// wrist housing, so a smaller near renders the housing interior instead.
const ROBOT_CAMERA_NEAR = 0.03;
const ROBOT_CAMERA_MOUNTS: Array<{
  view: Exclude<CameraView, "orbit">;
  frame: string;
  forward: THREE.Vector3;
  up: THREE.Vector3;
}> = [
  { view: "main", frame: "camera_optical_frame", forward: new THREE.Vector3(0, 0, 1), up: new THREE.Vector3(0, -1, 0) },
  { view: "arm", frame: "arm_camera_link", forward: new THREE.Vector3(1, 0, 0), up: new THREE.Vector3(0, 0, 1) },
];

export class SimScene {
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  readonly renderer: THREE.WebGLRenderer;
  readonly controls: OrbitControls;

  followCamera = true;

  // Hidden until the first real pose (spawnAt): a robot at the world origin
  // before state arrives reads as "spawned in the wrong place" when the true
  // failure is "no state yet".
  private robotRoot = new THREE.Group();
  private robot?: URDFRobot;
  private followPrevXY: [number, number] = [0, 0];
  private glossyMaterialCache = new Map<THREE.Material, THREE.MeshStandardMaterial>();
  private orange?: THREE.MeshStandardMaterial;
  private robotCameras = new Map<CameraView, THREE.PerspectiveCamera>();
  private activeView: CameraView = "orbit";
  private lidarPoints?: THREE.Points;
  private planRibbon?: THREE.Mesh;
  private followerRibbon?: THREE.Mesh;
  private cmdLinearArrow?: THREE.ArrowHelper;
  private cmdTurnArrow?: THREE.ArrowHelper;
  private hullsGroup?: THREE.Group;
  private hullsPromise?: Promise<void>;
  private hullsVisible = false;

  /** Fixed render size (offscreen use, e.g. SimSession); null = track the window. */
  private fixedSize: { width: number; height: number } | null = null;

  constructor(canvas: HTMLCanvasElement, opts: { fixedSize?: { width: number; height: number } } = {}) {
    this.fixedSize = opts.fixedSize ?? null;
    const w = this.fixedSize?.width ?? window.innerWidth;
    const h = this.fixedSize?.height ?? window.innerHeight;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(this.fixedSize ? 1 : Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h, !this.fixedSize);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.5;

    this.scene.background = new THREE.Color(0x14161a);
    this.scene.fog = new THREE.FogExp2(0x14161a, 0.035);

    this.camera = new THREE.PerspectiveCamera(55, w / h, 0.05, 200);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(-3.5, -3.5, 2.4);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.target.set(0, 0, 0.4);
    this.controls.minDistance = 0.5;
    this.controls.maxDistance = 30;
    this.controls.update();

    this.addLights();
    this.addGround();
    this.robotRoot.visible = false;
    this.scene.add(this.robotRoot);

    if (!this.fixedSize) window.addEventListener("resize", () => this.onResize());
  }

  private addLights(): void {
    // No env map on purpose: it grey-washes dark low-roughness materials;
    // several directional lights give the distinct highlights that read as
    // "glossy" on the robot parts.
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.2));

    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.position.set(4, -3, 6);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.near = 0.1;
    key.shadow.camera.far = 30;
    key.shadow.camera.left = -8;
    key.shadow.camera.right = 8;
    key.shadow.camera.top = 8;
    key.shadow.camera.bottom = -8;
    // normalBias clears the shadow-acne stripes a coarse 16m shadow map
    // paints on the robot's ~2cm detail, without depth-bias peter-panning.
    key.shadow.bias = -0.0004;
    key.shadow.normalBias = 0.05;
    this.scene.add(key);

    const fill = new THREE.DirectionalLight(0xaaccff, 0.8);
    fill.position.set(-4, 3, 3);
    this.scene.add(fill);

    this.scene.add(new THREE.HemisphereLight(0xaabbcc, 0x445566, 1.2));
  }

  private addGround(): void {
    // GridHelper lies in the XZ plane by default; rotate it into our XY
    // (ground) plane. Faint — the apartment mesh is the real floor. Nudged
    // just below z=0 so it doesn't z-fight with the apartment floor mesh
    // (which sits right at z=0) and show through as stray lines.
    const grid = new THREE.GridHelper(40, 80, 0x2a2d33, 0x1c1e22);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = -0.02;
    this.scene.add(grid);
  }

  /** Update the lidar overlay with world-frame hit points from /scan. */
  setLidarPoints(points: Float32Array): void {
    if (!this.lidarPoints) {
      const geometry = new THREE.BufferGeometry();
      const material = new THREE.PointsMaterial({ color: 0xff3333, size: 0.04 });
      this.lidarPoints = new THREE.Points(geometry, material);
      this.lidarPoints.visible = false;
      this.lidarPoints.frustumCulled = false;
      this.scene.add(this.lidarPoints);
    }
    this.lidarPoints.geometry.setAttribute("position", new THREE.BufferAttribute(points, 3));
  }

  setLidarVisible(visible: boolean): void {
    if (this.lidarPoints) this.lidarPoints.visible = visible;
  }

  // Ribbon widths (meters) and lift above the floor mesh (enough to clear
  // z-fighting, low enough to read as painted on it). The follower ribbon
  // sits a hair higher so it draws on top where it overlaps the plan.
  private static readonly PLAN_WIDTH = 0.06;
  private static readonly PLAN_Z = 0.02;
  private static readonly FOLLOWER_WIDTH = 0.05;
  private static readonly FOLLOWER_Z = 0.025;

  /** Build a flat miter-less triangle-strip ribbon from ground-frame points
   * [x0,y0, x1,y1, ...] at height `z`, or null for fewer than 2 points. */
  private static ribbonGeometry(points: Float32Array, width: number, z: number): THREE.BufferGeometry | null {
    const n = points.length / 2;
    if (n < 2) return null;
    // Two vertices per point, offset ±half-width along the averaged
    // perpendicular of the adjacent segments.
    const half = width / 2;
    const vertices = new Float32Array(n * 2 * 3);
    for (let i = 0; i < n; i++) {
      const x = points[i * 2];
      const y = points[i * 2 + 1];
      const px = points[Math.max(0, i - 1) * 2];
      const py = points[Math.max(0, i - 1) * 2 + 1];
      const nx = points[Math.min(n - 1, i + 1) * 2];
      const ny = points[Math.min(n - 1, i + 1) * 2 + 1];
      const len = Math.hypot(nx - px, ny - py) || 1;
      const perpX = -(ny - py) / len;
      const perpY = (nx - px) / len;
      vertices.set([x + perpX * half, y + perpY * half, z], i * 6);
      vertices.set([x - perpX * half, y - perpY * half, z], i * 6 + 3);
    }
    const indices: number[] = [];
    for (let i = 0; i < n - 1; i++) {
      const a = i * 2;
      indices.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(vertices, 3));
    geometry.setIndex(indices);
    return geometry;
  }

  /** Point `mesh` at a freshly built ribbon and dispose the old geometry
   * (swapping attributes on a live geometry leaks GPU buffers, and these
   * republish ~1Hz). Hides the mesh when there aren't enough points. */
  private updateRibbon(mesh: THREE.Mesh, points: Float32Array, width: number, z: number): void {
    const geometry = SimScene.ribbonGeometry(points, width, z);
    if (!geometry) {
      mesh.visible = false;
      return;
    }
    mesh.geometry.dispose();
    mesh.geometry = geometry;
    mesh.visible = true;
  }

  /** Show the Nav2 planned path as a flat ribbon on the floor. `points` is
   * ground-frame [x0,y0, x1,y1, ...]; fewer than 2 points hides it. */
  setPlanPoints(points: Float32Array): void {
    if (!this.planRibbon) {
      const material = new THREE.MeshBasicMaterial({
        color: 0x00b7ff, // matches the 2D map widget's route color
        transparent: true,
        opacity: 0.85,
        depthWrite: false,
        side: THREE.DoubleSide,
      });
      this.planRibbon = new THREE.Mesh(new THREE.BufferGeometry(), material);
      this.planRibbon.frustumCulled = false;
      this.scene.add(this.planRibbon);
    }
    this.updateRibbon(this.planRibbon, points, SimScene.PLAN_WIDTH, SimScene.PLAN_Z);
  }

  setPlanVisible(visible: boolean): void {
    if (this.planRibbon) this.planRibbon.visible = visible;
  }

  /** Show the path the MPPI controller is actively tracking (Nav2's
   * transformed_global_plan) as an amber ribbon over the blue plan -- the gap
   * between the two is what the follower is struggling to close. */
  setFollowerPathPoints(points: Float32Array): void {
    if (!this.followerRibbon) {
      const material = new THREE.MeshBasicMaterial({
        color: 0xffb020, // amber -- distinct from the cyan global plan
        transparent: true,
        opacity: 0.9,
        depthWrite: false,
        side: THREE.DoubleSide,
      });
      this.followerRibbon = new THREE.Mesh(new THREE.BufferGeometry(), material);
      this.followerRibbon.frustumCulled = false;
      this.scene.add(this.followerRibbon);
    }
    this.updateRibbon(this.followerRibbon, points, SimScene.FOLLOWER_WIDTH, SimScene.FOLLOWER_Z);
  }

  /** Draw the controller's commanded velocity as arrows anchored to the robot:
   * a forward/back arrow (green forward, red reverse) scaled by linear speed,
   * and a side arrow marking turn direction scaled by yaw rate. Both are
   * children of the robot root, so they inherit its live pose. */
  setCommandVelocity(vx: number, wz: number): void {
    if (!this.cmdLinearArrow) {
      // +X is forward, +Y is left in the robot frame (REP-103).
      this.cmdLinearArrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 0, 0.12), 0.3, 0x33ff88, 0.09, 0.06);
      this.cmdTurnArrow = new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, 0.12), 0.2, 0xffd24a, 0.07, 0.05);
      this.robotRoot.add(this.cmdLinearArrow, this.cmdTurnArrow);
    }
    const turn = this.cmdTurnArrow!;
    // 1 m/s -> ~0.6 m arrow; hidden below a small deadband so a stalled
    // controller reads as "no arrow", not a stub.
    const linLen = Math.min(Math.abs(vx) * 0.6, 0.9);
    if (linLen < 0.02) {
      this.cmdLinearArrow.visible = false;
    } else {
      this.cmdLinearArrow.visible = true;
      this.cmdLinearArrow.setDirection(new THREE.Vector3(Math.sign(vx), 0, 0));
      this.cmdLinearArrow.setLength(linLen, Math.min(0.09, linLen * 0.4), Math.min(0.06, linLen * 0.3));
      this.cmdLinearArrow.setColor(vx >= 0 ? 0x33ff88 : 0xff5544);
    }
    const turnLen = Math.min(Math.abs(wz) * 0.35, 0.5);
    if (turnLen < 0.02) {
      turn.visible = false;
    } else {
      turn.visible = true;
      turn.setDirection(new THREE.Vector3(0, Math.sign(wz), 0)); // wz>0 = CCW = left
      turn.setLength(turnLen, Math.min(0.07, turnLen * 0.4), Math.min(0.05, turnLen * 0.3));
    }
  }

  setFollowerVisible(visible: boolean): void {
    if (this.followerRibbon) this.followerRibbon.visible = visible;
    // Arrows are re-driven by setCommandVelocity each tick while on; here we
    // only need to force them off when the overlay is hidden.
    if (!visible) {
      if (this.cmdLinearArrow) this.cmdLinearArrow.visible = false;
      if (this.cmdTurnArrow) this.cmdTurnArrow.visible = false;
    }
  }

  /**
   * Wireframe overlay of the driver's collision hulls -- the same
   * apartment_collisions_v2 set mars_sim_driver collides against, lazily
   * fetched on first show (manifest.json lists the OBJs since a browser
   * can't list a directory), rotated Y-up -> Z-up like the apartment glb.
   */
  setCollisionHullsVisible(visible: boolean): void {
    this.hullsVisible = visible;
    if (visible && !this.hullsPromise) {
      // ~1300 OBJ fetches; takes seconds on first show. A failure resets the
      // promise so toggling again retries instead of staying dead forever.
      this.hullsPromise = this.loadCollisionHulls().catch((err) => {
        console.error("[sim-viewer] collision hulls failed to load:", err);
        this.hullsPromise = undefined;
      });
    }
    if (this.hullsGroup) this.hullsGroup.visible = visible;
  }

  private async loadCollisionHulls(): Promise<void> {
    const group = new THREE.Group();
    group.rotation.x = Math.PI / 2;
    const baseUrl = "/physics/apartment_collisions_v2/";
    const material = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });

    // Fast path: one binary triangle soup (float32 xyz), one fetch, no
    // parsing -- publish_assets writes it next to the per-hull OBJs.
    const bin = await fetch(`${baseUrl}hulls.f32`);
    if (bin.ok) {
      const positions = new Float32Array(await bin.arrayBuffer());
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      group.add(new THREE.Mesh(geometry, material));
    } else {
      // Older bundles: fetch + parse every hull OBJ individually (slow).
      const manifest: string[] = await (await fetch(`${baseUrl}manifest.json`)).json();
      const loader = new OBJLoader();
      await Promise.all(
        manifest.map(async (filename) => {
          const obj = await loader.loadAsync(`${baseUrl}${filename}`);
          obj.traverse((child) => {
            if (child instanceof THREE.Mesh) child.material = material;
          });
          group.add(obj);
        }),
      );
    }
    group.visible = this.hullsVisible; // honor toggles made while loading
    this.hullsGroup = group;
    this.scene.add(group);
  }

  async loadApartment(): Promise<void> {
    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(APARTMENT_URL);
    const root = gltf.scene;
    root.rotation.x = Math.PI / 2; // glTF Y-up -> scene Z-up
    root.traverse((obj) => {
      if (obj instanceof THREE.Mesh) {
        obj.receiveShadow = true;
        // Force FrontSide (the glb ships doubleSided): walls draw only from
        // inside the room, so overview cameras get the dollhouse-cutaway
        // look. If winding issues hide geometry, flip to BackSide.
        const setFrontSide = (mat: THREE.Material) => {
          mat.side = THREE.FrontSide;
        };
        if (Array.isArray(obj.material)) obj.material.forEach(setFrontSide);
        else setFrontSide(obj.material);
      }
    });
    this.scene.add(root);
  }

  async loadRobot(): Promise<URDFRobot> {
    // loadAsync resolves at URDF parse, but the STL meshes attach later via
    // this LoadingManager -- wait for onLoad or the restyle below misses them.
    const manager = new THREE.LoadingManager();
    const allMeshesLoaded = new Promise<void>((resolve) => {
      manager.onLoad = () => resolve();
    });

    const loader = new URDFLoader(manager);
    loader.packages = { mars_sim: "/robot" };
    const robot = await loader.loadAsync(ROBOT_URDF_URL);
    await allMeshesLoaded;

    for (const name of HIDDEN_FRAME_LINKS) {
      const link = robot.links[name];
      if (link) link.visible = false;
    }

    robot.traverse((obj) => {
      if (obj instanceof THREE.Mesh) {
        obj.castShadow = true;
        obj.receiveShadow = true;
        const linkName = nearestLinkName(obj);
        if (linkName && ORANGE_LINKS.has(linkName)) {
          obj.material = this.orangeMaterial();
        } else {
          obj.material = Array.isArray(obj.material)
            ? obj.material.map((m) => this.toGlossyMaterial(m))
            : this.toGlossyMaterial(obj.material);
        }
      }
    });
    this.robotRoot.add(robot);
    this.robot = robot;

    for (const mount of ROBOT_CAMERA_MOUNTS) {
      const frame = robot.frames[mount.frame];
      if (!frame) {
        console.warn(`[scene] camera frame "${mount.frame}" not found in URDF -- "${mount.view}" view unavailable`);
        continue;
      }
      const cam = new THREE.PerspectiveCamera(
        ROBOT_CAMERA_VFOV,
        this.viewSize().width / this.viewSize().height,
        ROBOT_CAMERA_NEAR,
        100,
      );
      // three.js cameras look down their local -Z with +Y up; build that
      // basis from the mount's forward/up convention.
      const zAxis = mount.forward.clone().negate();
      const xAxis = mount.up.clone().cross(zAxis).normalize();
      cam.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(xAxis, mount.up.clone(), zAxis));
      frame.add(cam);
      this.robotCameras.set(mount.view, cam);
    }
    return robot;
  }

  /** Views actually available (a frame can be missing from the URDF). */
  get availableViews(): CameraView[] {
    return ["orbit", ...this.robotCameras.keys()];
  }

  /** Switch what render() draws: the orbit camera or a robot-mounted one.
   * Falls back to orbit if the requested view's frame wasn't in the URDF. */
  setView(view: CameraView): void {
    this.activeView = view === "orbit" || this.robotCameras.has(view) ? view : "orbit";
    this.controls.enabled = this.activeView === "orbit";
  }

  /** Drive the URDF's arm/head joints to match physics-simulated angles (radians). */
  setJointAngles(joints: Record<string, number>): void {
    this.robot?.setJointValues(joints);
  }

  // Orange accent for the arm links (see ORANGE_LINKS). Cached so every mesh
  // on those links shares one material.
  private orangeMaterial(): THREE.MeshStandardMaterial {
    if (!this.orange) {
      this.orange = new THREE.MeshStandardMaterial({
        color: new THREE.Color(1.0, 0.5, 0.0),
        metalness: 0.4,
        roughness: 0.5,
        side: THREE.DoubleSide, // keeps near-clipped shells solid in the wrist cam
      });
    }
    return this.orange;
  }

  // Swap the URDF's flat MeshPhong for PBR: moderate metalness for a soft
  // sheen (pure metal with no env map reads near-black), rough enough to
  // stay matte.
  private toGlossyMaterial(source: THREE.Material): THREE.MeshStandardMaterial {
    const cached = this.glossyMaterialCache.get(source);
    if (cached) return cached;

    const color = source instanceof THREE.MeshPhongMaterial ? source.color.clone() : new THREE.Color(0xffffff);
    const isDark = (color.r + color.g + color.b) / 3 < 0.4;
    if (isDark) {
      // matt_black is 0.05 — a near-black void once shaded. Lift slightly
      // toward a dark charcoal so the robot's form reads from every angle
      // while staying clearly dark (not full black).
      color.lerp(new THREE.Color(0.12, 0.12, 0.12), 0.4);
    }
    const material = new THREE.MeshStandardMaterial({
      color,
      metalness: isDark ? 0.45 : 0.4,
      roughness: isDark ? 0.5 : 0.55,
      side: THREE.DoubleSide, // see orangeMaterial
    });
    this.glossyMaterialCache.set(source, material);
    return material;
  }

  /**
   * Place the robot at a pose immediately and frame the camera zoomed in
   * behind it (chase-cam), rather than following the incremental delta used
   * by setPose. Use once — e.g. on spawn or reset — before driving resumes.
   */
  spawnAt(x: number, y: number, yaw: number): void {
    this.robotRoot.visible = true;
    this.robotRoot.position.set(x, y, 0);
    this.robotRoot.rotation.set(0, 0, yaw);

    const forwardX = Math.cos(yaw);
    const forwardY = Math.sin(yaw);
    this.camera.position.set(x - forwardX * CHASE_DISTANCE, y - forwardY * CHASE_DISTANCE, CHASE_HEIGHT);
    this.controls.target.set(x, y, 0.5);
    this.controls.update();

    this.followPrevXY = [x, y];
  }

  /** Move the robot root to a 2D pose (meters, yaw radians about +Z). */
  setPose(x: number, y: number, yaw: number): void {
    this.robotRoot.position.set(x, y, 0);
    this.robotRoot.rotation.set(0, 0, yaw);

    if (this.followCamera) {
      const [prevX, prevY] = this.followPrevXY;
      const dx = x - prevX;
      const dy = y - prevY;
      this.camera.position.x += dx;
      this.camera.position.y += dy;
      this.controls.target.x += dx;
      this.controls.target.y += dy;
      this.followPrevXY = [x, y];
    }
  }

  render(): void {
    const robotCam = this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined;
    if (!robotCam) this.controls.update();
    this.renderer.render(this.scene, robotCam ?? this.camera);
  }

  /** Release the GL context + control listeners: the SPA router remounts the
   * stage per visit, and undisposed contexts pile up until the browser kills
   * the oldest (~16), breaking the live view. */
  dispose(): void {
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }

  /** Resize the render target (offscreen/stage use). Logical pixels + ratio. */
  setRenderSize(width: number, height: number, pixelRatio = 1): void {
    this.fixedSize = { width, height };
    this.renderer.setPixelRatio(pixelRatio);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    // Portrait stages (phones): bias the orbit framing so the robot reads in
    // the upper half -- webapp sheets/joystick cover the lower half.
    if (height > width * 1.2) this.camera.setViewOffset(width, height, 0, height * 0.22, width, height);
    else this.camera.clearViewOffset();
    for (const cam of this.robotCameras.values()) {
      cam.aspect = width / height;
      cam.updateProjectionMatrix();
    }
  }

  /** Render the active view into a sub-rectangle of the canvas (logical px,
   * origin bottom-left) -- used for PiP thumbnails. Restores full-canvas
   * viewport afterwards. */
  renderRegion(x: number, y: number, width: number, height: number): void {
    const cam = (this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined) ?? this.camera;
    const prevAspect = cam.aspect;
    cam.aspect = width / height;
    cam.updateProjectionMatrix();
    this.renderer.setViewport(x, y, width, height);
    this.renderer.setScissor(x, y, width, height);
    this.renderer.setScissorTest(true);
    this.renderer.render(this.scene, cam);
    this.renderer.setScissorTest(false);
    const { width: w, height: h } = this.fixedSize ?? { width: window.innerWidth, height: window.innerHeight };
    this.renderer.setViewport(0, 0, w, h);
    cam.aspect = prevAspect;
    cam.updateProjectionMatrix();
  }

  private viewSize(): { width: number; height: number } {
    return this.fixedSize ?? { width: window.innerWidth, height: window.innerHeight };
  }

  private onResize(): void {
    if (this.fixedSize) return; // stage mode: the ResizeObserver drives sizing
    const { width: w, height: h } = this.viewSize();
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    for (const cam of this.robotCameras.values()) {
      cam.aspect = w / h;
      cam.updateProjectionMatrix();
    }
  }
}

/** Walk up the parent chain to the URDFLink a mesh belongs to, returning its name. */
function nearestLinkName(obj: THREE.Object3D): string | null {
  for (let cur: THREE.Object3D | null = obj; cur; cur = cur.parent) {
    if ((cur as { isURDFLink?: boolean }).isURDFLink) return cur.name;
  }
  return null;
}
