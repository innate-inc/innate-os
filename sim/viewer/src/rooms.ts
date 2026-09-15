// RoomLibrary: draws a primitive-authored world (statics.py) from the roster
// frame's "rooms". The apartment is a mesh the viewer downloads; a benchmark
// world ships no mesh at all, and without this the 3D view is an empty box
// while the sim runs the map perfectly well. Lifecycle mirrors TrafficLibrary:
// built from a manifest, torn down on unloadEnvironment.

import * as THREE from "three";
import { isCeiling, isValidRoomGeom, roomBounds, type RoomGeom, type RoomInfo } from "./roomManifest";

/** THREE geometry for a MuJoCo primitive. Sizes are half-extents; MuJoCo
 * stands a cylinder or capsule on +z where THREE's is Y-up, hence rotateX. */
function roomGeometry(geom: RoomGeom): THREE.BufferGeometry {
  const s = geom.size;
  switch (geom.type) {
    case "sphere":
      return new THREE.SphereGeometry(s[0], 24, 16);
    case "cylinder":
      return new THREE.CylinderGeometry(s[0], s[0], s[1] * 2, 32).rotateX(Math.PI / 2);
    case "capsule":
      return new THREE.CapsuleGeometry(s[0], s[1] * 2, 4, 16).rotateX(Math.PI / 2);
    default:
      return new THREE.BoxGeometry(s[0] * 2, s[1] * 2, s[2] * 2);
  }
}

export class RoomLibrary {
  #group?: THREE.Group;
  #visuals: THREE.Mesh<THREE.BufferGeometry, THREE.MeshStandardMaterial>[] = [];
  #hulls: THREE.Mesh[] = [];
  #hullsVisible = false;
  #key = "";
  /** World extent of the drawn rooms (Z-up), for framing; undefined when empty. */
  bounds?: THREE.Box3;

  constructor(
    private scene: THREE.Scene,
    private hullMaterial: THREE.Material,
    private onChanged: () => void,
  ) {}

  setManifest(rooms: RoomInfo[]): void {
    const key = JSON.stringify(rooms);
    if (key === this.#key) return;
    this.#key = key;
    this.#clear();
    const group = new THREE.Group();
    group.name = "rooms";
    for (const room of rooms) {
      for (const geom of room.geoms) {
        if (!isValidRoomGeom(geom)) {
          console.warn(`[sim-viewer] skipping malformed geom in room '${room.name}':`, geom);
          continue;
        }
        if (isCeiling(geom)) continue; // the lid would hide the room from the top camera
        const [r, g, b, a] = geom.rgba;
        const mesh = new THREE.Mesh(
          roomGeometry(geom),
          new THREE.MeshStandardMaterial({ color: new THREE.Color(r, g, b), roughness: 0.85, transparent: a < 1, opacity: a }),
        );
        mesh.position.set(geom.pos[0], geom.pos[1], geom.pos[2]);
        const [qw, qx, qy, qz] = geom.quat; // MuJoCo (w, x, y, z) -> THREE (x, y, z, w)
        mesh.quaternion.set(qx, qy, qz, qw);
        // Decor is a surface (floor seams, skirting): a shadow from it is a
        // dark smear on the floor for no wall that is there.
        mesh.castShadow = geom.collide;
        mesh.receiveShadow = true;
        group.add(mesh);
        this.#visuals.push(mesh);
        if (geom.collide) {
          // Same wireframe overlay the props and hulls get from the
          // "collisions" toggle -- the geometry IS the collision shape here.
          const hull = new THREE.Mesh(mesh.geometry, this.hullMaterial);
          hull.position.copy(mesh.position);
          hull.quaternion.copy(mesh.quaternion);
          hull.visible = this.#hullsVisible;
          group.add(hull);
          this.#hulls.push(hull);
        }
      }
    }
    if (this.#visuals.length) {
      this.scene.add(group);
      this.#group = group;
      const extent = roomBounds(rooms);
      if (extent) this.bounds = new THREE.Box3(new THREE.Vector3(...extent.min), new THREE.Vector3(...extent.max));
    }
    this.onChanged();
  }

  setHullsVisible(visible: boolean): void {
    this.#hullsVisible = visible;
    for (const hull of this.#hulls) hull.visible = visible;
  }

  unloadEnvironment(): void {
    this.#key = "";
    this.#clear();
  }

  #clear(): void {
    if (this.#group) this.scene.remove(this.#group);
    // A hull shares its visual's geometry and the scene-wide hull material,
    // so the visuals own every disposal.
    for (const mesh of this.#visuals) {
      mesh.geometry.dispose();
      mesh.material.dispose();
    }
    this.#group = undefined;
    this.#visuals = [];
    this.#hulls = [];
    this.bounds = undefined;
  }
}
