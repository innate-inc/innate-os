// Prop glbs, parsed before the drop that needs one.
//
// The cost of a prop's model is not its download: the human is 23 MB of glb
// but 67 MB of decoded 2048px texture, and decoding that at drop time is what
// made a prop land as a coloured box and morph a beat later. So every model is
// parsed once the robot and apartment are done, and a drop takes the finished
// Group from here.
//
// Each model is a single instance, which holds because prop -> glb is 1:1; two
// props sharing a glb would fight over the same object.
//
// The PropInfo import is type-only (erased at runtime), so this module and
// props.ts do not form a runtime cycle.

import * as THREE from "three";
import { MeshoptDecoder } from "three/examples/jsm/libs/meshopt_decoder.module.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import type { PropInfo, PropViewerDef } from "./props";

/** Rescale + re-origin a raw glb into its MuJoCo body's local frame (glb
 * exports bake arbitrary origins, orientations and unit scales). */
function normalizeModel(scene: THREE.Object3D, def: PropViewerDef): void {
  if (def.preNormalized) return;
  if (def.rotateToZUp !== false) scene.rotation.x = Math.PI / 2; // glTF Y-up -> scene Z-up
  scene.updateMatrixWorld(true);
  const size = new THREE.Box3().setFromObject(scene).getSize(new THREE.Vector3());
  // `rotateToZUp: false` also supports intentionally unrotated Y-up models
  // (the lying rescue human). Infer which authored axis is vertical from the
  // dominant Y/Z extent so already-Z-up models remain usable with old rosters.
  const zUp = def.rotateToZUp !== false || size.z >= size.y;
  const upExtent = zUp ? size.z : size.y;
  const span = def.fitDim === "height" ? upExtent : Math.max(size.x, size.y, size.z);
  if (def.fitSizeM && span > 0) scene.scale.multiplyScalar(def.fitSizeM / span);

  // Re-measure post-scale to place the origin.
  scene.updateMatrixWorld(true);
  const scaled = new THREE.Box3().setFromObject(scene);
  const center = scaled.getCenter(new THREE.Vector3());
  if (def.origin !== "base") {
    scene.position.sub(center);
    return;
  }
  // base: centred in the ground plane, up-axis min sitting at the origin.
  scene.position.x -= center.x;
  if (zUp) {
    scene.position.y -= center.y;
    scene.position.z -= scaled.min.z;
  } else {
    scene.position.z -= center.z;
    scene.position.y -= scaled.min.y;
  }
}

/** Every prop model, parsed at most once and kept for the drops that follow. */
export class PropModels {
  #loads = new Map<string, Promise<THREE.Group | null>>();
  #ready = new Map<string, THREE.Group>();
  #failed = new Set<string>();

  /** `onReady` fires as each model finishes parsing (texture warming). */
  constructor(private onReady: (model: THREE.Group) => void = () => {}) {}

  /** Parse every model in the roster. Props without a glb are skipped, and a
   * parse already under way is left alone, so repeat calls are free. */
  prefetch(infos: Iterable<PropInfo>): void {
    for (const info of infos) {
      if (info.viewer.glb) void this.load(info);
    }
  }

  /** The parsed model, if it is ready this frame. */
  get(name: string): THREE.Group | undefined {
    return this.#ready.get(name);
  }

  /** True once we know no model is coming, so callers stop waiting on one. */
  hasFailed(name: string): boolean {
    return this.#failed.has(name);
  }

  /** This prop's parse, started at most once. */
  load(info: PropInfo): Promise<THREE.Group | null> {
    const started = this.#loads.get(info.name);
    if (started) return started;
    const load = this.#parse(info);
    this.#loads.set(info.name, load);
    return load;
  }

  async #parse(info: PropInfo): Promise<THREE.Group | null> {
    try {
      const gltf = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).loadAsync(info.viewer.glb!);
      normalizeModel(gltf.scene, info.viewer);
      gltf.scene.traverse((obj) => {
        if (obj instanceof THREE.Mesh) {
          obj.castShadow = true;
          obj.receiveShadow = true;
        }
      });
      this.#ready.set(info.name, gltf.scene);
      this.onReady(gltf.scene);
      return gltf.scene;
    } catch (err) {
      // Expected whenever the asset bundle predates this prop: the caller keeps
      // the primitive, which is what physics is using anyway.
      this.#failed.add(info.name);
      console.warn(`[sim-viewer] prop '${info.name}' has no model (${info.viewer.glb}); drawing its primitive`, err);
      return null;
    }
  }
}
