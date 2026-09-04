// SimScene — Three.js scene: the environment pack's glb (visual only) plus
// the real MARS robot from its ROS URDF. Convention: Z-up, X-forward
// (REP-103); the URDF loads unrotated, the Y-up glb is rotated on load.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import URDFLoader from "urdf-loader";
import type { URDFRobot } from "urdf-loader";
import { LoadQueue, queuedGLB } from "./loadQueue";
import { PropLibrary, type PropInfo } from "./props";
import { TrafficLibrary } from "./traffic";
import type { TrafficManifest, TrafficState } from "./trafficState";

/** An environment pack's browser assets as its manifest names them: paths
 * under sim/viewer/public, which the webapp serves at /models and /physics
 * (environments.py). A world server that predates packs announces none, and
 * the apartment is what it is running. */
export type EnvironmentViewer = Record<string, string>;
export const APARTMENT_VIEWER: EnvironmentViewer = {
  type: "split-glb",
  manifest: "models/apartment/manifest.json",
  base_dir: "models/apartment",
  model: "models/appartement.glb",
  collision_dir: "physics/apartment_collisions_v2",
};
const publicUrl = (path: string): string => `/${path.replace(/^\/+/, "")}`;
// /robot is the mars_sim ROS package itself (served straight from ros2_ws, see
// webapp/proxy/https_server.py), so the URDF sits at its real path inside it.
const ROBOT_URDF_URL = "/robot/urdf/mars.urdf";

/** Per-room split written by tools/split-apartment.mjs. bbox is in the glb's
 * Y-up frame (the whole apartment is rotated Y-up -> Z-up on load). */
interface ApartmentManifest {
  rooms: ManifestRoom[];
  total: number;
}
interface ManifestRoom {
  file: string;
  name: string;
  bytes: number;
  bbox: { min: number[]; max: number[] };
}

/** The apartment's wireframe skeleton: parent group (already in the scene,
 * carrying the Y-up -> Z-up rotation) plus one placeholder box per room, drawn
 * from the manifest alone. streamApartment() then swaps boxes for real glbs. */
export interface ApartmentLayout {
  group: THREE.Group;
  rooms: { room: ManifestRoom; box: LineSegments2 }[];
  /** No manifest: streamApartment falls back to the monolith glb. */
  monolith: boolean;
  /** Where the rooms' files live (trailing slash), or the monolith's URL. */
  baseUrl: string;
  modelUrl?: string;
}
// These URDF links carry no real body geometry — just small marker spheres
// used to visualize the end-effector / camera optical frames (e.g. in
// RViz). Hide them here rather than in the URDF itself, since that file is
// shared with the rest of the ROS stack.
const HIDDEN_FRAME_LINKS: string[] = ["ee_link", "head_camera_left", "head_camera_right"];

// Arm links painted orange. Every URDF link actually shares the matt_black
// material, so we override by link name rather than by material.
const ORANGE_LINKS = new Set(["link1", "link3", "link5"]);

// The dome capping each barrel in head.STL is the lens's cover glass. Splitting
// by position is the only handle on it: an STL carries no materials, and every
// URDF link declares one matt_black. Measured on the mesh, the dome spans
// x=60.07..63.07 at r<=7.50 and the wall behind it starts at r=8.75.
const CAM_LINK = "head";
const CAM_GLASS_MIN_X = 0.059;
const CAM_GLASS_RADIUS = 0.008;
// Millimetre features, so a micron welds only float noise.
const CAM_WELD_M = 1e-6;
// RoomEnvironment is Y-up; this scene is Z-up, so its bright side would
// otherwise reflect in from one side.
const CAM_ENV_ROTATION = new THREE.Euler(-Math.PI / 2, 0, 0);

// Inside the barrel. The wall already renders from within (shell is
// DoubleSide); only the lens and a disc over the 7.00mm hole are missing.
const CAM_BACK_X = 0.0563;
const CAM_BACK_RADIUS = 0.0074;
const CAM_LENS_X = 0.059; // just behind the dome's base at x=60.07
const CAM_LENS_RADIUS = 0.0045;
const CAM_LENS_SPHERE = 0.02; // the curve it is a cap of: ~0.5mm of bulge
const CAM_CENTRES: [number, number][] = [
  [0.0297, -0.000275],
  [-0.0303, -0.000275],
];

// Room streaming order: the spaces the operator looks at first load first.
// Matched as substrings of the room name (from the source glb, Portuguese:
// "Sala" = living room, "Corredor" = hallway); anything unmatched keeps
// manifest order behind them. The queue is FIFO (loadQueue.ts), so enqueue
// order is load order.
const ROOM_PRIORITY = ["sala", "corredor"];
function roomPriority(name: string): number {
  const lower = name.toLowerCase();
  const i = ROOM_PRIORITY.findIndex((keyword) => lower.includes(keyword));
  return i === -1 ? ROOM_PRIORITY.length : i;
}
// Key light offset from whatever it is lighting; setPose slides it along with
// the robot so the tight shadow box stays centred on it. The direction is the
// original key light's, so the robot shades the same as it always did.
const KEY_LIGHT_OFFSET: [number, number, number] = [2.0, -1.5, 3.0];

// The shadow box covers the robot AND every prop in the world, so nothing
// loses its shadow by being left behind -- but it shrinks to the minimum that
// does, because texel size is 2 * box / map and that is what decides whether
// shadows look sharp or blocky. 0.7m is the floor (a robot 0.35m across with
// a 0.36m reach, props dropped within 0.35m): 0.68mm per texel at 2048. Past
// SHADOW_BOX_MAX_M it stops growing and distant props lose their shadow
// rather than blurring the robot's, which is the part being looked at.
const SHADOW_BOX_MIN_M = 0.7;
const SHADOW_BOX_MAX_M = 3.0;
const SHADOW_BOX_STEP_M = 0.25; // quantised, so the box does not resize every frame
const SHADOW_MARGIN_M = 0.5; // the robot's own extent plus the throw of its shadow
const SHADOW_MAP_PX = 2048;

// Initial orbit framing used when a pose is snapped in (see spawnAt below).
const INITIAL_ORBIT_POSITION = { forward: 0.61, left: 0.02, height: 0.25 };
const INITIAL_ORBIT_TARGET = { forward: -0.01, left: 0, height: 0.13 };

// How the orbit camera behaves. Orthogonal to CameraView, which picks WHICH
// camera renders: every mode here drives the same orbit camera.
//   free  -- drag to orbit; the camera pans with the robot but never turns.
//   chase -- pinned above and behind the robot, turning with it.
//   top   -- the whole apartment from above, indifferent to where the robot is.
export type CameraMode = "free" | "chase" | "top";
// Chase framing, in the robot's own frame. Close over the shoulder: ~1.4m out
// at a 28 degree depression, which fills about a third of the frame height
// with robot and still shows the floor it is about to drive over.
const CHASE_BACK_M = 1.2;
const CHASE_HEIGHT_M = 1.0;
const CHASE_TARGET_HEIGHT_M = 0.35;
// Exponential follow rate. A hard pin makes every wheel wobble a camera
// shake; this lags the robot slightly and the turn reads as a turn.
const CHASE_LAG_HZ = 4.0;
// Near-nadir, not nadir: straight down leaves the camera's roll undefined
// (up is +Z, and lookAt has nothing to resolve it against).
const TOP_TILT = new THREE.Vector3(0, -0.35, 1).normalize();
const TOP_FIT_MARGIN = 1.15;
// Long enough to read as the camera pulling back rather than cutting.
const TOP_TWEEN_S = 0.8;
// No layout to frame (bare stage, failed manifest): fall back to a fixed
// height over the robot rather than leaving the camera wherever it was.
const TOP_FALLBACK_HEIGHT_M = 12;

