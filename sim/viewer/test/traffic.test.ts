import assert from "node:assert/strict";
import test from "node:test";
import * as THREE from "three";
import { createServer } from "vite";
import type { SimScene } from "../src/scene.ts";
import { TrafficLibrary } from "../src/traffic.ts";
import { interpolateTraffic, parseTrafficManifest, type TrafficManifest, type TrafficState } from "../src/trafficState.ts";

const manifest: TrafficManifest = {
  schema_version: 1,
  car_model: {
    forward_axis: "+x",
    up_axis: "+z",
    length: 3.6,
    width: 1.55,
    height: 1.35,
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

test("traffic interpolation follows the playback clock but never blends a respawn or signal boundary", () => {
  const a = state(-10, 3, "red");
  const b = state(-8, 3, "green");
  assert.equal(interpolateTraffic(a, b, 0.5)?.cars.eastbound.pose[0], -9);
  assert.equal(interpolateTraffic(a, b, 0.5)?.signals.north_south, "red");
  assert.equal(interpolateTraffic(a, b, 1)?.signals.north_south, "green");

  const respawned = state(10, 4, "green");
  assert.equal(interpolateTraffic(b, respawned, 0.9)?.cars.eastbound.pose[0], -8);
  assert.equal(interpolateTraffic(b, respawned, 1)?.cars.eastbound.pose[0], 10);
  assert.equal(interpolateTraffic(b, { ...respawned, world_epoch: 3 }, 0.9)?.world_epoch, 2);
  assert.equal(interpolateTraffic(b, { ...respawned, world_epoch: 3 }, 1)?.world_epoch, 3);
});

test("manifest validation rejects malformed primitives", () => {
  assert.deepEqual(parseTrafficManifest(manifest), manifest);
  assert.equal(
    parseTrafficManifest({ ...manifest, car_model: { ...manifest.car_model, parts: [{ shape: "box", size: [0, 1, 1] }] } }),
    null,
  );
  assert.equal(
    parseTrafficManifest({
      ...manifest,
      car_model: { ...manifest.car_model, colliders: [{ shape: "box", position: [0, 0, 0], size: [1, 0, 1] }] },
    }),
    null,
  );
  assert.equal(
    parseTrafficManifest({
      ...manifest,
      car_model: { ...manifest.car_model, parts: [{ shape: "box", size: [1, 1, 1], position: [0, 0, 0], material: "road" }] },
    }),
    null,
  );
  assert.equal(
    parseTrafficManifest({
      ...manifest,
      car_model: {
        ...manifest.car_model,
        parts: [{ shape: "box", size: [1, 1, 1], position: [0, 0, 0], rotation: "bad", material: "body" }],
      },
    }),
    null,
  );
});

test("procedural cars and authored signal materials apply state and clear independently", () => {
  const scene = new THREE.Scene();
  const hull = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });
  const library = new TrafficLibrary(scene, hull, () => undefined);
  const root = new THREE.Group();
  const signalNames = Object.values(manifest.signal_materials).flatMap((aspects) =>
    ["red", "yellow", "green"].map((aspect) => aspects[aspect as keyof typeof aspects]),
  );
  const signalMaterials = new Map(signalNames.map((name) => {
    const material = new THREE.MeshStandardMaterial({ color: 0xffffff });
    material.name = name;
    root.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), material));
    return [name, material] as const;
  }));
  library.registerEnvironment(root);
  library.setManifest(manifest);
  library.markEnvironmentReady();
  assert.equal(signalMaterials.get("Signal_NS_Red")?.color.getHexString(), "ff4b55");
  assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "171b1d");

  library.setState(state(-9, 0, "green"));
  assert.equal(library.visibleRoots.length, 1);
  assert.equal(library.visibleRoots[0].position.x, -9);
  assert.deepEqual(library.visibleBounds[0], { minX: -10.8, maxX: -7.2, minY: -2.285, maxY: -0.735 });
  assert.equal(signalMaterials.get("Signal_NS_Red")?.color.getHexString(), "171b1d");
  assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "5ee27a");

  // An explicit null roster is a connection/environment generation boundary:
  // it must clear cars and restore all-red without waiting for another state.
  library.setManifest(null);
  assert.equal(library.visibleRoots.length, 0);
  assert.equal(signalMaterials.get("Signal_NS_Red")?.color.getHexString(), "ff4b55");
  assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "171b1d");
  library.dispose();
  hull.dispose();
});

