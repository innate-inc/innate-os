import * as THREE from "three";
import type { TrafficAspect, TrafficManifest, TrafficPart, TrafficState } from "./trafficState";

const DEFAULT_SIGNAL_MATERIALS: TrafficManifest["signal_materials"] = {
  north_south: { red: "Signal_NS_Red", yellow: "Signal_NS_Yellow", green: "Signal_NS_Green" },
  east_west: { red: "Signal_EW_Red", yellow: "Signal_EW_Yellow", green: "Signal_EW_Green" },
};
const DEFAULT_SIGNAL_COLORS: Record<TrafficAspect, string> = {
  red: "#ff4b55",
  yellow: "#ffd45a",
  green: "#5ee27a",
};
const OFF_COLOR = new THREE.Color("#171b1d");

interface CarRender {
  root: THREE.Group;
  wheels: { mesh: THREE.Mesh; radius: number }[];
  colliders: THREE.Object3D[];
  halfLength: number;
  halfWidth: number;
}

type ColoredMaterial = THREE.Material & { color: THREE.Color };
type EmissiveMaterial = ColoredMaterial & { emissive: THREE.Color; emissiveIntensity: number };

function normalizeName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function isColored(material: THREE.Material): material is ColoredMaterial {
  return "color" in material && (material as { color?: unknown }).color instanceof THREE.Color;
}

function isEmissive(material: ColoredMaterial): material is EmissiveMaterial {
  return "emissive" in material && (material as { emissive?: unknown }).emissive instanceof THREE.Color;
}

/** Procedural renderer for simulator-owned traffic. No browser clock or path
 * logic lives here: it only materializes the opening manifest and snapshots. */
export class TrafficLibrary {
  #scene: THREE.Scene;
  #hullMaterial: THREE.Material;
  #onMoved: () => void;
  #manifest: TrafficManifest | null = null;
  #state: TrafficState | null = null;
  #cars = new Map<string, CarRender>();
  #environmentMaterials = new Set<THREE.Material>();
  #signalMaterials = new Map<string, Map<TrafficAspect, Set<ColoredMaterial>>>();
  #appliedSignals = new Map<string, TrafficAspect>();
  #ownedGeometries = new Set<THREE.BufferGeometry>();
  #ownedMaterials = new Set<THREE.Material>();
  #hullsVisible = false;
  #environmentReady = false;

  constructor(scene: THREE.Scene, hullMaterial: THREE.Material, onMoved: () => void) {
    this.#scene = scene;
    this.#hullMaterial = hullMaterial;
    this.#onMoved = onMoved;
  }

