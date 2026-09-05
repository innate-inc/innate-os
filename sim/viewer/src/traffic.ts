import * as THREE from "three";
import { GLTFLoader, type GLTF } from "three/addons/loaders/GLTFLoader.js";
import type { TrafficAspect, TrafficManifest, TrafficState } from "./trafficState";

const SIGNAL_COLORS = {
  red: new THREE.Color("#ff4b55"),
  yellow: new THREE.Color("#ffd45a"),
  green: new THREE.Color("#5ee27a"),
};
const OFF_COLOR = new THREE.Color("#171b1d");

interface CarRender {
  root: THREE.Group;
  bodyMaterial: THREE.MeshStandardMaterial;
  collision: THREE.Object3D;
  wheels: THREE.Object3D[];
}

function disposeModel(model: THREE.Object3D): void {
  const materials = new Set<THREE.MeshStandardMaterial>();
  model.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return;
    object.geometry.dispose();
    materials.add(object.material);
  });
  for (const material of materials) {
    material.map?.dispose();
    material.dispose();
  }
}

/** One generated car GLB, cloned and tinted per actor. Only server poses drive
 * movement, including the wheels; no browser traffic simulation lives here. */
export class TrafficLibrary {
  #roster: TrafficManifest = [];
  #state: TrafficState | null = null;
  #cars = new Map<string, CarRender>();
  #template?: THREE.Group;
  #loading?: Promise<GLTF>;
  #halfLength = 0;
  #halfWidth = 0;
  #hullsVisible = false;
  #signals: { material: THREE.MeshStandardMaterial; group: string; aspect: TrafficAspect }[] = [];

  constructor(
    private scene: THREE.Scene,
    private hullMaterial: THREE.Material,
    private onMoved: () => void,
  ) {}

  /** Full footprints, not centres, keep nearby cars inside the shadow volume. */
  get visibleBounds(): { minX: number; maxX: number; minY: number; maxY: number }[] {
    return [...this.#cars.values()].flatMap(({ root }) => {
      if (!root.visible) return [];
      const cosine = Math.abs(Math.cos(root.rotation.z));
      const sine = Math.abs(Math.sin(root.rotation.z));
      const dx = cosine * this.#halfLength + sine * this.#halfWidth;
      const dy = sine * this.#halfLength + cosine * this.#halfWidth;
      return [{ minX: root.position.x - dx, maxX: root.position.x + dx, minY: root.position.y - dy, maxY: root.position.y + dy }];
    });
  }

  registerEnvironment(root: THREE.Object3D): void {
    root.traverse((object) => {
      if (!(object instanceof THREE.Mesh) || !(object.material instanceof THREE.MeshStandardMaterial)) return;
      const match = /^Signal_(NS|EW)_(Red|Yellow|Green)$/.exec(object.material.name);
      if (match) this.#signals.push({
        material: object.material,
        group: match[1] === "NS" ? "north_south" : "east_west",
        aspect: match[2].toLowerCase() as TrafficAspect,
      });
    });
    this.#applyState();
  }

  setManifest(roster: TrafficManifest): void {
    if (JSON.stringify(roster) === JSON.stringify(this.#roster)) return;
    this.#removeCars();
    this.#roster = roster;
    if (this.#template) this.#buildCars();
    else if (roster.length && !this.#loading) {
      const load = this.#loading = new GLTFLoader().loadAsync("/models/intersection/car.glb");
      void load.then(({ scene: model }) => {
        // An environment switch or teardown may win the race with this fetch.
        if (this.#loading !== load) { disposeModel(model); return; }
        this.#template = model;
        model.rotation.x = Math.PI / 2; // standard glTF Y-up -> simulator Z-up
        const size = new THREE.Box3().setFromObject(model).getSize(new THREE.Vector3());
        this.#halfLength = size.x / 2;
        this.#halfWidth = size.y / 2;
        this.#buildCars();
        this.#loading = undefined;
      }).catch((error) => {
        if (this.#loading !== load) return;
        this.#loading = undefined;
        this.#removeCars();
        if (this.#template) disposeModel(this.#template);
        this.#template = undefined;
        console.warn("[sim-viewer] car model unavailable; traffic visuals hidden", error);
      });
    }
    this.#applyState();
  }

  setState(state: TrafficState | null): void {
    this.#state = state;
    this.#applyState();
  }

  setHullsVisible(visible: boolean): void {
    this.#hullsVisible = visible;
    for (const car of this.#cars.values()) car.collision.visible = visible;
  }

  unloadEnvironment(): void {
    this.#state = null;
    this.#applyState(); // restore fail-safe red before releasing environment materials
    this.#signals = [];
    this.#roster = [];
    this.#loading = undefined;
    this.#removeCars();
    if (this.#template) disposeModel(this.#template);
    this.#template = undefined;
  }

  #buildCars(): void {
    for (const car of this.#roster) {
      const model = this.#template!.clone(true);
      const root = new THREE.Group();
      root.name = `traffic_${car.id}`;
      root.add(model);
      const body = model.getObjectByName("body") as THREE.Mesh<THREE.BufferGeometry, THREE.MeshStandardMaterial>;
      const bodyMaterial = body.material.clone();
      bodyMaterial.color.set(car.color);
      body.material = bodyMaterial;
      const collision = model.getObjectByName("collision") as THREE.Mesh;
      collision.material = this.hullMaterial;
      collision.visible = this.#hullsVisible;
      const wheels: THREE.Object3D[] = [];
      model.traverse((object) => {
        if (object.userData.rolling_radius) wheels.push(object);
        if (object instanceof THREE.Mesh) {
          object.castShadow = object !== collision;
          object.receiveShadow = true;
        }
      });
      this.#cars.set(car.id, { root, bodyMaterial, collision, wheels });
      this.scene.add(root);
    }
    this.#applyState();
  }

  #applyState(): void {
    for (const [id, car] of this.#cars) {
      const state = this.#state?.cars[id];
      car.root.visible = state !== undefined;
      if (!state) continue;
      const [x, y, yaw] = state.pose;
      car.root.position.set(x, y, 0);
      car.root.rotation.z = yaw;
      const distance = x * Math.cos(yaw) + y * Math.sin(yaw);
      for (const wheel of car.wheels) wheel.rotation.y = distance / wheel.userData.rolling_radius;
    }
    for (const { material, group, aspect } of this.#signals) {
      const active = (this.#state?.signals[group] ?? "red") === aspect;
      material.color.copy(active ? SIGNAL_COLORS[aspect] : OFF_COLOR);
      material.emissive.copy(material.color);
      material.emissiveIntensity = active ? 0.65 : 0;
    }
    this.onMoved();
  }

  #removeCars(): void {
    for (const car of this.#cars.values()) {
      this.scene.remove(car.root);
      car.bodyMaterial.dispose();
    }
    this.#cars.clear();
  }
}