// Robot-mounted camera views: frames, axis conventions, FOV and near plane
// match the driver's cameras (mars_sim_driver.core's CAMERAS).
export type CameraView = "orbit" | "main" | "arm";
// Track mars_sim_driver/constants.py: per-camera FOVs matching what the
// driver renders (the head and wrist are different physical lenses), so the
// operator's preview frames what the robot consumes. main is the head's real
// ~84 deg vertical; its 116 deg horizontal cannot be matched by a square-pixel
// three.js camera, so the preview runs narrower sideways than the real frame.
const ROBOT_CAMERA_VFOV: Record<"main" | "arm", number> = { main: 83.9, arm: 80 };
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
  private optic?: THREE.MeshStandardMaterial;
  private glass?: THREE.MeshStandardMaterial;
  private cameraEnv?: THREE.Texture;
  private robotCameras = new Map<CameraView, THREE.PerspectiveCamera>();
  private activeView: CameraView = "orbit";
  private lidarPoints?: THREE.Points;
  private keyLight?: THREE.DirectionalLight;
  private shadowCatcher?: THREE.Mesh;
  private shadowBoxM = SHADOW_BOX_MIN_M;
  private robotXY: [number, number] = [0, 0];
  private layoutGroup?: THREE.Group;
  private environmentViewer: EnvironmentViewer = APARTMENT_VIEWER;
  // Bumped by unloadEnvironment: a load still in flight for the previous
  // pack must drop its result rather than attach it to the next one.
  private environmentGeneration = 0;
  private hullsGroup?: THREE.Group;
  private hullsPromise?: Promise<void>;
  private hullsVisible = false;
  // Shared fat-line material for placeholder boxes (LineBasicMaterial's
  // linewidth is ignored by WebGL). resolution is refreshed each render().
  private placeholderMat?: LineMaterial;
  private tmpSize = new THREE.Vector2();
  // Robot-shaped placeholder box, shown at the spawn pose until the STLs load.
  private robotBox?: LineSegments2;
  // Set by the first spawnAt: after that the camera belongs to the robot, and
  // the layout overview framing must not yank it away.
  private spawned = false;
  // The URDF's <collision> subtrees, one per link. They hang off the link in
  // the robot's own graph, so they follow the joints for free.
  private robotColliders: THREE.Object3D[] = [];
  private hullMaterial = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });
  // Every prop in the world, built from the server's roster (props.ts).
  private props: PropLibrary;
  private traffic: TrafficLibrary;
  // While true a placement drag owns the pointer and orbit stays off.
  private placementMode = false;
  private cameraMode: CameraMode = "free";
  /** Notified on every mode change, including one the user caused by grabbing
   * the camera out of chase -- so a mode switch in the UI can reflect it. */
  onCameraModeChange?: (mode: CameraMode) => void;
  // Whole-apartment extent, kept from the layout so "top" can reframe on every
  // entry -- frameLayout only ever runs once, and only before the first pose.
  private layoutBounds?: THREE.Box3;
  private cameraClock = new THREE.Clock();
  // True between OrbitControls' start/end: the pointer or wheel owns the camera.
  private userDriving = false;
  private cameraTween?: {
    fromPos: THREE.Vector3;
    toPos: THREE.Vector3;
    fromTarget: THREE.Vector3;
    toTarget: THREE.Vector3;
    t: number;
  };

  /** Fixed render size (offscreen use, e.g. SimSession); null = track the window. */
  private fixedSize: { width: number; height: number } | null = null;
  /** Canvas width covered on the right by page chrome (see setSafeInsets). */
  private safeInsetRight = 0;

  constructor(canvas: HTMLCanvasElement, opts: { fixedSize?: { width: number; height: number } } = {}) {
    this.fixedSize = opts.fixedSize ?? null;
    const w = this.fixedSize?.width ?? window.innerWidth;
    const h = this.fixedSize?.height ?? window.innerHeight;
    // Pre-pose orbit framing would flash before spawnAt replaces it.
    canvas.style.visibility = "hidden";
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(this.fixedSize ? 1 : Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h, !this.fixedSize);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.5;

    this.scene.background = new THREE.Color(0x14161a);
    this.scene.fog = new THREE.FogExp2(0x14161a, 0.035);

    // Props share the apartment's hull wireframe material, so one "collisions"
    // toggle covers both. A prop entering the world refits the shadow box.
    this.props = new PropLibrary(
      this.scene,
      this.hullMaterial,
      () => this.updateShadowVolume(),
      (model) => this.warmTextures(model),
    );
    this.traffic = new TrafficLibrary(this.scene, this.hullMaterial, () => this.updateShadowVolume());

    this.camera = new THREE.PerspectiveCamera(55, w / h, 0.05, 200);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(-3.5, -3.5, 2.4);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.target.set(0, 0, 0.4);
    // Zoom stops minDistance short of the target and pan scales with the
    // distance to it, so without this you can only ever approach one point.
    this.controls.zoomToCursor = true;
    this.controls.minDistance = 0.15;
    this.controls.maxDistance = 30;
    this.controls.update();
    // Grabbing the camera is a statement that you want it: a drag or a wheel
    // takes chase off and abandons a fly-out mid-arc. "start"/"end" bracket
    // user input only, so this cannot fire for our own per-frame update() or
    // for a programmatic reframe (spawnAt) -- and a click that never moves
    // anything gets a "start" with no "change", so it leaves chase alone.
    this.controls.addEventListener("start", () => {
      this.userDriving = true;
    });
    this.controls.addEventListener("end", () => {
      this.userDriving = false;
    });
    this.controls.addEventListener("change", () => {
      if (!this.userDriving) return;
      this.cameraTween = undefined;
      if (this.cameraMode === "chase") this.setCameraMode("free");
    });

    this.addLights();
    this.addGround();
    // Robot-sized placeholder box inside robotRoot: hidden with it until the
    // first pose (spawnAt), then it marks the real spawn spot while the STLs
    // stream; loadRobot removes it once the meshes are in.
    this.robotBox = this.boxOutline(new THREE.Box3(new THREE.Vector3(-0.22, -0.22, 0), new THREE.Vector3(0.22, 0.22, 0.75)));
    this.robotRoot.add(this.robotBox);
    this.addShadowCatcher();
    this.robotRoot.visible = false;
    this.scene.add(this.robotRoot);

    if (!this.fixedSize) window.addEventListener("resize", () => this.onResize());
  }

  private addLights(): void {
    // No env map on purpose: it grey-washes dark low-roughness materials;
    // several directional lights give the distinct highlights that read as
    // "glossy" on the robot parts.
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.2));

    // The shadow map follows the robot (see setPose) over a 5m box rather than
    // trying to cover the whole flat. That is 2.4mm per texel, fine enough for
    // the arm's ~2cm detail and the manipulation props to cast real contact
    // shadows -- without one under the gripper there is no depth cue to judge
    // a grasp by. It also lets normalBias stay at 12mm; the 50mm it needed
    // over a 16m box erased the shadow of anything smaller than 50mm, i.e.
    // every part that matters here.
    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.castShadow = true;
    key.shadow.mapSize.set(SHADOW_MAP_PX, SHADOW_MAP_PX);
    key.shadow.camera.near = 0.1;
    key.shadow.camera.far = 12;
    key.shadow.camera.left = -SHADOW_BOX_MIN_M;
    key.shadow.camera.right = SHADOW_BOX_MIN_M;
    key.shadow.camera.top = SHADOW_BOX_MIN_M;
    key.shadow.camera.bottom = -SHADOW_BOX_MIN_M;
    // Both biases stay near zero, and that is safe rather than sloppy.
    // Shadow bias exists to stop a surface shadowing ITSELF, and three renders
    // the flipped side into the shadow map (WebGLShadowMap's shadowSide: a
    // FrontSide material casts from its back faces), so the depth stored is
    // already the far side of every closed mesh. The shadow catcher casts
    // nothing at all, so it cannot self-shadow under any circumstances -- for
    // the floor shadow, every millimetre of bias is pure daylight between an
    // object and its own shadow, worst under a sphere or a cube corner. The
    // 0.5mm left is a margin for the robot's own meshes, which do receive.
    key.shadow.bias = 0;
    key.shadow.normalBias = 0.0005;
    // REQUIRED. DirectionalLightShadow builds its camera as
    // OrthographicCamera(-5, 5, 5, -5, 0.5, 500) and computes the projection
    // in that constructor; LightShadow.updateMatrices never recomputes it. So
    // every frustum value set above is ignored until this call, which is why
    // nothing cast a shadow: the box stayed the default 10m centred on the
    // world origin, while the robot spawns 4.3m away and drives off from
    // there. Delete this line and the shadows go away again.
    key.shadow.camera.updateProjectionMatrix();
    key.position.set(...KEY_LIGHT_OFFSET);
    this.scene.add(key);
    this.scene.add(key.target);
    this.keyLight = key;

    const fill = new THREE.DirectionalLight(0xaaccff, 0.8);
    fill.position.set(-4, 3, 3);
    this.scene.add(fill);

    this.scene.add(new THREE.HemisphereLight(0xaabbcc, 0x445566, 1.2));
  }

  /** A transparent plane under the robot that shows nothing but the shadows
   * falling on it.
   *
   * The apartment glb is baked: every one of its materials carries
   * KHR_materials_unlit, so GLTFLoader gives them MeshBasicMaterial, which
   * ignores lights entirely. The shading you see in the flat -- including the
   * shadow under the sofa -- is painted into the texture, and receiveShadow on
   * those meshes does nothing. Without this plane no shadow can ever land on
   * the floor, however the light is set up.
   *
   * It rides with the robot (see setPose) because it only needs to cover the
   * shadow box, and sits ON the physics floor at z=0 so shadows start where
   * the object touches. Where the visual floor is raised (a rug), the rug
   * draws over the shadow. */
  private addShadowCatcher(): void {
    const mesh = new THREE.Mesh(
      // Big enough to cover the widest shadow box; it costs nothing where no
      // shadow lands. PlaneGeometry is already XY / +Z normal, i.e. our floor.
      new THREE.PlaneGeometry(2 * SHADOW_BOX_MAX_M + 2, 2 * SHADOW_BOX_MAX_M + 2),
      // Coplanar with the floor at z=0 rather than lifted off it: a plane
      // floated even 4mm up starts the shadow 4mm up the object's side, which
      // at this light angle is a visible gap under everything. polygonOffset
      // wins the z-fight in depth instead, without moving it in world space.
      new THREE.ShadowMaterial({
        opacity: 0.42,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2,
      }),
    );
    mesh.receiveShadow = true;
    mesh.castShadow = false;
    mesh.renderOrder = 1;
    this.shadowCatcher = mesh;
    this.scene.add(mesh);
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

  /**
   * Wireframe overlay of everything the driver collides with: the robot's own
   * <collision> primitives from mars.urdf (already posed by the URDF graph,
   * so they track the joints) plus the apartment_collisions_v2 hull set
   * mars_sim_driver collides against. The hulls are lazily fetched on first
   * show (manifest.json lists the OBJs since a browser can't list a
   * directory) and rotated Y-up -> Z-up like the apartment glb.
   */
  setCollisionHullsVisible(visible: boolean): void {
    this.hullsVisible = visible;
    this.props.setHullsVisible(visible);
    this.traffic.setHullsVisible(visible);
    if (visible && !this.hullsPromise) {
      // ~1300 OBJ fetches; takes seconds on first show. A failure resets the
      // promise so toggling again retries instead of staying dead forever.
      this.hullsPromise = this.loadCollisionHulls().catch((err) => {
        console.error("[sim-viewer] collision hulls failed to load:", err);
        this.hullsPromise = undefined;
      });
    }
    if (this.hullsGroup) this.hullsGroup.visible = visible;
    for (const collider of this.robotColliders) collider.visible = visible;
  }

  private async loadCollisionHulls(): Promise<void> {
    const generation = this.environmentGeneration;
    const group = new THREE.Group();
    group.rotation.x = Math.PI / 2;
    const baseUrl = `${publicUrl(this.environmentViewer.collision_dir ?? APARTMENT_VIEWER.collision_dir)}/`;
    const material = this.hullMaterial;

    // Fast path: one binary triangle soup (float32 xyz), one fetch, no
    // parsing -- build_viewer_physics.py writes it next to the per-hull OBJs.
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
    if (generation !== this.environmentGeneration) {
      group.traverse((obj) => obj instanceof THREE.Mesh && obj.geometry.dispose()); // hullMaterial is shared
      return;
    }
    group.visible = this.hullsVisible; // honor toggles made while loading
    this.hullsGroup = group;
    this.scene.add(group);
  }

  /**
   * Phase one of the apartment load: fetch the manifest (a few KB) and draw
   * every room's placeholder box. Called before any mesh download starts, so
   * the first frame shows the apartment's wireframe layout instead of an empty
   * void, and the camera has something real to frame (see frameLayout).
   */
  async loadApartmentLayout(viewer: EnvironmentViewer = APARTMENT_VIEWER): Promise<ApartmentLayout> {
    this.environmentViewer = viewer;
    // Exterior packs need a longer, daylight view. Always restore defaults
    // on the next pack so an outdoor visit cannot change indoor rendering.
    const daylight = viewer.atmosphere === "daylight";
    const background = daylight ? 0xcddbe2 : 0x14161a;
    this.scene.background = new THREE.Color(background);
    this.scene.fog = new THREE.FogExp2(background, daylight ? 0.004 : 0.035);
    this.controls.maxDistance = daylight ? 65 : 30;
    // One parent group holds every room and carries the Y-up -> Z-up rotation,
    // so it's applied once; placeholder boxes and rooms attach underneath.
    const group = new THREE.Group();
    group.rotation.x = Math.PI / 2;
    this.scene.add(group);
    this.layoutGroup = group;

    const manifestUrl = viewer.type === "glb" || !viewer.manifest ? null : publicUrl(viewer.manifest);
    let manifest: ApartmentManifest | null = null;
    if (manifestUrl) {
      try {
        const res = await fetch(manifestUrl);
        if (res.ok) manifest = (await res.json()) as ApartmentManifest;
      } catch {
        /* no manifest -- fall through to the monolith */
      }
    }
    if (!manifestUrl || !manifest || !Array.isArray(manifest.rooms)) {
      const modelUrl = viewer.model ? publicUrl(viewer.model) : undefined;
      return { group, rooms: [], monolith: true, baseUrl: "", modelUrl };
    }
    const baseUrl = viewer.base_dir ? `${publicUrl(viewer.base_dir)}/` : manifestUrl.slice(0, manifestUrl.lastIndexOf("/") + 1);

    // Skip a malformed room rather than throwing -- a bad bbox would build a
    // Box3 from Vector3(undefined) and error the whole (visual-only) session.
    const rooms = manifest.rooms
      .filter((room) => {
        if (isValidRoom(room)) return true;
        console.warn("[sim-viewer] skipping malformed manifest room:", room);
        return false;
      })
      .map((room) => {
        const b = room.bbox;
        const box = this.boxOutline(
          new THREE.Box3(
            new THREE.Vector3(b.min[0], b.min[1], b.min[2]),
            new THREE.Vector3(b.max[0], b.max[1], b.max[2]),
          ),
        );
        group.add(box);
        return { room, box };
      });
    return { group, rooms, monolith: false, baseUrl };
  }

  /** Dispose environment assets; retain the robot and props for the next pose. */
  unloadEnvironment(): void {
    this.traffic.unloadEnvironment();
    for (const group of [this.layoutGroup, this.hullsGroup]) {
      if (!group) continue;
      this.scene.remove(group);
      group.traverse((obj) => {
        if (!(obj instanceof THREE.Mesh)) return;
        obj.geometry.dispose();
        if (group === this.layoutGroup) disposeMaterials(obj.material); // hulls share hullMaterial
      });
    }
    this.layoutGroup = this.hullsGroup = this.hullsPromise = this.layoutBounds = undefined;
    this.environmentGeneration += 1;
    this.robotRoot.visible = false;
    this.spawned = false;
  }

  /**
   * Phase two: stream each room's glb through the queue and swap its
   * placeholder box out on arrival. A room that fails is non-fatal (visual
   * only): log it, drop its box, and let the rest of the apartment and the
   * session carry on.
   */
  async streamApartment(queue: LoadQueue, layout: ApartmentLayout): Promise<void> {
    const loader = new GLTFLoader();
    const { group } = layout;

    if (layout.monolith) {
      // Single-glb packs, and the dev fallback for a checkout that never ran
      // the apartment split (the published bundle always ships the manifest).
      // Non-fatal: the sim just runs without the visual environment.
      try {
        if (!layout.modelUrl) throw new Error("the pack names no model");
        const root = await queuedGLB(queue, loader, layout.modelUrl);
        if (group !== this.layoutGroup) {
          disposeObject(root); // the pack was unloaded while this streamed
          return;
        }
        this.dressRoom(root);
        group.add(root);
      } catch (err) {
        console.error("[sim-viewer] environment unavailable (no manifest, no monolith):", err);
      }
      return;
    }

    const loadRoom = ({ room, box }: ApartmentLayout["rooms"][number]) =>
      queuedGLB(queue, loader, `${layout.baseUrl}${room.file}`)
        .then((root) => {
          if (group !== this.layoutGroup) {
            disposeObject(root); // the pack was unloaded while this streamed
            return;
          }
          this.dressRoom(root);
          group.add(root);
        })
        .catch((err) => console.error(`[sim-viewer] apartment room '${room.file}' failed to load:`, err))
        .finally(() => {
          group.remove(box);
          box.geometry.dispose(); // material is shared -- disposed in dispose()
        });

    // Priority rooms (living room, then hallway) load one at a time so they
    // appear in that order -- with the shared queue running 2 downloads at
    // once they'd otherwise race, and the living room is the biggest file, so
    // the smaller hallway would pop in first. The remaining rooms then stream
    // together behind them.
    const ordered = [...layout.rooms].sort((a, b) => roomPriority(a.room.name) - roomPriority(b.room.name));
    const priority = ordered.filter(({ room }) => roomPriority(room.name) < ROOM_PRIORITY.length);
    const rest = ordered.slice(priority.length);
    for (const room of priority) await loadRoom(room);
    await Promise.all(rest.map(loadRoom));
  }

  /**
   * Record the apartment's extent (the "top" camera mode's framing, for the
   * rest of the session) and frame the orbit camera on the placeholder layout,
   * so the wireframe reads as an apartment from the first frame. The framing
   * half is skipped once a real pose has arrived -- spawnAt's close framing
   * wins; that half is only for the pre-pose gap.
   */
  frameLayout(layout: ApartmentLayout): void {
    if (layout.rooms.length === 0) return;
    const bounds = new THREE.Box3().setFromObject(layout.group);
    if (bounds.isEmpty()) return;
    this.layoutBounds = bounds;
    if (this.spawned) return;

    const center = layoutFocus(bounds);
    const size = bounds.getSize(new THREE.Vector3());
    // Pull back far enough that the widest horizontal extent fits the vertical
    // FOV (the horizontal one is wider on any landscape stage), with margin.
    const fov = (this.camera.fov * Math.PI) / 180;
    const fit = (Math.max(size.x, size.y) / 2 / Math.tan(fov / 2)) * 1.25;
    // OrbitControls clamps the distance on update(); pick one it will honor.
    const distance = Math.min(fit, this.controls.maxDistance);
    const dir = new THREE.Vector3(-1, -1, 0.85).normalize();
    this.camera.position.copy(center).addScaledVector(dir, distance);
    this.controls.target.copy(center);
    this.controls.update();
  }

  /** Ready a loaded apartment room for the scene: receive shadows and force
   * FrontSide (the glb ships doubleSided) so walls draw only from inside the
   * room, giving overview cameras the dollhouse-cutaway look. */
  private dressRoom(root: THREE.Object3D): void {
    root.traverse((obj) => {
      if (obj instanceof THREE.Mesh) {
        // Does nothing, kept only so this doesn't look like an oversight: the
        // glb's materials are all KHR_materials_unlit, so GLTFLoader makes
        // them MeshBasicMaterial and they cannot receive a shadow. Their
        // shading is baked into the texture. addShadowCatcher is what puts
        // the robot's own shadow on the floor. castShadow is deliberately
        // left off -- the flat already shades itself.
        obj.receiveShadow = true;
        const setFrontSide = (mat: THREE.Material) => {
          mat.side = THREE.FrontSide;
        };
        if (Array.isArray(obj.material)) obj.material.forEach(setFrontSide);
        else setFrontSide(obj.material);
      }
    });
    this.traffic.registerEnvironment(root);
  }

  /** A thick wireframe outline of a box, used as a loading placeholder (room or
   * robot). Fat lines (LineSegments2) since WebGL ignores LineBasicMaterial width. */
  private boxOutline(box: THREE.Box3): LineSegments2 {
    if (!this.placeholderMat) {
      this.placeholderMat = new LineMaterial({ color: 0x3a6b5a, linewidth: 3, transparent: true, opacity: 0.6 });
      this.placeholderMat.resolution.copy(this.renderer.getDrawingBufferSize(this.tmpSize));
    }
    const size = box.getSize(new THREE.Vector3());
    const boxGeo = new THREE.BoxGeometry(size.x, size.y, size.z);
    const edges = new THREE.EdgesGeometry(boxGeo);
    const geometry = new LineSegmentsGeometry().fromEdgesGeometry(edges);
    boxGeo.dispose();
    edges.dispose();
    const seg = new LineSegments2(geometry, this.placeholderMat);
    seg.position.copy(box.getCenter(new THREE.Vector3()));
    return seg;
  }

  /**
   * Two-phase load. The returned promise resolves as soon as the URDF is parsed
   * and every STL job is in the queue -- so a caller can `await loadRobot(...)`
   * and then enqueue lower-priority work (apartment rooms) knowing it lands
   * behind the robot. Await the returned `done` for the fully-loaded robot.
   */
  async loadRobot(queue: LoadQueue): Promise<{ done: Promise<URDFRobot> }> {
    const loader = new URDFLoader();
    loader.packages = { mars_sim: "/robot" };

    // Route each STL through the shared queue (bounded concurrency + byte
    // progress) instead of URDFLoader's default all-at-once loading.
    const meshLoads: Promise<void>[] = [];
    const loadMesh = (
      path: string,
      manager: THREE.LoadingManager,
      material: THREE.Material,
      done: (obj: THREE.Object3D | null, err?: unknown) => void,
    ): void => {
      meshLoads.push(
        queue.add((report) => {
          return new Promise<void>((resolve) => {
            const finish = (obj: THREE.Object3D | null, err?: unknown) => {
              done(obj, err);
              resolve();
            };
            if (!/\.stl$/i.test(path)) {
              console.warn(`[scene] unsupported robot mesh (expected STL): ${path}`);
              finish(null);
              return;
            }
            new STLLoader(manager).load(
              path,
              (geom) => finish(new THREE.Mesh(geom, material ?? new THREE.MeshPhongMaterial())),
              (ev) => report(ev.loaded, ev.total),
              (err) => finish(null, err),
            );
          });
        }),
      );
    };
    // The shipped .d.ts omits the `material` arg the runtime actually passes.
    loader.loadMeshCb = loadMesh as unknown as typeof loader.loadMeshCb;
    loader.parseCollision = true; // the collisions overlay draws these

    const robot = await loader.loadAsync(ROBOT_URDF_URL); // loadMeshCb enqueued every STL during parse
    return { done: this.finishRobot(robot, meshLoads) };
  }

  /** Attach + restyle the robot once its queued STL meshes have all loaded. */
  private async finishRobot(robot: URDFRobot, meshLoads: Promise<void>[]): Promise<URDFRobot> {
    await Promise.all(meshLoads);

    for (const name of HIDDEN_FRAME_LINKS) {
      const link = robot.links[name];
      if (link) link.visible = false;
    }

    // Collider subtrees first, so the visual restyle below can skip them.
    robot.traverse((obj) => {
      if (!(obj as { isURDFCollider?: boolean }).isURDFCollider) return;
      obj.visible = this.hullsVisible;
      this.robotColliders.push(obj);
      obj.traverse((child) => {
        child.userData.collider = true;
        if (child instanceof THREE.Mesh) child.material = this.hullMaterial;
      });
    });

    let camerasSplit = false;
    robot.traverse((obj) => {
      if (obj.userData.collider) return;
      if (obj instanceof THREE.Mesh) {
        obj.castShadow = true;
        obj.receiveShadow = true;
        const linkName = nearestLinkName(obj);
        if (linkName && ORANGE_LINKS.has(linkName)) {
          obj.material = this.orangeMaterial();
        } else if (Array.isArray(obj.material)) {
          obj.material = obj.material.map((m) => this.toGlossyMaterial(m));
        } else {
          const shell = this.toGlossyMaterial(obj.material);
          if (linkName === CAM_LINK && splitCameraGroups(obj.geometry)) {
            obj.material = [shell, this.glassMaterial()];
            camerasSplit = true;
          } else {
            obj.material = shell;
          }
        }
      }
    });
    const head = robot.links[CAM_LINK];
    if (camerasSplit && head !== undefined) head.add(this.buildCameraModule());

    this.robotRoot.add(robot);
    this.robot = robot;
    if (this.robotBox) {
      this.robotRoot.remove(this.robotBox);
      this.robotBox.geometry.dispose(); // material is shared -- disposed in dispose()
      this.robotBox = undefined;
    }

    for (const mount of ROBOT_CAMERA_MOUNTS) {
      const frame = robot.frames[mount.frame];
      if (!frame) {
        console.warn(`[scene] camera frame "${mount.frame}" not found in URDF -- "${mount.view}" view unavailable`);
        continue;
      }
      const cam = new THREE.PerspectiveCamera(
        ROBOT_CAMERA_VFOV[mount.view],
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

  /** Fit the shadow box around the robot AND every prop in the world, then
   * park the light and the catcher on it.
   *
   * Pinned to the robot, anything left behind stops having a shadow the moment
   * it leaves the box, which just reads as a bug. Sized to the whole set
   * always, one prop dropped across the room costs sharpness everywhere. So it
   * tracks the actual spread, quantised so it is not resized every frame, and
   * stops growing at SHADOW_BOX_MAX_M -- past that a distant prop loses its
   * shadow rather than blurring the robot's, which is the part being looked
   * at. normalBias follows the texel size, since its whole job is to clear
   * about one texel. */
  private updateShadowVolume(): void {
    const key = this.keyLight;
    if (!key) return;

    // Props further than the box could ever reach are dropped rather than
    // dragging the centre off the robot: at the cap, a midpoint between the
    // two would push the robot itself out of its own shadow box.
    const reach = SHADOW_BOX_MAX_M - SHADOW_MARGIN_M;
    const points: Array<[number, number]> = [this.robotXY];
    for (const root of this.props.visibleRoots) {
      const dx = root.position.x - this.robotXY[0];
      const dy = root.position.y - this.robotXY[1];
      if (Math.hypot(dx, dy) <= reach) points.push([root.position.x, root.position.y]);
    }
    for (const bounds of this.traffic.visibleBounds) {
      const dx = Math.max(bounds.minX - this.robotXY[0], 0, this.robotXY[0] - bounds.maxX);
      const dy = Math.max(bounds.minY - this.robotXY[1], 0, this.robotXY[1] - bounds.maxY);
      if (Math.hypot(dx, dy) <= reach) {
        points.push(
          [bounds.minX, bounds.minY],
          [bounds.minX, bounds.maxY],
          [bounds.maxX, bounds.minY],
          [bounds.maxX, bounds.maxY],
        );
      }
    }
    const xs = points.map((pt) => pt[0]);
    const ys = points.map((pt) => pt[1]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    // Radius, not half-width: the box is axis-aligned in the LIGHT's frame,
    // not the world's. SHADOW_MARGIN_M covers the robot's own extent and the
    // throw of its shadow beyond whichever point is furthest out.
    const needed = Math.max(...points.map((pt) => Math.hypot(pt[0] - cx, pt[1] - cy))) + SHADOW_MARGIN_M;
    const box = Math.min(
      SHADOW_BOX_MAX_M,
      Math.max(SHADOW_BOX_MIN_M, Math.ceil(needed / SHADOW_BOX_STEP_M) * SHADOW_BOX_STEP_M),
    );
    if (box !== this.shadowBoxM) {
      this.shadowBoxM = box;
      key.shadow.camera.left = -box;
      key.shadow.camera.right = box;
      key.shadow.camera.top = box;
      key.shadow.camera.bottom = -box;
      key.shadow.camera.updateProjectionMatrix(); // never automatic; see addLights
      key.shadow.normalBias = Math.max(0.0005, ((2 * box) / SHADOW_MAP_PX) * 0.8);
    }

    // Snap to whole texels: slid continuously the map re-samples every frame
    // and the shadow edges crawl, which reads as cheap however sharp they are.
    const texel = (2 * box) / SHADOW_MAP_PX;
    const sx = Math.round(cx / texel) * texel;
    const sy = Math.round(cy / texel) * texel;
    key.position.set(sx + KEY_LIGHT_OFFSET[0], sy + KEY_LIGHT_OFFSET[1], KEY_LIGHT_OFFSET[2]);
    key.target.position.set(sx, sy, 0);
    key.target.updateMatrixWorld();
    this.shadowCatcher?.position.set(sx, sy, 0);
  }

  /** Views actually available (a frame can be missing from the URDF). */
  get availableViews(): CameraView[] {
    return ["orbit", ...this.robotCameras.keys()];
  }

  /** Switch what render() draws: the orbit camera or a robot-mounted one.
   * Falls back to orbit if the requested view's frame wasn't in the URDF. */
  setView(view: CameraView): void {
    this.activeView = view === "orbit" || this.robotCameras.has(view) ? view : "orbit";
    this.applyControlsEnabled();
  }

  /** While a placement drag owns the pointer the orbit controls stay off,
   * even in the orbit view -- otherwise aiming a prop also spins the camera.
   * Applied immediately: a flag that is only consulted by setView() would not
   * take effect until the user happened to switch views. */
  setPlacementMode(on: boolean): void {
    this.placementMode = on;
    this.applyControlsEnabled();
  }

  /** Pick how the orbit camera behaves; see CameraMode. "top" flies out to the
   * apartment framing, after which it is an ordinary orbit you can drag. */
  setCameraMode(mode: CameraMode): void {
    if (mode === this.cameraMode) return;
    this.cameraMode = mode;
    this.cameraTween = undefined;
    this.cameraClock.getDelta(); // drop the gap since the last frame, or the first step is a jump
    if (mode === "top") this.flyToOverview();
    this.applyControlsEnabled();
    this.onCameraModeChange?.(mode);
  }

  private applyControlsEnabled(): void {
    this.controls.enabled = this.activeView === "orbit" && !this.placementMode;
  }

  /** Start the pull-back onto the whole apartment (or, lacking a layout, onto
   * the robot). */
  private flyToOverview(): void {
    const bounds = this.layoutBounds;
    const center = bounds ? layoutFocus(bounds) : new THREE.Vector3(...this.robotXY, 0);
    let distance = TOP_FALLBACK_HEIGHT_M;
    if (bounds) {
      const size = bounds.getSize(new THREE.Vector3());
      // Each world axis against the FOV it actually falls under -- looking down
      // with up=+Z puts world X across the screen and Y up it. Fitting both to
      // the vertical FOV (what frameLayout does, for a stage it knows is
      // landscape) crops the apartment sideways on a stage narrower than tall.
      const vFov = (this.camera.fov * Math.PI) / 180;
      const hFov = 2 * Math.atan(Math.tan(vFov / 2) * this.camera.aspect);
      const fit = Math.max(size.x / 2 / Math.tan(hFov / 2), size.y / 2 / Math.tan(vFov / 2));
      distance = fit * TOP_FIT_MARGIN;
    }
    this.cameraTween = {
      fromPos: this.camera.position.clone(),
      toPos: center.clone().addScaledVector(TOP_TILT, Math.min(distance, this.controls.maxDistance)),
      fromTarget: this.controls.target.clone(),
      toTarget: center,
      t: 0,
    };
  }

  /** Swing the camera along its arc to the tween's destination.
   *
   * The offset from the target is slerped and its length lerped, rather than
   * lerping the position outright: a straight line from a close chase to a
   * ceiling-height overview cuts through the apartment on the way. */
  private advanceTween(dt: number): void {
    const tween = this.cameraTween;
    if (!tween) return;
    tween.t = Math.min(1, tween.t + dt / TOP_TWEEN_S);
    const k = tween.t * tween.t * (3 - 2 * tween.t); // smoothstep: no jerk at either end
    const from = tween.fromPos.clone().sub(tween.fromTarget);
    const to = tween.toPos.clone().sub(tween.toTarget);
    const radius = THREE.MathUtils.lerp(from.length(), to.length(), k);
    from.normalize();
    to.normalize();
    // Identity at k=0, the full from->to rotation at k=1.
    const swing = new THREE.Quaternion().setFromUnitVectors(from, to).slerp(new THREE.Quaternion(), 1 - k);
    const offset = from.applyQuaternion(swing).multiplyScalar(radius);
    this.controls.target.lerpVectors(tween.fromTarget, tween.toTarget, k);
    this.camera.position.copy(this.controls.target).add(offset);
    this.camera.lookAt(this.controls.target);
    if (tween.t >= 1) this.cameraTween = undefined;
  }

  /** Ease the camera toward its perch behind the robot. Frame-rate independent:
   * the same lag whether the tab renders at 30fps or 120. */
  private updateChase(dt: number): void {
    const [x, y] = this.robotXY;
    const yaw = this.robotRoot.rotation.z;
    const desired = new THREE.Vector3(
      x - Math.cos(yaw) * CHASE_BACK_M,
      y - Math.sin(yaw) * CHASE_BACK_M,
      CHASE_HEIGHT_M,
    );
    const target = new THREE.Vector3(x, y, CHASE_TARGET_HEIGHT_M);
    const alpha = 1 - Math.exp(-CHASE_LAG_HZ * dt);
    this.camera.position.lerp(desired, alpha);
    this.controls.target.lerp(target, alpha);
    this.camera.lookAt(this.controls.target);
  }

  /** One camera step, whoever owns it this frame. */
  private stepCamera(): void {
    const dt = Math.min(this.cameraClock.getDelta(), 0.1);
    if (this.cameraMode === "chase") this.updateChase(dt);
    else if (this.cameraTween) this.advanceTween(dt);
    else this.controls.update();
  }

  /** Intersect a canvas pointer position with the floor plane (z=0) through
   * the active view's camera; null when it points above the horizon. */
  screenToFloor(clientX: number, clientY: number): THREE.Vector3 | null {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1,
    );
    const cam = (this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined) ?? this.camera;
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(ndc, cam);
    const hit = new THREE.Vector3();
    return raycaster.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 0, 1), 0), hit) ? hit : null;
  }

  showPropPlacementPreview(name: string, x: number, y: number, yaw: number): void {
    this.props.showPlacementPreview(name, x, y, yaw);
  }

  clearPropPlacementPreview(): void {
    this.props.clearPlacementPreview();
  }

  /** Drive the URDF's arm/head joints to match physics-simulated angles (radians). */
  setJointAngles(joints: Record<string, number>): void {
    this.robot?.setJointValues(joints);
  }

  /** Adopt the world server's prop roster (props.py sidecars): what exists,
   * what to call it, and how to draw it. Sent once per observer connection. */
  setPropManifest(props: PropInfo[]): void {
    this.props.setManifest(props);
  }

  /** Parse the prop models ahead of any drop. Call once the robot and
   * apartment have finished, so the props queue behind them. */
  prefetchPropModels(): void {
    this.props.prefetchModels();
  }

  /** Upload a freshly parsed model's textures now. Decoding is what prefetch
   * moved off the drop; without this the upload itself (67 MB for the human)
   * still hitches the frame the prop first renders on. */
  private warmTextures(model: THREE.Object3D): void {
    model.traverse((obj) => {
      if (!(obj instanceof THREE.Mesh)) return;
      for (const material of Array.isArray(obj.material) ? obj.material : [obj.material]) {
        for (const value of Object.values(material)) {
          if (value instanceof THREE.Texture) this.renderer.initTexture(value);
        }
      }
    });
  }

  /** Mirror every prop in the world from ground truth, keyed by name
   * ({name: [x, y, z, qw, qx, qy, qz]} -- world_server's "objects" block).
   * A prop the block stops naming has left the world and is hidden rather
   * than left behind at its last pose. */
  setObjectPoses(poses: Record<string, number[]>): void {
    this.props.setPoses(poses);
    this.updateShadowVolume(); // the props moved; the box may need to grow or shrink
  }

  /** Adopt the world server's procedural traffic roster. Cars intentionally
   * stay separate from manipulation props and their Clear/challenge flows. */
  setTrafficManifest(manifest: TrafficManifest | null): void {
    this.traffic.setManifest(manifest);
  }

  /** Complete the environment/traffic asset contract before revealing a
   * hot-swap candidate. */
  markEnvironmentReady(): void {
    this.traffic.markEnvironmentReady();
  }

  /** Mirror authoritative signal aspects and car poses from MuJoCo. */
  setTrafficState(state: TrafficState | null): void {
    this.traffic.setState(state);
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

  // Rough on purpose: a near-mirror lens reflects the environment's bright
  // panels as a hard comma, and two of those read as cartoon eyes.
  private opticMaterial(): THREE.MeshStandardMaterial {
    if (!this.optic) {
      this.optic = new THREE.MeshStandardMaterial({
        color: new THREE.Color(0.012, 0.012, 0.014),
        metalness: 0,
        roughness: 0.22,
        envMap: this.cameraEnvironment(),
        envMapRotation: CAM_ENV_ROTATION,
        envMapIntensity: 0.9,
        side: THREE.DoubleSide, // see orangeMaterial
      });
    }
    return this.optic;
  }

  // opacity trades against the highlight: three attenuates specular by it too.
  private glassMaterial(): THREE.MeshStandardMaterial {
    if (!this.glass) {
      this.glass = new THREE.MeshStandardMaterial({
        color: new THREE.Color(0.03, 0.03, 0.035),
        metalness: 0,
        roughness: 0.06,
        envMap: this.cameraEnvironment(),
        envMapRotation: CAM_ENV_ROTATION,
        envMapIntensity: 1.6,
        transparent: true,
        opacity: 0.45,
        side: THREE.FrontSide, // a cover: back faces would tint it twice
      });
    }
    return this.glass;
  }

  // Scoped here, not scene-wide (see addLights), but a dielectric passes only
  // ~4% head-on so these two are unreadable without it. Blurred to a wash.
  private cameraEnvironment(): THREE.Texture {
    if (!this.cameraEnv) {
      const pmrem = new THREE.PMREMGenerator(this.renderer);
      this.cameraEnv = pmrem.fromScene(new RoomEnvironment(), 0.8).texture;
      pmrem.dispose();
    }
    return this.cameraEnv;
  }

  /** What sits inside each barrel, behind the STL's own glass dome. */
  private buildCameraModule(): THREE.Group {
    const module = new THREE.Group();
    for (const [y, z] of CAM_CENTRES) {
      const back = new THREE.Mesh(barrelBackGeometry(), this.opticMaterial());
      back.position.set(CAM_BACK_X, y, z);
      const lens = new THREE.Mesh(lensCapGeometry(), this.opticMaterial());
      lens.position.set(CAM_LENS_X, y, z);
      back.receiveShadow = true;
      lens.receiveShadow = true;
      module.add(back, lens);
    }
    return module;
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
   * Place the robot at a pose immediately and frame the camera facing its
   * front, rather than following the incremental delta used by setPose.
   * Use once — e.g. on spawn or reset — before driving resumes.
   */
  spawnAt(x: number, y: number, yaw: number): void {
    this.spawned = true;
    this.robotRoot.visible = true;
    this.robotRoot.position.set(x, y, 0);
    this.robotRoot.rotation.set(0, 0, yaw);
    this.frameFacing(x, y, yaw);
    this.renderer.domElement.style.visibility = "";
    this.followPrevXY = [x, y];
  }

  /** Re-frame on the robot where it stands (see simStage's attach). */
  frameRobot(): void {
    if (!this.spawned) return; // no real pose yet -- spawnAt still owes the first framing
    this.frameFacing(this.robotRoot.position.x, this.robotRoot.position.y, this.robotRoot.rotation.z);
  }

  private frameFacing(x: number, y: number, yaw: number): void {
    this.cameraTween = undefined; // an overview fly-out in flight would drag it straight back off

    const forwardX = Math.cos(yaw);
    const forwardY = Math.sin(yaw);
    const leftX = -forwardY;
    const leftY = forwardX;
    const target = new THREE.Vector3(
      x + forwardX * INITIAL_ORBIT_TARGET.forward + leftX * INITIAL_ORBIT_TARGET.left,
      y + forwardY * INITIAL_ORBIT_TARGET.forward + leftY * INITIAL_ORBIT_TARGET.left,
      INITIAL_ORBIT_TARGET.height,
    );
    const perch = new THREE.Vector3(
      x + forwardX * INITIAL_ORBIT_POSITION.forward + leftX * INITIAL_ORBIT_POSITION.left,
      y + forwardY * INITIAL_ORBIT_POSITION.forward + leftY * INITIAL_ORBIT_POSITION.left,
      INITIAL_ORBIT_POSITION.height,
    );
    // The vertical FOV is fixed and the horizontal narrows with the aspect,
    // so a portrait stage would crop the arm.
    const pullback = Math.max(1, 1 / this.camera.aspect);
    this.camera.position.copy(target).addScaledVector(perch.sub(target), pullback);
    this.controls.target.copy(target);
    this.controls.update();
  }

  /** Move the robot root to a 2D pose (meters, yaw radians about +Z). */
  setPose(x: number, y: number, yaw: number): void {
    this.robotRoot.position.set(x, y, 0);
    this.robotRoot.rotation.set(0, 0, yaw);
    this.robotXY = [x, y];
    this.updateShadowVolume();

    // Recorded even when nothing consumes it: a mode that parks the follow
    // (chase, top) must not hand free-orbit the whole distance travelled while
    // it was away as one jump.
    const [prevX, prevY] = this.followPrevXY;
    this.followPrevXY = [x, y];
    if (this.followCamera && this.cameraMode === "free") {
      const dx = x - prevX;
      const dy = y - prevY;
      this.camera.position.x += dx;
      this.camera.position.y += dy;
      this.controls.target.x += dx;
      this.controls.target.y += dy;
    }
  }

  render(): void {
    const robotCam = this.activeView !== "orbit" ? this.robotCameras.get(this.activeView) : undefined;
    if (!robotCam) this.stepCamera();
    // Fat lines need the current drawing-buffer size to size their width in px.
    this.placeholderMat?.resolution.copy(this.renderer.getDrawingBufferSize(this.tmpSize));
    this.renderer.render(this.scene, robotCam ?? this.camera);
  }

  /** Release the GL context + control listeners: the SPA router remounts the
   * stage per visit, and undisposed contexts pile up until the browser kills
   * the oldest (~16), breaking the live view. */
  dispose(): void {
    this.props.clearPlacementPreview();
    this.traffic.dispose();
    this.placeholderMat?.dispose();
    this.cameraEnv?.dispose(); // a PMREM render target, not a loaded image
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
    this.applyViewOffset();
    for (const cam of this.robotCameras.values()) {
      cam.aspect = width / height;
      cam.updateProjectionMatrix();
    }
  }

  /** How much of the canvas's right edge page chrome covers. Only the framing
   * moves; the whole canvas still renders. */
  setSafeInsets(insets: { right?: number }): void {
    const right = Math.max(0, insets.right ?? 0);
    if (right === this.safeInsetRight) return;
    this.safeInsetRight = right;
    this.applyViewOffset();
  }

  /** Bias the framing off dead centre: sideways for whatever covers the right
   * edge, upward on a portrait stage. Robot cameras are untouched. */
  private applyViewOffset(): void {
    const { width, height } = this.viewSize();
    const offsetX = this.safeInsetRight / 2;
    const offsetY = height > width * 1.2 ? height * 0.1 : 0;
    if (offsetX === 0 && offsetY === 0) {
      this.camera.clearViewOffset();
      return;
    }
    this.camera.setViewOffset(width, height, offsetX, offsetY, width, height);
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
    this.applyViewOffset();
    for (const cam of this.robotCameras.values()) {
      cam.aspect = w / h;
      cam.updateProjectionMatrix();
    }
  }
}

/** The flat's centre, on the floor. The bounding box spans floor to ceiling, so
 * its centre is mid-air -- and zoom stops minDistance short of the target, which
 * would leave the camera stuck a metre up. */
function layoutFocus(bounds: THREE.Box3): THREE.Vector3 {
  const focus = bounds.getCenter(new THREE.Vector3());
  focus.z = bounds.min.z;
  return focus;
}

/** Free a loaded subtree's GPU-bound resources (geometries, materials, textures). */
function disposeObject(root: THREE.Object3D): void {
  root.traverse((obj) => {
    if (!(obj instanceof THREE.Mesh)) return;
    obj.geometry.dispose();
    disposeMaterials(obj.material);
  });
}

function disposeMaterials(material: THREE.Material | THREE.Material[]): void {
  for (const mat of Array.isArray(material) ? material : [material]) {
    for (const value of Object.values(mat)) if (value instanceof THREE.Texture) value.dispose();
    mat.dispose();
  }
}

/** Runtime guard against a malformed manifest (untrusted JSON): file is a
 * string and bbox is two arrays of >=3 finite numbers -- else we'd build a Box3
 * from Vector3(undefined) and get NaN geometry. */
function isValidRoom(room: unknown): boolean {
  const r = room as { file?: unknown; bbox?: { min?: unknown; max?: unknown } } | null;
  const finite3 = (a: unknown): boolean =>
    Array.isArray(a) && a.length >= 3 && a.slice(0, 3).every((n) => typeof n === "number" && Number.isFinite(n));
  return typeof r?.file === "string" && finite3(r?.bbox?.min) && finite3(r?.bbox?.max);
}

/** Closes the hole under the barrel, so the glass does not see into the head. */
function barrelBackGeometry(): THREE.BufferGeometry {
  const geometry = new THREE.CircleGeometry(CAM_BACK_RADIUS, 48);
  geometry.rotateY(Math.PI / 2);
  return geometry;
}

/** A shallow spherical cap, apex on the origin, bulging along +X. */
function lensCapGeometry(): THREE.BufferGeometry {
  const opening = Math.asin(CAM_LENS_RADIUS / CAM_LENS_SPHERE);
  const geometry = new THREE.SphereGeometry(CAM_LENS_SPHERE, 48, 12, 0, Math.PI * 2, 0, opening);
  geometry.rotateZ(-Math.PI / 2); // the cap sits on the +Y pole; point it at +X
  geometry.translate(-CAM_LENS_SPHERE, 0, 0);
  return geometry;
}

/** Split the head mesh into shell / glass groups; group index is the caller's
 * material array order. */
function splitCameraGroups(geometry: THREE.BufferGeometry): boolean {
  const position = geometry.getAttribute("position");
  if (geometry.index !== null || !(position instanceof THREE.BufferAttribute)) return false;

  const shell: number[] = [];
  const glass: number[] = [];
  for (let triangle = 0; triangle < position.count / 3; triangle++) {
    (isDomeTriangle(position, triangle) ? glass : shell).push(triangle);
  }
  if (glass.length === 0) return false;

  // Indexing beats reordering the buffers, and welding the dome's vertices --
  // only its own, so the crease where it meets the rim survives -- lets one
  // computeVertexNormals smooth it while every flat facet keeps its normal.
  const welded = new Map<string, number>();
  const index: number[] = [];
  for (const triangle of shell) index.push(triangle * 3, triangle * 3 + 1, triangle * 3 + 2);
  for (const triangle of glass) {
    for (let vertex = triangle * 3; vertex < triangle * 3 + 3; vertex++) {
      const key = [position.getX(vertex), position.getY(vertex), position.getZ(vertex)]
        .map((v) => Math.round(v / CAM_WELD_M))
        .join(":");
      const first = welded.get(key);
      if (first === undefined) welded.set(key, vertex);
      index.push(first ?? vertex);
    }
  }
  geometry.setIndex(index);
  geometry.clearGroups();
  geometry.addGroup(0, shell.length * 3, 0);
  geometry.addGroup(shell.length * 3, glass.length * 3, 1);
  geometry.computeVertexNormals();
  return true;
}

/** All three vertices inside ONE dome -- testing them against the pair instead
 * admits the triangles bridging the eyes, which render as a bar. */
function isDomeTriangle(position: THREE.BufferAttribute, triangle: number): boolean {
  return CAM_CENTRES.some(([cy, cz]) => {
    for (let vertex = triangle * 3; vertex < triangle * 3 + 3; vertex++) {
      if (position.getX(vertex) < CAM_GLASS_MIN_X) return false;
      if (Math.hypot(position.getY(vertex) - cy, position.getZ(vertex) - cz) > CAM_GLASS_RADIUS) return false;
    }
    return true;
  });
}

/** Walk up the parent chain to the URDFLink a mesh belongs to, returning its name. */
function nearestLinkName(obj: THREE.Object3D): string | null {
  for (let cur: THREE.Object3D | null = obj; cur; cur = cur.parent) {
    if ((cur as { isURDFLink?: boolean }).isURDFLink) return cur.name;
  }
  return null;
}