  /** Axis-aligned XY footprint of each visible car, including yaw. Shadow
   * fitting needs corners: a 3.6m car cannot be represented by its center. */
  get visibleBounds(): { minX: number; maxX: number; minY: number; maxY: number }[] {
    return [...this.#cars.values()].flatMap(({ root, halfLength, halfWidth }) => {
      if (!root.visible) return [];
      const cosine = Math.abs(Math.cos(root.rotation.z));
      const sine = Math.abs(Math.sin(root.rotation.z));
      const dx = cosine * halfLength + sine * halfWidth;
      const dy = sine * halfLength + cosine * halfWidth;
      return [{ minX: root.position.x - dx, maxX: root.position.x + dx, minY: root.position.y - dy, maxY: root.position.y + dy }];
    });
  }

  registerEnvironment(root: THREE.Object3D): void {
    root.traverse((object) => {
      if (!(object instanceof THREE.Mesh)) return;
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      materials.forEach((material) => this.#environmentMaterials.add(material));
    });
    const indexed = this.#collectSignalMaterials(this.#manifest?.signal_materials ?? DEFAULT_SIGNAL_MATERIALS);
    if (this.#environmentReady) this.#assertSignalMaterials(this.#manifest, indexed);
    this.#signalMaterials = indexed;
    this.#appliedSignals.clear();
    this.#applySignals(); // fail-safe all-red even if the roster is still in flight
  }

  /** Called once every required environment GLB has loaded. A traffic roster
   * without all six authored lamp materials is an unsafe/stale asset bundle. */
  markEnvironmentReady(): void {
    this.#assertSignalMaterials(this.#manifest, this.#signalMaterials);
    this.#environmentReady = true;
  }

  setManifest(manifest: TrafficManifest | null): void {
    const indexed = this.#collectSignalMaterials(manifest?.signal_materials ?? DEFAULT_SIGNAL_MATERIALS);
    // Validate against the candidate mapping before touching the active cars,
    // signal cache, or state. A stale asset bundle must leave the last complete
    // frame intact so the caller can report the error and retry safely.
    if (this.#environmentReady) this.#assertSignalMaterials(manifest, indexed);

    // Restore the previous mapping to its fail-safe state before adopting a
    // different one; otherwise a custom green material could stay lit after it
    // stops being part of the new roster.
    this.#state = null;
    this.#applySignals();
    this.#removeCars();
    this.#manifest = manifest;
    this.#signalMaterials = indexed;
    this.#appliedSignals.clear();
    if (manifest) {
      this.#buildCars(manifest);
    }
    this.#applyState();
  }

  setState(state: TrafficState | null): void {
    this.#state = state;
    this.#applyState();
  }

  setHullsVisible(visible: boolean): void {
    this.#hullsVisible = visible;
    for (const car of this.#cars.values()) car.colliders.forEach((collider) => (collider.visible = visible));
  }

  /** Forget every pack-owned material and dynamic object before the shared
   * SimScene loads another environment. The next roster and GLB load rebuild
   * both sides of the traffic contract. */
  unloadEnvironment(): void {
    this.#state = null;
    this.#applySignals();
    this.#removeCars();
    this.#manifest = null;
    this.#environmentMaterials.clear();
    this.#signalMaterials.clear();
    this.#appliedSignals.clear();
    this.#environmentReady = false;
  }

  clear(): void {
    this.#state = null;
    this.#applyState();
  }

  dispose(): void {
    this.unloadEnvironment();
  }

  #buildCars(manifest: TrafficManifest): void {
    const model = manifest.car_model;
    const geometries = model.parts.map((part) => this.#partGeometry(part));
    const colliderGeometries = model.colliders.map((collider) => {
      const geometry = new THREE.BoxGeometry(...collider.size);
      this.#ownedGeometries.add(geometry);
      return geometry;
    });
    const shared = Object.fromEntries(
      Object.entries(model.materials).map(([role, color]) => {
        const material = new THREE.MeshStandardMaterial({
          color,
          roughness: role === "glass" ? 0.25 : 0.72,
          metalness: role === "alloy" ? 0.45 : role === "glass" ? 0.1 : 0.0,
          flatShading: true,
        });
        if (role === "headlight") {
          material.emissive.set(color);
          material.emissiveIntensity = 0.45;
        }
        this.#ownedMaterials.add(material);
        return [role, material];
      }),
    ) as Record<Exclude<TrafficPart["material"], "body">, THREE.MeshStandardMaterial>;

    for (const car of manifest.cars) {
      const root = new THREE.Group();
      root.name = `traffic_${car.id}`;
      root.visible = false;
      const bodyMaterial = new THREE.MeshStandardMaterial({
        color: car.color,
        roughness: 0.62,
        metalness: 0.08,
        flatShading: true,
      });
      this.#ownedMaterials.add(bodyMaterial);
      const wheels: CarRender["wheels"] = [];
      model.parts.forEach((part, index) => {
        const material = part.material === "body" ? bodyMaterial : shared[part.material];
        const mesh = new THREE.Mesh(geometries[index], material);
        mesh.position.set(...part.position);
        if (part.rolling_radius) wheels.push({ mesh, radius: part.rolling_radius });
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        root.add(mesh);
      });
      const colliders = model.colliders.map((collider, index) => {
        const mesh = new THREE.Mesh(colliderGeometries[index], this.#hullMaterial);
        mesh.position.set(...collider.position);
        mesh.visible = this.#hullsVisible;
        root.add(mesh);
        return mesh;
      });
      this.#cars.set(car.id, {
        root,
        wheels,
        colliders,
        halfLength: model.length / 2,
        halfWidth: model.width / 2,
      });
      this.#scene.add(root);
    }
  }

  #partGeometry(part: TrafficPart): THREE.BufferGeometry {
    let geometry: THREE.BufferGeometry;
    if (part.shape === "box" && part.size) {
      geometry = new THREE.BoxGeometry(...part.size);
    } else if (part.shape === "prism" && part.profile && part.width !== undefined) {
      geometry = new THREE.ExtrudeGeometry(new THREE.Shape(part.profile.map(([x, z]) => new THREE.Vector2(x, z))), {
        depth: part.width, bevelEnabled: false, steps: 1,
      });
      geometry.rotateX(Math.PI / 2); // profile XY -> XZ, extrusion Z -> -Y
      geometry.translate(0, part.width / 2, 0);
    } else if (part.shape === "cylinder" && part.radius !== undefined && part.length !== undefined) {
      geometry = new THREE.CylinderGeometry(part.radius, part.radius, part.length, 12, 1, false);
      // Three cylinders are +Y; the shared descriptor starts at +Z like MJCF.
      geometry.rotateX(Math.PI / 2);
    } else {
      throw new Error("invalid traffic primitive in a validated manifest");
    }
    if (part.rotation) geometry.applyQuaternion(new THREE.Quaternion().setFromEuler(new THREE.Euler(...part.rotation)));
    this.#ownedGeometries.add(geometry);
    return geometry;
  }

  #applyState(): void {
    for (const [id, car] of this.#cars) {
      const state = this.#state?.cars[id];
      car.root.visible = state !== undefined;
      if (!state) continue;
      car.root.position.set(state.pose[0], state.pose[1], 0.0);
      car.root.rotation.set(0, 0, state.pose[2]);
      // The server's lanes are straight. Use the interpolated pose, so wheels
      // stop, resume and respawn on exactly the same timeline as the car.
      const distance = state.pose[0] * Math.cos(state.pose[2]) + state.pose[1] * Math.sin(state.pose[2]);
      for (const { mesh, radius } of car.wheels) mesh.rotation.y = distance / radius;
    }
    this.#applySignals();
    this.#onMoved();
  }

  #collectSignalMaterials(
    mapping: TrafficManifest["signal_materials"],
  ): Map<string, Map<TrafficAspect, Set<ColoredMaterial>>> {
    const indexed = new Map<string, Map<TrafficAspect, Set<ColoredMaterial>>>();
    const lookup = new Map<string, [string, TrafficAspect]>();
    for (const [group, aspects] of Object.entries(mapping)) {
      for (const aspect of ["red", "yellow", "green"] as const) {
        lookup.set(normalizeName(aspects[aspect]), [group, aspect]);
      }
    }
    for (const material of this.#environmentMaterials) {
      const match = lookup.get(normalizeName(material.name));
      if (!match || !isColored(material)) continue;
      const [group, aspect] = match;
      const aspects = indexed.get(group) ?? new Map<TrafficAspect, Set<ColoredMaterial>>();
      const materials = aspects.get(aspect) ?? new Set<ColoredMaterial>();
      materials.add(material);
      aspects.set(aspect, materials);
      indexed.set(group, aspects);
    }
    return indexed;
  }

  #applySignals(): void {
    const colors = this.#manifest?.signal_colors ?? DEFAULT_SIGNAL_COLORS;
    for (const [group, aspects] of this.#signalMaterials) {
      const requested = this.#state?.signals[group];
      const selected: TrafficAspect = requested === "green" || requested === "yellow" ? requested : "red";
      if (this.#appliedSignals.get(group) === selected) continue;
      for (const [aspect, materials] of aspects) {
        const active = aspect === selected;
        const color = active ? new THREE.Color(colors[aspect]) : OFF_COLOR;
        for (const material of materials) {
          material.color.copy(color);
          if (isEmissive(material)) {
            material.emissive.copy(color);
            material.emissiveIntensity = active ? 0.65 : 0.0;
          }
        }
      }
      this.#appliedSignals.set(group, selected);
    }
  }

  #assertSignalMaterials(
    manifest: TrafficManifest | null,
    indexed: Map<string, Map<TrafficAspect, Set<ColoredMaterial>>>,
  ): void {
    if (!manifest) return;
    const missing: string[] = [];
    for (const [group, aspects] of Object.entries(manifest.signal_materials)) {
      const materials = indexed.get(group);
      for (const aspect of ["red", "yellow", "green"] as const) {
        if (!materials?.get(aspect)?.size) missing.push(aspects[aspect]);
      }
    }
    if (missing.length > 0) {
      throw new Error(`environment is missing traffic signal materials: ${missing.join(", ")}`);
    }
  }

  #removeCars(): void {
    for (const car of this.#cars.values()) this.#scene.remove(car.root);
    this.#cars.clear();
    for (const geometry of this.#ownedGeometries) geometry.dispose();
    for (const material of this.#ownedMaterials) material.dispose();
    this.#ownedGeometries.clear();
    this.#ownedMaterials.clear();
  }
}
