import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";
import type { SimScene } from "../src/scene.ts";
import { TrafficLibrary } from "../src/traffic.ts";
import { interpolateTraffic, type TrafficManifest, type TrafficState } from "../src/trafficState.ts";

const manifest: TrafficManifest = {
  schema_version: 1,
  car_model: {
    length: 3.6,
    width: 1.55,
    parts: [
      { shape: "box", size: [3.6, 1.55, 0.56], position: [0, 0, 0.45], material: "body" },
      {
        shape: "cylinder",
        radius: 0.3,
        length: 0.18,
        position: [1.1, 0.77, 0.3],
        rotation: [Math.PI / 2, 0, 0],
        material: "rubber",
      },
    ],
    colliders: [{ shape: "box", size: [3.6, 1.56, 0.56], position: [0, 0, 0.45] }],
    materials: { glass: "#263641", rubber: "#202328", headlight: "#fff0a8", taillight: "#d7353f" },
  },
  cars: [{ id: "eastbound", color: "#e05b47" }],
  signal_materials: {
    north_south: { red: "Signal_NS_Red", yellow: "Signal_NS_Yellow", green: "Signal_NS_Green" },
    east_west: { red: "Signal_EW_Red", yellow: "Signal_EW_Yellow", green: "Signal_EW_Green" },
  },
  signal_colors: { red: "#ff4b55", yellow: "#ffd45a", green: "#5ee27a" },
};

function state(x: number, spawnSeq = 0, ns: "red" | "yellow" | "green" = "red"): TrafficState {
  return {
    world_epoch: 2,
    phase: `${ns}_phase`,
    signals: { north_south: ns, east_west: "red" },
    cars: { eastbound: { pose: [x, -1.51, 0], speed: 2, spawn_seq: spawnSeq } },
  };
}

function addSignalMaterial(root: THREE.Group, name: string): THREE.MeshStandardMaterial {
  const material = new THREE.MeshStandardMaterial({ color: 0xffffff });
  material.name = name;
  root.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), material));
  return material;
}

function addSignalMaterials(root: THREE.Group): Map<string, THREE.MeshStandardMaterial> {
  const names = Object.values(manifest.signal_materials).flatMap((aspects) =>
    ["red", "yellow", "green"].map((aspect) => aspects[aspect as keyof typeof aspects]),
  );
  return new Map(names.map((name) => [name, addSignalMaterial(root, name)]));
}

class SessionWebSocket {
  static readonly OPEN = 1;
  static instances: SessionWebSocket[] = [];
  readyState = SessionWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  closeCalls: { code?: number; reason?: string }[] = [];

  constructor(_url: string | URL) {
    SessionWebSocket.instances.push(this);
  }
  send(_value: string): void {}
  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
  }
  open(): void {
    this.onopen?.();
  }
  message(value: unknown): void {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}

function worldFrame(t: number, x: number, traffic: TrafficState | null) {
  return {
    t,
    wall: Date.now() / 1000,
    world_epoch: 2,
    pose: [x, 0, 0],
    joints: {},
    objects: {},
    traffic,
    challenge: null,
  };
}

