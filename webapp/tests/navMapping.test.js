// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Mapping-mode and disruptive-operation tests — zero dependencies, plain node:
//   node tests/navMapping.test.js

import assert from "node:assert/strict";

// navStore imports the shared RosClient singleton. Its constructor only needs
// these browser event surfaces; the store itself receives the fake client below.
class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.classList = { toggle() {} };
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  addEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(callback);
    this.listeners.set(type, listeners);
  }

  async emit(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) await callback(event);
  }

  focus() {}
  remove() {}
}

globalThis.window = { addEventListener() {} };
globalThis.document = {
  addEventListener() {},
  createElement(tagName) {
    return new FakeElement(tagName);
  },
  visibilityState: "visible",
};
globalThis.localStorage = { getItem() { return null; } };

const {
  CANCEL_SKILL_SERVICE,
  JOYSTICK_TOPIC,
  NAV_CHANGE_MAP_SERVICE,
  NAV_CHANGE_MODE_SERVICE,
  PINNED_SKILLS,
  NAV_SAVE_MAP_SERVICE,
  isMappingNavMode,
} = await import("../js/constants.js");
const { createNavStore } = await import("../js/nav/navStore.js");
const { createMappingSession, quiesceForMapNaming } = await import("../js/nav/mappingSession.js");

let passed = 0;
/** @param {string} name @param {() => void | Promise<void>} fn */
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

function fakeRos() {
  const calls = [];
  return {
    calls,
    subscribe() {
      return () => {};
    },
    async callService(service, args, timeoutMs) {
      calls.push({ service, args, timeoutMs });
      return { success: true, message: "ok" };
    },
  };
}

await test("manual and autonomous mapping share the mapping UI policy", () => {
  assert.equal(isMappingNavMode("mapping"), true);
  assert.equal(isMappingNavMode("autonomous_mapping"), true);
  assert.equal(isMappingNavMode("navigation"), false);
  assert.equal(isMappingNavMode("mapfree"), false);
  assert.equal(isMappingNavMode(null), false);
});

await test("Explore Map is pinned and takeover preserves the robot teleop pipeline", () => {
  assert.equal(PINNED_SKILLS.includes("explore map"), true);
  assert.equal(JOYSTICK_TOPIC, "/joystick");
});

await test("Finish settles autonomous navigation before map naming", async () => {
  const calls = [];
  const store = {
    state: { mode: "autonomous_mapping" },
    async changeMode(mode) {
      calls.push(mode);
      return true;
    },
  };

  assert.equal(await quiesceForMapNaming(/** @type {any} */ (store)), true);
  assert.deepEqual(calls, ["mapping"]);

  store.state.mode = "mapping";
  assert.equal(await quiesceForMapNaming(/** @type {any} */ (store)), true);
  assert.deepEqual(calls, ["mapping"]);
});

await test("Finish preserves naming across service-before-final-mode delivery", async () => {
  const listeners = new Set();
  const store = {
    state: { mode: "autonomous_mapping", maps: [], currentMap: "", busy: null, status: null },
    onChange(callback) {
      listeners.add(callback);
      callback(this.state);
      return () => listeners.delete(callback);
    },
    async changeMode(mode) {
      assert.equal(mode, "mapping");
      this.state.mode = "switching";
      for (const callback of [...listeners]) callback(this.state);
      // The service can resolve before the final latched mode topic arrives.
      return true;
    },
  };
  const scene = new FakeElement("section");
  const session = createMappingSession(/** @type {any} */ (scene), /** @type {any} */ (store));
  const banner = scene.children[0];
  const primary = banner.children.find((child) => child.tagName === "button" && child.textContent === "Finish");

  await primary.emit("click");
  assert.equal(banner.hidden, true);

  store.state.mode = "mapping";
  for (const callback of [...listeners]) callback(store.state);
  assert.equal(banner.hidden, false);
  assert.equal(primary.textContent, "Save");
  session.destroy();
});

await test("explicit mode changes best-effort cancel the active skill first", async () => {
  const client = fakeRos();
  const store = createNavStore(/** @type {any} */ (client));

  assert.equal(await store.changeMode("navigation"), true);
  assert.deepEqual(client.calls.map((call) => call.service), [CANCEL_SKILL_SERVICE, NAV_CHANGE_MODE_SERVICE]);
  store.destroy();
});

await test("starting manual mapping leaves the robot-side Slow policy to mars_app", async () => {
  const client = fakeRos();
  const store = createNavStore(/** @type {any} */ (client));

  assert.equal(await store.startMapping(), true);
  assert.deepEqual(client.calls.map((call) => call.service), [CANCEL_SKILL_SERVICE, NAV_CHANGE_MODE_SERVICE]);
  store.destroy();
});

await test("a missing cancel service never blocks the requested mode change", async () => {
  const client = fakeRos();
  client.callService = async (service, args, timeoutMs) => {
    client.calls.push({ service, args, timeoutMs });
    if (service === CANCEL_SKILL_SERVICE) throw new Error("service unavailable");
    return { success: true, message: "ok" };
  };
  const store = createNavStore(/** @type {any} */ (client));
  const originalWarn = console.warn;
  console.warn = () => {};

  try {
    assert.equal(await store.changeMode("mapfree"), true);
    assert.deepEqual(client.calls.map((call) => call.service), [CANCEL_SKILL_SERVICE, NAV_CHANGE_MODE_SERVICE]);
  } finally {
    console.warn = originalWarn;
    store.destroy();
  }
});

await test("map activation cancels the active skill before switching maps", async () => {
  const client = fakeRos();
  const store = createNavStore(/** @type {any} */ (client));

  assert.equal(await store.changeMap("home.yaml"), true);
  assert.deepEqual(client.calls.map((call) => call.service), [CANCEL_SKILL_SERVICE, NAV_CHANGE_MAP_SERVICE]);
  store.destroy();
});

await test("save-and-activate cancels before the sequence, then saves, changes mode, and loads", async () => {
  const client = fakeRos();
  const store = createNavStore(/** @type {any} */ (client));

  assert.equal(await store.saveAndActivate("office", false), true);
  assert.deepEqual(client.calls.map((call) => call.service), [
    CANCEL_SKILL_SERVICE,
    NAV_SAVE_MAP_SERVICE,
    NAV_CHANGE_MODE_SERVICE,
    NAV_CHANGE_MAP_SERVICE,
  ]);
  store.destroy();
});

console.log(`\n${passed} passed`);
