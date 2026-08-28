// Droppable props in the browser: one class covering both kinds the world
// server serves (see mars_sim_driver/props.py).
//
// The server sends a roster once per observer connection -- name, label, and
// how to draw each prop -- so adding a prop is a sidecar in sim/props/ and
// nothing here. Two ways a prop gets a body:
//
//   - a glb named by its sidecar's `viewer.glb`, normalized into the SAME
//     local frame its MuJoCo body uses so one pose quaternion orients both;
//   - otherwise (no glb, or the asset bundle never shipped it) the same
//     primitive physics is using, built from `collision`/`size`/`rgba`.
//
// The second is not a degraded path to apologise for: most props ARE
// primitives, and a prop whose mesh is missing still has to be visible or the
// 3D view disagrees with what the robot's cameras see.
//
// Models come from PropModels, which parses them ahead of any drop; this file
// only decides how long to wait for one.

import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import { clone as cloneSkeleton } from "three/examples/jsm/utils/SkeletonUtils.js";
import { decodeDeformableSkin, skinDeformablePositions, type DeformableSkin } from "./deformableSkin";
import type { DeformableFrame } from "./physics/deformableFrame";
import { PropModels } from "./propModels";

/** How long a prop with a glb stays undrawn waiting for its model before we
 * settle for the primitive. A prefetched model is ready well inside this, so
 * the window only opens for a drop that beats its own prefetch. */
const MODEL_GRACE_MS = 300;

/** Browser assets and stream identity for a deformable prop. */
export interface PropDeformableViewerDef {
  id: number;
  controlVertexCount: number;
  renderVertexCount: number;
  skin: string;
  /** Runtime IDF1 positions are world-space (the glb remains an identity local rest mesh). */
  space: "world";
}

/** How the browser should place a prop's glb into its MuJoCo body frame. */
export interface PropViewerDef {
  glb?: string;
  /** The model is already in metres, Z-up, and authored around its body origin. */
  preNormalized?: boolean;
  /** Standard glTF Y-up -> scene Z-up. False for a model already authored Z-up. */
  rotateToZUp?: boolean;
  /** Rescale so fitDim spans this many metres. */
  fitSizeM?: number;
  /** "height" = up-axis extent; "max" = largest bbox side. */
  fitDim?: "height" | "max";
  /** Where the body origin sits: feet-down vs geometric centre. */
  origin?: "base" | "center";
  /** CoACD hull soup (float32 xyz) in the body frame, for the collision overlay. */
  hulls?: string;
  /** Draw the prop title as a browser-only billboard above the model. */
  nameLabel?: boolean;
  /** Body-frame height of the billboard's bottom edge. */
  nameLabelHeightM?: number;
  /** Present when this model is CPU-skinned from streamed control vertices. */
  deformable?: PropDeformableViewerDef;
}