test("a rejected ready-scene roster preserves active traffic and can be retried", () => {
  const scene = new THREE.Scene();
  const hull = new THREE.MeshBasicMaterial({ color: 0x00ff88, wireframe: true });
  const library = new TrafficLibrary(scene, hull, () => undefined);
  const root = new THREE.Group();
  const signalNames = Object.values(manifest.signal_materials).flatMap((aspects) =>
    ["red", "yellow", "green"].map((aspect) => aspects[aspect as keyof typeof aspects]),
  );
  const signalMaterials = new Map(signalNames.map((name) => {
    const material = new THREE.MeshStandardMaterial({ color: 0xffffff });
    material.name = name;
    root.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), material));
    return [name, material] as const;
  }));
  library.registerEnvironment(root);
  library.setManifest(manifest);
  library.markEnvironmentReady();
  library.setState(state(-9, 0, "green"));
  const activeCar = library.visibleRoots[0];

  const replacement: TrafficManifest = {
    ...manifest,
    signal_materials: {
      ...manifest.signal_materials,
      east_west: { ...manifest.signal_materials.east_west, green: "Signal_EW_Green_V2" },
    },
  };
  assert.throws(() => library.setManifest(replacement), /Signal_EW_Green_V2/);
  assert.equal(library.visibleRoots[0], activeCar);
  assert.equal(library.visibleRoots[0].position.x, -9);
  assert.equal(signalMaterials.get("Signal_NS_Green")?.color.getHexString(), "5ee27a");

  const added = new THREE.Group();
  const addedMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff });
  addedMaterial.name = "Signal_EW_Green_V2";
  added.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), addedMaterial));
  library.registerEnvironment(added);
  assert.doesNotThrow(() => library.setManifest(replacement));
  assert.equal(library.visibleRoots.length, 0); // new generation waits for its first state
  library.setState(state(-7, 0, "green"));
  assert.equal(library.visibleRoots[0].position.x, -7);

  library.dispose();
  hull.dispose();
});

test("a traffic roster fails closed when the environment lacks an authored lamp material", () => {
  const library = new TrafficLibrary(new THREE.Scene(), new THREE.MeshBasicMaterial(), () => undefined);
  const root = new THREE.Group();
  for (const name of ["Signal_NS_Red", "Signal_NS_Yellow", "Signal_NS_Green"]) {
    const material = new THREE.MeshStandardMaterial();
    material.name = name;
    root.add(new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), material));
  }
  library.registerEnvironment(root);
  library.setManifest(manifest);
  assert.throws(() => library.markEnvironmentReady(), /Signal_EW_Red/);
  library.dispose();
});

class SessionWebSocket {
  static readonly OPEN = 1;
  static instances: SessionWebSocket[] = [];

  readonly url: string;
  readyState = SessionWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    SessionWebSocket.instances.push(this);
  }

  send(_value: string): void {}
  close(): void {}
  open(): void {
    this.onopen?.();
  }
  message(value: unknown): void {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}

