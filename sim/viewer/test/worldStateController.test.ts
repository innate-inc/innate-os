import assert from "node:assert/strict";
import test from "node:test";
import { WorldStateController } from "../src/physics/worldStateController.ts";

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  closeCalls: { code?: number; reason?: string }[] = [];

  constructor(url: string | URL) {
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  send(_value: string): void {}

  close(code?: number, reason?: string): void {
    this.closeCalls.push({ code, reason });
  }

  open(): void {
    this.onopen?.();
  }

  message(value: unknown): void {
    this.onmessage?.({ data: typeof value === "string" ? value : JSON.stringify(value) });
  }
}

test("world-state wire parsing is fail-closed and orders a valid opening roster before readiness", () => {
  const previous = globalThis.WebSocket;
  FakeWebSocket.instances = [];
  globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
  try {
    const controller = new WorldStateController("ws://example.test/worldstate");
    const socket = FakeWebSocket.instances[0];
    const events: string[] = [];
    const states: unknown[] = [];
    controller.onConnectionChange = (connected) => events.push(`connected:${connected}`);
    controller.onEnvironment = () => events.push("environment");
    controller.onProps = () => events.push("props");
    controller.onChallenges = () => events.push("challenges");
    controller.onTrafficManifest = (manifest) => events.push(`traffic:${manifest === null ? "null" : "manifest"}`);
    controller.onState = (state) => states.push(state);

    socket.open();
    assert.doesNotThrow(() => socket.message("null"));
    assert.doesNotThrow(() => socket.message("[]"));
    socket.message({
      environment: { id: "apartment", fingerprint: "apartment-v1" },
      props: [],
      challenges: [],
      traffic_manifest: null,
    });
    assert.deepEqual(events, [
      "connected:false",
      "environment",
      "props",
      "challenges",
      "traffic:null",
      "connected:true",
    ]);

    socket.message({
      t: 1,
      wall: 2,
      world_epoch: 3,
      pose: [4, 5, 0.25],
      joints: { joint6: 0.2 },
      objects: {},
      traffic: null,
      challenge: null,
    });
    assert.equal(states.length, 1);
    assert.equal((states[0] as { worldEpoch: number }).worldEpoch, 3);
    assert.equal((states[0] as { joints: Record<string, number> }).joints.joint6M, -0.2);
    socket.message({ t: 2, wall: 3, pose: [0, 1], joints: {} });
    assert.equal(states.length, 1);
    controller.dispose();

    const invalid = new WorldStateController("ws://example.test/invalid");
    const invalidSocket = FakeWebSocket.instances[1];
    const readiness: boolean[] = [];
    const adopted: string[] = [];
    invalid.onConnectionChange = (connected) => readiness.push(connected);
    invalid.onEnvironment = () => adopted.push("environment");
    invalid.onProps = () => adopted.push("props");
    invalid.onChallenges = () => adopted.push("challenges");
    invalid.onTrafficManifest = () => adopted.push("traffic");
    invalidSocket.open();
    invalidSocket.message({
      environment: { id: "low-poly-town", fingerprint: "town-v1" },
      props: [],
      challenges: [],
      traffic_manifest: {},
    });
    assert.deepEqual(readiness, [false]);
    assert.deepEqual(adopted, []);
    assert.deepEqual(invalidSocket.closeCalls, [{ code: 1002, reason: "invalid world-state roster" }]);
    invalid.dispose();

    const unidentified = new WorldStateController("ws://example.test/unidentified");
    const unidentifiedSocket = FakeWebSocket.instances[2];
    unidentifiedSocket.open();
    unidentifiedSocket.message({ props: [], challenges: [], traffic_manifest: null });
    assert.deepEqual(unidentifiedSocket.closeCalls, [{ code: 1002, reason: "invalid world-state roster" }]);
    unidentified.dispose();
  } finally {
    globalThis.WebSocket = previous;
  }
});