test("traffic runs from validated wire snapshots through safe scene swaps and Three rendering", async () => {
  const before = state(-10, 3, "red");
  const after = state(-8, 3, "green");
  assert.equal(interpolateTraffic(before, after, 0.5)?.cars.eastbound.pose[0], -9);
  assert.equal(interpolateTraffic(before, after, 0.5)?.signals.north_south, "red");
  const respawned = state(10, 4, "green");
  assert.equal(interpolateTraffic(after, respawned, 0.9)?.cars.eastbound.pose[0], -8);
  assert.equal(interpolateTraffic(after, { ...respawned, world_epoch: 3 }, 1)?.world_epoch, 3);

  const previousLocation = Object.getOwnPropertyDescriptor(globalThis, "location");
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const previousWebSocket = globalThis.WebSocket;
  const previousConsoleError = console.error;
  const errors: unknown[][] = [];
  Object.defineProperty(globalThis, "location", {
    configurable: true,
    value: { protocol: "http:", host: "example.test", hostname: "example.test" },
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: { createElement: () => ({ width: 0, height: 0, getContext: () => null }) },
  });
  globalThis.WebSocket = SessionWebSocket as unknown as typeof WebSocket;
  console.error = (...args: unknown[]) => errors.push(args);
  SessionWebSocket.instances = [];

  const threeScene = new THREE.Scene();
  const hull = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });
  const library = new TrafficLibrary(threeScene, hull, () => undefined);
  const renderedCar = () => threeScene.getObjectByName("traffic_eastbound") as THREE.Group | undefined;
  const environment = new THREE.Group();
  const signalMaterials = addSignalMaterials(environment);
  library.registerEnvironment(environment);
  library.markEnvironmentReady();
  assert.equal(signalMaterials.get("Signal_NS_Red")?.color.getHexString(), "ff4b55");
  assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "171b1d");
  let robotX = 0;
  let manifestAttempts = 0;
  let renderedTraffic: TrafficState | null = null;
  const scene = {
    setPropManifest: () => undefined,
    setTrafficManifest: (candidate: TrafficManifest | null) => {
      manifestAttempts += 1;
      library.setManifest(candidate);
    },
    setTrafficState: (next: TrafficState | null) => {
      renderedTraffic = next;
      library.setState(next);
    },
    setLidarVisible: () => undefined,
    setCollisionHullsVisible: () => undefined,
    spawnAt: () => undefined,
    setPose: (x: number) => (robotX = x),
    setJointAngles: () => undefined,
    setObjectPoses: () => undefined,
  } as unknown as SimScene;
  const replacement: TrafficManifest = {
    ...manifest,
    signal_materials: {
      ...manifest.signal_materials,
      east_west: { ...manifest.signal_materials.east_west, green: "Signal_EW_Green_V2" },
    },
  };
  const { SimSession } = await import("../dist-lib/sim-session.js");
  const session = new SimSession({ stateUrl: "ws://example.test/worldstate" });

  try {
    session.start();
    const socket = SessionWebSocket.instances[0];
    socket.open();
    socket.message({
      environment: {
        id: "intersection",
        display_name: "Crossroads",
        viewer: { type: "glb", model: "models/intersection/intersection.glb" },
        spawn: [3, -9.5, 90],
      },
      environments: [],
      switch: null,
      props: [],
      challenges: [],
      traffic_manifest: manifest,
    });
    socket.message(worldFrame(1, 0, state(-9)));
    session.tick(scene, 0);
    assert.equal(manifestAttempts, 1);
    assert.equal(renderedCar()?.position.x, -9);
    assert.deepEqual(library.visibleBounds[0], { minX: -10.8, maxX: -7.2, minY: -2.285, maxY: -0.735 });
    const activeCar = renderedCar();

    socket.message({ traffic_manifest: replacement });
    socket.message(worldFrame(2, 1, state(-7, 0, "green")));
    session.tick(scene, 0.016);
    assert.equal(manifestAttempts, 2);
    assert.equal(errors.length, 1);
    assert.match(String(errors[0][0]), /traffic roster rejected/);
    assert.equal(renderedCar(), activeCar);
    assert.equal(renderedCar()?.position.x, -9);
    assert.ok(robotX > 0, "the rest of the scene must continue updating");

    session.tick(scene, 0.016);
    assert.equal(manifestAttempts, 2, "a rejected roster must not be retried every animation frame");
    assert.equal(errors.length, 1, "a rejected roster is reported once per authoritative delivery");
    assert.equal(renderedCar()?.position.x, -9);

    const added = new THREE.Group();
    addSignalMaterial(added, "Signal_EW_Green_V2");
    library.registerEnvironment(added);
    socket.message({ traffic_manifest: replacement });
    session.tick(scene, 0);
    assert.equal(manifestAttempts, 3);
    assert.equal(errors.length, 1);
    assert.ok((renderedCar()?.position.x ?? -Infinity) > -9);
    socket.message(worldFrame(2.1, 1.1, state(-6.8, 0, "green")));
    session.tick(scene, 0.1);
    assert.equal(renderedTraffic?.signals.north_south, "green");
    assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "5ee27a");

    library.unloadEnvironment();
    socket.message({
      environment: {
        id: "apartment",
        display_name: "Apartment",
        viewer: { type: "glb", model: "models/appartement.glb" },
        spawn: [-4.34, -0.17, -89.8],
      },
      environments: [],
      switch: null,
      props: [],
      challenges: [],
      traffic_manifest: null,
    });
    socket.message(worldFrame(1, 0, null));
    session.tick(scene, 0);
    assert.equal(manifestAttempts, 4, "an explicit no-traffic roster must still be applied");
    assert.equal(renderedCar(), undefined);
    assert.equal(signalMaterials.get("Signal_NS_Red")?.color.getHexString(), "ff4b55");
    assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "171b1d");

    // A shared SimScene must not retain the previous pack's materials. A town
    // roster can arrive before its GLB, but readiness stays blocked until the
    // replacement environment contributes all six signal materials.
    library.unloadEnvironment();
    library.setManifest(manifest);
    assert.throws(() => library.markEnvironmentReady(), /missing traffic signal materials/);
    const reloadedTown = new THREE.Group();
    addSignalMaterials(reloadedTown);
    library.registerEnvironment(reloadedTown);
    assert.doesNotThrow(() => library.markEnvironmentReady());

    socket.message({
      traffic_manifest: {},
    });
    assert.deepEqual(socket.closeCalls.at(-1), { code: 1002, reason: "invalid traffic manifest" });
  } finally {
    session.stop();
    library.dispose();
    hull.dispose();
    console.error = previousConsoleError;
    globalThis.WebSocket = previousWebSocket;
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument);
    else Reflect.deleteProperty(globalThis, "document");
    if (previousLocation) Object.defineProperty(globalThis, "location", previousLocation);
    else Reflect.deleteProperty(globalThis, "location");
  }
});