test("SimSession reports a rejected roster once, freezes traffic, and retries on a later roster", async () => {
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
    value: {
      createElement: () => ({ width: 0, height: 0, getContext: () => null }),
    },
  });
  globalThis.WebSocket = SessionWebSocket as unknown as typeof WebSocket;
  console.error = (...args: unknown[]) => errors.push(args);
  SessionWebSocket.instances = [];

  const appliedTraffic: (TrafficState | null)[] = [];
  const robotPoses: number[] = [];
  let manifestAttempts = 0;
  let rejectReplacement = true;
  const scene = {
    environmentFingerprint: "town-v1",
    clearWorldState: () => undefined,
    setPropManifest: () => undefined,
    setTrafficManifest: (candidate: TrafficManifest | null) => {
      manifestAttempts += 1;
      if (candidate?.signal_materials.east_west.green === "Signal_EW_Green_V2" && rejectReplacement) {
        throw new Error("environment is missing traffic signal materials: Signal_EW_Green_V2");
      }
    },
    setLidarVisible: () => undefined,
    setCollisionHullsVisible: () => undefined,
    spawnAt: () => undefined,
    setPose: (x: number) => robotPoses.push(x),
    setJointAngles: () => undefined,
    setObjectPoses: () => undefined,
    setTrafficState: (next: TrafficState | null) => appliedTraffic.push(next),
  } as unknown as SimScene;
  const replacement: TrafficManifest = {
    ...manifest,
    signal_materials: {
      ...manifest.signal_materials,
      east_west: { ...manifest.signal_materials.east_west, green: "Signal_EW_Green_V2" },
    },
  };
  // Load through the same Vite resolver used by the viewer bundle: SimSession's
  // public entry point deliberately retains extensionless browser imports that
  // Node's strip-types test loader cannot resolve on its own.
  const vite = await createServer({
    root: new URL("../", import.meta.url).pathname,
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  const { SimSession } = (await vite.ssrLoadModule("/src/simSession.ts")) as typeof import("../src/simSession.ts");
  const session = new SimSession({ stateUrl: "ws://example.test/worldstate" });

  try {
    session.start();
    const socket = SessionWebSocket.instances[0];
    socket.open();
    socket.message({
      environment: { id: "low-poly-town", fingerprint: "town-v1" },
      props: [],
      challenges: [],
      traffic_manifest: manifest,
    });
    socket.message({
      t: 1,
      wall: Date.now() / 1000,
      world_epoch: 2,
      pose: [0, 0, 0],
      joints: {},
      objects: {},
      traffic: state(-9),
      challenge: null,
    });
    session.tick(scene, 0.016);
    assert.equal(manifestAttempts, 1);
    assert.equal(appliedTraffic.length, 1);
    assert.equal(appliedTraffic[0]?.cars.eastbound.pose[0], -9);

    let candidateAttempts = 0;
    const rejectedCandidate = {
      ...(scene as unknown as Record<string, unknown>),
      setTrafficManifest: () => {
        candidateAttempts += 1;
        throw new Error("candidate is missing an authored traffic material");
      },
    } as unknown as SimScene;
    assert.throws(
      () => session.tick(rejectedCandidate, 0, { strictTrafficManifest: true }),
      /candidate is missing an authored traffic material/,
      "a hidden replacement must not be activated after a rejected roster",
    );
    assert.equal(candidateAttempts, 1);
    assert.equal(errors.length, 0, "the hot-swap coordinator owns reporting candidate build failures");
    session.tick(scene, 0);
    assert.equal(manifestAttempts, 1, "a rejected candidate must restore the active scene's delivery state");
    const completeTrafficFrames = appliedTraffic.length;

    socket.message({
      environment: { id: "low-poly-town", fingerprint: "town-v1" },
      traffic_manifest: replacement,
    });
    socket.message({
      t: 2,
      wall: Date.now() / 1000,
      world_epoch: 2,
      pose: [1, 0, 0],
      joints: {},
      objects: {},
      traffic: state(-7, 0, "green"),
      challenge: null,
    });

    assert.doesNotThrow(() => session.tick(scene, 0.016));
    assert.equal(manifestAttempts, 2);
    assert.equal(errors.length, 1);
    assert.match(String(errors[0][0]), /traffic roster rejected/);
    assert.equal(
      appliedTraffic.length,
      completeTrafficFrames,
      "new state must not mutate the previously accepted traffic frame",
    );
    assert.ok(robotPoses.at(-1)! > 0, "the rest of the live scene should continue updating");

    assert.doesNotThrow(() => session.tick(scene, 0.016));
    assert.equal(manifestAttempts, 2, "the rejected roster must not be retried every animation frame");
    assert.equal(errors.length, 1, "the rejected roster must only be reported once per attempt");
    assert.equal(appliedTraffic.length, completeTrafficFrames);

    rejectReplacement = false;
    socket.message({
      environment: { id: "low-poly-town", fingerprint: "town-v1" },
      traffic_manifest: replacement,
    });
    assert.doesNotThrow(() => session.tick(scene, 0));
    assert.equal(manifestAttempts, 3);
    assert.equal(errors.length, 1);
    assert.equal(appliedTraffic.length, completeTrafficFrames + 1);
    assert.ok(appliedTraffic.at(-1)!.cars.eastbound.pose[0] > -9);
  } finally {
    session.stop();
    await vite.close();
    console.error = previousConsoleError;
    globalThis.WebSocket = previousWebSocket;
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument);
    else Reflect.deleteProperty(globalThis, "document");
    if (previousLocation) Object.defineProperty(globalThis, "location", previousLocation);
    else Reflect.deleteProperty(globalThis, "location");
  }
});