function makeNameLabel(text: string, heightM: number): THREE.Sprite {
  const fontPx = 52;
  const padX = 28;
  const padY = 16;
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (!context) throw new Error("2D canvas is unavailable for prop name label");
  context.font = `600 ${fontPx}px system-ui, sans-serif`;
  canvas.width = Math.ceil(context.measureText(text).width + padX * 2);
  canvas.height = fontPx + padY * 2;

  context.font = `600 ${fontPx}px system-ui, sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillStyle = "rgba(12, 14, 18, 0.82)";
  context.beginPath();
  context.roundRect(0, 0, canvas.width, canvas.height, 18);
  context.fill();
  context.fillStyle = "white";
  context.fillText(text, canvas.width / 2, canvas.height / 2 + 1);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.minFilter = THREE.LinearFilter;
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, toneMapped: false }),
  );
  const labelHeightM = 0.22;
  sprite.scale.set(labelHeightM * (canvas.width / canvas.height), labelHeightM, 1);
  sprite.center.set(0.5, 0);
  sprite.position.z = heightM;
  sprite.renderOrder = 10;
  sprite.userData.isNameLabel = true;
  return sprite;
}

function nameLabelOf(root: THREE.Object3D): THREE.Sprite | undefined {
  return root.children.find((child) => child.userData.isNameLabel) as THREE.Sprite | undefined;
}

/** Body-frame z just above everything drawn under `root` (label excluded). */
function labelAnchorZ(root: THREE.Object3D): number {
  const label = nameLabelOf(root);
  if (label) root.remove(label);
  root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root);
  if (label) root.add(label);
  const top = box.isEmpty() ? 0.42 : box.max.z - root.getWorldPosition(new THREE.Vector3()).z;
  return top + 0.08;
}

/** One prop as the world server describes it (props.py Prop.manifest). */
export interface PropInfo {
  name: string;
  label: string;
  title: string;
  /** Props laid out together by one click, or null for a prop that is only
   * ever placed deliberately (props.py `group`). */
  group: string | null;
  collision: string;
  size: number[];
  /** Wall thickness of an "open_box" (props.py `wall`). */
  wall: number;
  rgba: number[];
  viewer: PropViewerDef;
}

interface PlacementPreview {
  name: string;
  root: THREE.Group;
  materials: THREE.Material[];
  geometries: THREE.BufferGeometry[];
}

/** Build the primitive MuJoCo is colliding with. Mirrors props.py's
 * _PRIMITIVE_FOR_SIZE: "hull"/"pieces" name a mesh rather than a shape, so a
 * prop whose mesh is absent falls back on what its `size` implies. */
function primitiveGeometry(info: PropInfo): THREE.BufferGeometry {
  const s = info.size;
  let shape = info.collision;
  if (shape === "open_box") return openBoxGeometry(s, info.wall);
  if (shape !== "box" && shape !== "sphere" && shape !== "cylinder") {
    shape = s.length === 1 ? "sphere" : s.length === 2 ? "cylinder" : "box";
  }
  if (shape === "sphere") {
    // Finely tessellated: a coarse sphere casts a visibly polygonal shadow
    // right where it meets the floor, which is where the eye checks contact.
    return new THREE.SphereGeometry(s[0], 32, 24);
  }
  if (shape === "cylinder") {
    // MuJoCo sizes a cylinder (radius, half-length) and stands it on +z;
    // THREE's is (radius, radius, length) and Y-up.
    return new THREE.CylinderGeometry(s[0], s[0], s[1] * 2, 40).rotateX(Math.PI / 2);
  }
  // MuJoCo box sizes are half-extents.
  return new THREE.BoxGeometry(s[0] * 2, s[1] * 2, s[2] * 2);
}

/** Floor plus four walls, hollow above — the same five box geoms props.py
 * writes for an open_box, so a crate reads as something to drop into rather
 * than a solid block. */
function openBoxGeometry(s: number[], wall: number): THREE.BufferGeometry {
  const [hx, hy, hz] = s;
  const w = Math.min(wall, hx, hy, hz);
  const slab = (sx: number, sy: number, sz: number, x: number, y: number, z: number) =>
    new THREE.BoxGeometry(sx, sy, sz).translate(x, y, z);
  return mergeGeometries([
    slab(hx * 2, hy * 2, w, 0, 0, -hz + w / 2),
    slab(w, hy * 2, hz * 2, -hx + w / 2, 0, 0),
    slab(w, hy * 2, hz * 2, hx - w / 2, 0, 0),
    slab((hx - w) * 2, w, hz * 2, 0, -hy + w / 2, 0),
    slab((hx - w) * 2, w, hz * 2, 0, hy - w / 2, 0),
  ]);
}

/**
 * Every prop's body in the scene, driven by ground-truth poses.
 *
 * Each prop's root is built on first sight and then reused: a prop that is
 * taken away and put back down reappears rather than staying invisible.
 */
export class PropLibrary {
  /** Roster from the server, by name. Empty until the first manifest lands. */
  private info = new Map<string, PropInfo>();
  private roots = new Map<string, THREE.Group>();
  private placeholders = new Map<string, THREE.Mesh>();
  /** When each prop was first seen in a pose block, for MODEL_GRACE_MS. */
  private firstSeen = new Map<string, number>();
  private models: PropModels;
  private prefetching = false;
  private hulls: THREE.Mesh[] = [];
  private hullsVisible = false;
  private placementPreview?: PlacementPreview;
  /** Stream id -> manifest name. Deformable IDs are connection-stable. */
  private deformableNames = new Map<number, string>();
  /** Only the newest frame matters; rendering deliberately does not queue. */
  private latestDeformables = new Map<number, DeformableFrame>();
  private deformableMeshes = new Map<string, THREE.Mesh>();
  private deformableRestPositions = new Map<string, Float32Array>();
  private activeDeformables = new Set<string>();
  private skinLoads = new Map<string, Promise<DeformableSkin | null>>();
  private skins = new Map<string, DeformableSkin>();
  private failedSkins = new Set<string>();
  private waitingForSkin = new Set<string>();
  private warnings = new Set<string>();

  constructor(
    private scene: THREE.Scene,
    private material: THREE.MeshBasicMaterial,
    /** Called when a prop's body first enters the scene (shadow box refit). */
    private onChanged: () => void = () => {},
    /** Called with each model as it finishes parsing, to warm its textures. */
    onModelReady: (model: THREE.Group) => void = () => {},
  ) {
    this.models = new PropModels(onModelReady);
  }

  /** Adopt the server's roster. Props that vanish from it lose their bodies. */
  setManifest(props: PropInfo[]): void {
    this.info = new Map(props.map((p) => [p.name, p]));
    if (this.placementPreview && !this.info.has(this.placementPreview.name)) {
      this.clearPlacementPreview();
    }
    this.deformableNames.clear();
    for (const info of props) {
      const def = info.viewer.deformable;
      if (!def) continue;
      if (def.space !== "world") {
        this.warnOnce(`space:${info.name}`, `deformable prop '${info.name}' uses unsupported space '${def.space}'`);
        continue;
      }
      const existing = this.deformableNames.get(def.id);
      if (existing !== undefined) {
        this.warnOnce(`id:${def.id}`, `deformable id ${def.id} is shared by '${existing}' and '${info.name}'`);
        continue;
      }
      this.deformableNames.set(def.id, info.name);
    }
    for (const [name, root] of this.roots) {
      if (!this.info.has(name)) {
        this.scene.remove(root);
        const label = nameLabelOf(root);
        if (label) {
          label.material.map?.dispose();
          label.material.dispose();
        }
        this.roots.delete(name);
        this.placeholders.delete(name);
        this.firstSeen.delete(name); // re-added later, it gets a fresh grace window
        this.deformableMeshes.delete(name);
        this.deformableRestPositions.delete(name);
        this.activeDeformables.delete(name);
      }
    }
    for (const id of this.latestDeformables.keys()) {
      if (!this.deformableNames.has(id)) this.latestDeformables.delete(id);
    }
    if (this.prefetching) this.prefetchModels();
  }

  /**
   * Parse every prop's glb now, so a later drop can use it on the frame it
   * lands. Called once the robot and apartment are done, so prop models never
   * compete with them for the load queue's bandwidth.
   *
   * Safe to call before the roster arrives: it latches, and setManifest runs
   * it again once there is something to prefetch.
   */
  prefetchModels(): void {
    this.prefetching = true;
    this.models.prefetch(this.info.values());
    for (const info of this.info.values()) {
      if (info.viewer.deformable) void this.loadSkin(info);
    }
  }

  get manifest(): PropInfo[] {
    return [...this.info.values()];
  }

  /** Mirror ground truth: {name: [x, y, z, qw, qx, qy, qz]}. A prop the block
   * stops naming has left the world (parked, or never dropped) and is hidden
   * rather than left behind at its last pose. */
  setPoses(poses: Record<string, number[]>): void {
    for (const [name, root] of this.roots) {
      if (!poses[name]) {
        root.visible = false;
        this.restoreDeformable(name);
        const id = this.info.get(name)?.viewer.deformable?.id;
        if (id !== undefined) this.latestDeformables.delete(id);
      }
    }
    for (const [name, pose] of Object.entries(poses)) {
      const root = this.ensure(name);
      if (!root) continue; // unknown prop, or its glb is still loading
      const deformable = this.info.get(name)?.viewer.deformable;
      if (
        deformable &&
        this.deformableMeshes.has(name) &&
        !this.latestDeformables.has(deformable.id)
      ) {
        // A previously skinned GLB was restored to local rest shape when the
        // prop left. On re-drop, keep that base-origin mesh hidden for the one
        // stream interval before its fresh world-space frame arrives.
        root.visible = false;
        continue;
      }
      // Explicitly, not just on creation: a prop that was removed and put down
      // again reuses its hidden root and would otherwise stay invisible.
      root.visible = true;
      if (this.activeDeformables.has(name)) {
        // Keep the representative world translation on the root so scene
        // systems such as the shadow fitter can locate this prop. The skinned
        // world coordinates are translated back to this root's local frame.
        root.position.set(pose[0], pose[1], pose[2]);
        root.quaternion.identity();
        root.scale.set(1, 1, 1);
      } else {
        // Before the first deformable frame, the undeformed local glb is a
        // useful fallback and follows the soft body's representative pose.
        root.position.set(pose[0], pose[1], pose[2]);
        root.quaternion.set(pose[4], pose[5], pose[6], pose[3]);
      }
      // Once active, only a newly forwarded IDF1 frame should run the CPU
      // skinning pass. This startup call is solely for a frame that arrived
      // before its root/model became ready.
      if (this.info.get(name)?.viewer.deformable && !this.activeDeformables.has(name)) {
        this.requestDeformableApply(name);
      }
    }
  }

  /** Cache and, when its glb/skin are ready, render the newest IDF1 update. */
  setDeformableFrame(frame: DeformableFrame): void {
    const name = this.deformableNames.get(frame.id);
    if (!name) {
      this.warnOnce(`frame-id:${frame.id}`, `received deformable frame for unknown id ${frame.id}`);
      return;
    }
    const def = this.info.get(name)?.viewer.deformable;
    if (!def || frame.vertexCount !== def.controlVertexCount) {
      this.warnOnce(
        `frame-count:${frame.id}:${frame.vertexCount}`,
        `deformable '${name}' sent ${frame.vertexCount} controls; manifest expects ${def?.controlVertexCount ?? "none"}`,
      );
      return;
    }
    this.latestDeformables.set(frame.id, frame);
    this.requestDeformableApply(name);
  }

  setHullsVisible(visible: boolean): void {
    this.hullsVisible = visible;
    for (const hull of this.hulls) hull.visible = visible;
  }

  showPlacementPreview(name: string, x: number, y: number, yaw: number): void {
    if (name !== this.placementPreview?.name) {
      this.clearPlacementPreview();
      this.placementPreview = this.buildPlacementPreview(name);
    }
    if (!this.placementPreview) return;
    this.placementPreview.root.position.set(x, y, 0.015);
    this.placementPreview.root.rotation.z = yaw;
  }

  clearPlacementPreview(): void {
    if (!this.placementPreview) return;
    this.scene.remove(this.placementPreview.root);
    for (const material of this.placementPreview.materials) material.dispose();
    for (const geometry of this.placementPreview.geometries) geometry.dispose();
    this.placementPreview = undefined;
  }

  /** Every prop body currently in the world (shadow-box fitting). */
  get visibleRoots(): THREE.Object3D[] {
    return [...this.roots.values()].filter((r) => r.visible);
  }

  private buildPlacementPreview(name: string): PlacementPreview | undefined {
    const info = this.info.get(name);
    if (!info) return undefined;

    const root = new THREE.Group();
    const model = this.models.get(name);
    const visual = model ? cloneSkeleton(model) : this.primitiveMesh(info);
    const ownedMaterials: THREE.Material[] = [];
    const geometries: THREE.BufferGeometry[] = [];
    if (!model && visual instanceof THREE.Mesh) {
      geometries.push(visual.geometry);
    }
    visual.traverse((object) => {
      if (!(object instanceof THREE.Mesh)) return;
      const sourceMaterials = Array.isArray(object.material) ? object.material : [object.material];
      const ghostMaterials = sourceMaterials.map((material) => {
        const ghost = model ? material.clone() : material;
        ghost.transparent = true;
        ghost.opacity = Math.min(ghost.opacity, 0.48);
        ghost.depthWrite = false;
        ownedMaterials.push(ghost);
        return ghost;
      });
      object.material = Array.isArray(object.material) ? ghostMaterials : ghostMaterials[0];
      object.castShadow = false;
      object.receiveShadow = false;
    });
    root.add(visual);
    root.updateMatrixWorld(true);
    visual.position.z -= new THREE.Box3().setFromObject(root).min.z;
    this.scene.add(root);
    return { name, root, materials: ownedMaterials, geometries };
  }

  private ensure(name: string): THREE.Group | undefined {
    const existing = this.roots.get(name);
    if (existing) return existing;
    const info = this.info.get(name);
    if (!info) return undefined; // a prop this build was never told about

    const deformable = info.viewer.deformable;
    const skinReady = !deformable || this.skins.has(deformable.skin);
    const skinFailed = deformable ? this.failedSkins.has(deformable.skin) : false;
    const frameReady = !deformable || this.latestDeformables.has(deformable.id);

    // Hold a prop with a glb undrawn for up to MODEL_GRACE_MS rather than
    // showing a primitive we are about to replace. setPoses retries every
    // frame, so the prop appears as soon as either the model or the grace
    // window is done -- and at once if we already know no model is coming.
    const waiting =
      info.viewer.glb &&
      (!this.models.get(name) || !skinReady || !frameReady) &&
      !this.models.hasFailed(name) &&
      !skinFailed;
    if (waiting && !this.graceExpired(name, info)) return undefined;

    const root = new THREE.Group();
    this.roots.set(name, root);
    this.scene.add(root);
    if (info.viewer.hulls) void this.loadHullSoup(info, root);

    // A deformable GLB is meaningful only with its skin. Its local origin is
    // intentionally not the representative centroid pose, so rendering it as
    // a rigid fallback would float it. Use the centred physics primitive if
    // the skin is pending past grace or has failed.
    const model = skinReady && frameReady ? this.models.get(name) : undefined;
    if (model) {
      root.add(model);
      this.requestDeformableApply(name);
    } else {
      // No glb, or one still parsing past its grace window: draw the primitive
      // physics is using, and swap the model in if it does still arrive.
      const placeholder = this.primitiveMesh(info);
      this.placeholders.set(name, placeholder);
      root.add(placeholder);
      if (info.viewer.glb && !skinFailed) {
        if (deformable) {
          // A streamed frame is the final readiness gate. Whichever of the
          // model, skin, or frame arrives last asks requestDeformableApply to
          // promote this centred primitive atomically.
          void Promise.all([this.models.load(info), this.loadSkin(info)]).then(() =>
            this.requestDeformableApply(info.name),
          );
        } else {
          void this.swapWhenReady(info, root, placeholder);
        }
      }
    }
    if (info.viewer.nameLabel) {
      // Measured from the drawn content: `size` is the fallback collision box,
      // which for a mesh prop bears no relation to the model's height.
      root.add(makeNameLabel(info.title, info.viewer.nameLabelHeightM ?? labelAnchorZ(root)));
    }
    this.onChanged();
    return root;
  }

  /** Start this prop's model load on first sight, then report whether it has
   * since waited out MODEL_GRACE_MS. */
  private graceExpired(name: string, info: PropInfo): boolean {
    const since = this.firstSeen.get(name);
    if (since === undefined) {
      this.firstSeen.set(name, performance.now());
      void this.models.load(info); // dropped before its prefetch ran: parse it now
      if (info.viewer.deformable) void this.loadSkin(info);
      return false;
    }
    return performance.now() - since >= MODEL_GRACE_MS;
  }

  private primitiveMesh(info: PropInfo): THREE.Mesh {
    const [r, g, b] = info.rgba;
    const mesh = new THREE.Mesh(
      primitiveGeometry(info),
      new THREE.MeshStandardMaterial({ color: new THREE.Color(r, g, b), roughness: 0.7 }),
    );
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
  }

  /** Replace a prop's primitive once its model lands -- only reached when the
   * parse outran MODEL_GRACE_MS. */
  private async swapWhenReady(info: PropInfo, root: THREE.Group, placeholder: THREE.Mesh): Promise<void> {
    const model = await this.models.load(info);
    if (!model || placeholder.parent !== root) return; // failed, or the prop left the roster
    if (info.viewer.deformable && !(await this.loadSkin(info))) return;
    root.remove(placeholder);
    this.placeholders.delete(info.name);
    placeholder.geometry.dispose();
    root.add(model);
    const label = info.viewer.nameLabelHeightM === undefined ? nameLabelOf(root) : undefined;
    if (label) label.position.z = labelAnchorZ(root); // the placeholder's height was a stand-in
    this.requestDeformableApply(info.name);
    this.onChanged();
  }

  /** Apply only the most recent cached frame, never a backlog of old shapes. */
  private requestDeformableApply(name: string): void {
    const info = this.info.get(name);
    const def = info?.viewer.deformable;
    const root = this.roots.get(name);
    if (!info || !def || !root?.visible || !this.latestDeformables.has(def.id)) return;

    const skin = this.skins.get(def.skin);
    if (skin) {
      const model = this.models.get(name);
      const placeholder = this.placeholders.get(name);
      if (model && placeholder?.parent === root) {
        root.remove(placeholder);
        placeholder.geometry.dispose();
        root.add(model);
        this.placeholders.delete(name);
        this.onChanged();
      }
      this.applyLatestDeformable(info, root, skin);
      return;
    }
    if (this.failedSkins.has(def.skin) || this.waitingForSkin.has(name)) return;
    this.waitingForSkin.add(name);
    void this.loadSkin(info).then((loaded) => {
      this.waitingForSkin.delete(name);
      const currentRoot = this.roots.get(name);
      if (loaded && currentRoot?.visible) this.applyLatestDeformable(info, currentRoot, loaded);
    });
  }

  private applyLatestDeformable(info: PropInfo, root: THREE.Group, skin: DeformableSkin): void {
    const def = info.viewer.deformable!;
    const frame = this.latestDeformables.get(def.id);
    if (!frame || this.roots.get(info.name) !== root || !root.visible) return;
    if (
      skin.controlCount !== def.controlVertexCount ||
      skin.renderCount !== def.renderVertexCount ||
      frame.vertexCount !== skin.controlCount
    ) {
      this.warnOnce(
        `skin-count:${info.name}`,
        `deformable '${info.name}' count mismatch (manifest ${def.renderVertexCount}/${def.controlVertexCount}, skin ${skin.renderCount}/${skin.controlCount}, frame ${frame.vertexCount})`,
      );
      return;
    }

    const model = this.models.get(info.name);
    if (!model || model.parent !== root) return; // placeholder still owns the root
    const mesh = this.deformableMesh(info, model);
    if (!mesh) return;
    const position = mesh.geometry.getAttribute("position");
    if (!(position instanceof THREE.BufferAttribute) || position.itemSize !== 3 || position.count !== skin.renderCount) {
      this.warnOnce(`position:${info.name}`, `deformable '${info.name}' glb has no matching xyz position buffer`);
      return;
    }

    let output: Float32Array;
    if (position.array instanceof Float32Array && position.array.length === skin.renderCount * 3) {
      output = position.array;
    } else {
      output = new Float32Array(skin.renderCount * 3);
      mesh.geometry.setAttribute("position", new THREE.BufferAttribute(output, 3));
    }
    skinDeformablePositions(skin, frame.positions, output);
    // skinDeformablePositions deliberately reconstructs exact world-space
    // vertices. Geometry attributes are root-local, so remove only the root's
    // representative translation; rotation and scale stay identity.
    const rootX = root.position.x;
    const rootY = root.position.y;
    const rootZ = root.position.z;
    for (let i = 0; i < output.length; i += 3) {
      output[i] -= rootX;
      output[i + 1] -= rootY;
      output[i + 2] -= rootZ;
    }
    mesh.geometry.getAttribute("position").needsUpdate = true;
    mesh.geometry.computeVertexNormals();
    mesh.geometry.computeBoundingBox();
    mesh.geometry.computeBoundingSphere();

    root.quaternion.identity();
    root.scale.set(1, 1, 1);
    root.updateMatrixWorld(true);
    this.activeDeformables.add(info.name);
  }

  /** Find the generated render mesh and preserve its undeformed local shape. */
  private deformableMesh(info: PropInfo, model: THREE.Group): THREE.Mesh | undefined {
    const cached = this.deformableMeshes.get(info.name);
    if (cached?.parent) return cached;

    const expected = info.viewer.deformable!.renderVertexCount;
    const matches: THREE.Mesh[] = [];
    model.traverse((obj) => {
      if (!(obj instanceof THREE.Mesh)) return;
      const position = obj.geometry.getAttribute("position");
      if (position?.itemSize === 3 && position.count === expected) matches.push(obj);
    });
    if (matches.length !== 1) {
      this.warnOnce(
        `mesh:${info.name}`,
        `deformable '${info.name}' expected one ${expected}-vertex mesh in its glb; found ${matches.length}`,
      );
      return undefined;
    }

    const mesh = matches[0];
    const position = mesh.geometry.getAttribute("position");
    if (position instanceof THREE.BufferAttribute) position.setUsage(THREE.DynamicDrawUsage);
    this.deformableMeshes.set(info.name, mesh);
    this.deformableRestPositions.set(info.name, Float32Array.from(position.array));
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      material.side = THREE.DoubleSide;
      material.needsUpdate = true;
    }
    return mesh;
  }

  /** Put a hidden deformable back in its local rest shape for its next drop. */
  private restoreDeformable(name: string): void {
    if (!this.activeDeformables.delete(name)) return;
    const mesh = this.deformableMeshes.get(name);
    const rest = this.deformableRestPositions.get(name);
    const position = mesh?.geometry.getAttribute("position");
    if (!mesh || !rest || !(position instanceof THREE.BufferAttribute) || position.count * 3 !== rest.length) return;
    position.copyArray(rest);
    position.needsUpdate = true;
    mesh.geometry.computeVertexNormals();
    mesh.geometry.computeBoundingBox();
    mesh.geometry.computeBoundingSphere();
  }

  private loadSkin(info: PropInfo): Promise<DeformableSkin | null> {
    const def = info.viewer.deformable!;
    const started = this.skinLoads.get(def.skin);
    if (started) return started;
    const load = (async () => {
      try {
        const res = await fetch(def.skin);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const skin = decodeDeformableSkin(await res.arrayBuffer());
        if (skin.renderCount !== def.renderVertexCount || skin.controlCount !== def.controlVertexCount) {
          throw new Error(
            `manifest expects ${def.renderVertexCount}/${def.controlVertexCount}, skin contains ${skin.renderCount}/${skin.controlCount}`,
          );
        }
        this.skins.set(def.skin, skin);
        return skin;
      } catch (err) {
        this.failedSkins.add(def.skin);
        console.warn(`[sim-viewer] deformable skin missing for '${info.name}' (${def.skin}); drawing its primitive`, err);
        return null;
      }
    })();
    this.skinLoads.set(def.skin, load);
    return load;
  }

  private warnOnce(key: string, message: string): void {
    if (this.warnings.has(key)) return;
    this.warnings.add(key);
    console.warn(`[sim-viewer] ${message}`);
  }

  private async loadHullSoup(info: PropInfo, root: THREE.Group): Promise<void> {
    try {
      const res = await fetch(info.viewer.hulls!);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(await res.arrayBuffer()), 3));
      const hull = new THREE.Mesh(geometry, this.material);
      hull.visible = this.hullsVisible; // honour a toggle made before the drop
      this.hulls.push(hull);
      root.add(hull);
    } catch (err) {
      console.warn(`[sim-viewer] collision soup missing for '${info.name}'; overlay skipped`, err);
    }
  }
}
