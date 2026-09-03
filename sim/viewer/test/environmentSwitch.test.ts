// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import test from "node:test";

class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  message(value: unknown): void {
    this.onmessage?.({ data: JSON.stringify(value) });
  }

  send(_payload: string): void {}

  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

function fakeScene(fingerprint: string): object {
  return {
    environmentFingerprint: fingerprint,
    clearWorldState() {},
    setPropManifest() {},
    setTrafficManifest() {},
    setTrafficState() {},
    setLidarVisible() {},
    setCollisionHullsVisible() {},
    spawnAt() {},
    setPose() {},
    setJointAngles() {},
    setObjectPoses() {},
  };
}

function worldState(worldEpoch: number): object {
  return {
    t: worldEpoch,
    wall: Date.now() / 1000,
    world_epoch: worldEpoch,
    pose: [worldEpoch, 0, 0],
    joints: {},
    objects: {},
  };
}

test("one environment generation interlocks drive until its matching scene is ready", async () => {
  // Exercise the real world-state/session sequence: generation A is prepared,
  // generation B connects and publishes a pose, and it must remain connecting
  // until its own matching scene is prepared and marked ready.
  let switching = false;
  Object.defineProperty(globalThis, "location", {
    configurable: true,
    value: { protocol: "http:", host: "localhost", hostname: "localhost" },
  });
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: new EventTarget(),
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: Object.assign(new EventTarget(), {
      visibilityState: "visible",
      documentElement: { classList: { contains: () => switching } },
      createElement: () => ({ width: 0, height: 0, getContext: () => null }),
    }),
  });
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: { getItem: () => null, setItem() {}, removeItem() {} },
  });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: FakeWebSocket });

  // The same event dispatched by SimStage must neutralize held drive input,
  // and the replacement world requires a release before accepting it again.
  const { DriveController } = await import("../../../webapp/js/driveController.js");
  const drive = new DriveController();
  let activeDrive: { source: string | null; x: number; y: number; engaged: boolean } | null = null;
  drive.onActiveChange((state: typeof activeDrive) => { activeDrive = state; });
  drive.setInput("keyboard", 0, 1, true);
  assert.equal(activeDrive?.engaged, true);
  switching = true;
  document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: true }));
  assert.deepEqual(activeDrive, { source: null, x: 0, y: 0, engaged: false });
  drive.setInput("joystick", 0.5, 0.5, true);
  switching = false;
  document.dispatchEvent(new CustomEvent("innate:sim-environment-switch-state", { detail: false }));
  drive.setInput("joystick", 0.5, 0.5, true);
  assert.equal(activeDrive?.engaged, false);
  drive.setInput("joystick", 0, 0, false);
  drive.setInput("joystick", 0.5, 0.5, true);
  assert.equal(activeDrive?.engaged, true);
  drive.haltAll();

  // Import the same bundled entry point loaded by the webapp. Raw source
  // imports are intentionally left extensionless for Vite and cannot be
  // traversed by Node's strip-types runner.
  const { SimSession } = await import("../dist-lib/sim-session.js");
  const session = new SimSession({ stateUrl: "ws://world.test" });
  session.start();
  const firstSocket = FakeWebSocket.instances.at(-1)!;
  firstSocket.open();
  firstSocket.message({
    environment: { id: "apartment", fingerprint: "apartment-v1" }, props: [], challenges: [], traffic_manifest: null,
  });
  assert.equal(session.prepareScene(fakeScene("apartment-v1")), false);
  firstSocket.message(worldState(1));
  assert.equal(session.prepareScene(fakeScene("apartment-v1")), true);
  session.stageReady();
  assert.equal(session.state.status, "streaming");

  firstSocket.close();
  assert.equal(session.state.status, "connecting");
  await new Promise((resolve) => setTimeout(resolve, 550));
  const replacementSocket = FakeWebSocket.instances.at(-1)!;
  assert.notEqual(replacementSocket, firstSocket);
  replacementSocket.open();
  replacementSocket.message({
    environment: { id: "town", fingerprint: "town-v2" }, props: [], challenges: [], traffic_manifest: null,
  });
  replacementSocket.message(worldState(2));
  assert.equal(session.state.status, "connecting");
  assert.equal(session.state.videoLive.some(Boolean), false);
  assert.equal(session.prepareScene(fakeScene("apartment-v1")), false);
  assert.equal(session.prepareScene(fakeScene("town-v2")), true);
  assert.equal(session.state.status, "connecting");
  session.stageReady();
  assert.equal(session.state.status, "streaming");
  session.destroy();
});
