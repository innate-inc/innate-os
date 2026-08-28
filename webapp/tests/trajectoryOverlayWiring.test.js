// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// One browser-free smoke test for the overlay's ROS and canvas wiring.

import assert from "node:assert/strict";

/** @type {any} */ const g = globalThis;
const rafQueue = [];
g.requestAnimationFrame = (fn) => {
  rafQueue.push(fn);
  return rafQueue.length;
};
g.cancelAnimationFrame = () => {};
g.ResizeObserver = class {
  constructor(cb) {
    this.cb = cb;
  }
  observe() {
    this.cb();
  }
  disconnect() {}
};
g.localStorage = { getItem: () => null, setItem() {} };
g.window = { devicePixelRatio: 1 };

const ctx = {
  filled: 0,
  polygon: [],
  setTransform() {},
  clearRect() {},
  save() {},
  restore() {},
  rect() {},
  clip() {},
  beginPath() {
    this.polygon = [];
  },
  moveTo(x, y) {
    this.polygon.push({ x, y });
  },
  lineTo(x, y) {
    this.polygon.push({ x, y });
  },
  closePath() {},
  fill() {
    this.filled += 1;
  },
  fillStyle: "",
};

function makeEl() {
  return {
    className: "",
    innerHTML: "",
    type: "",
    title: "",
    width: 0,
    height: 0,
    clientWidth: 1280,
    clientHeight: 720,
    classList: { toggle() {} },
    listeners: {},
    getContext: () => ctx,
    appendChild() {},
    remove() {},
    setAttribute() {},
    addEventListener(name, fn) {
      this.listeners[name] = fn;
    },
    removeEventListener() {},
  };
}
g.document = { createElement: () => makeEl() };

const { createTrajectoryOverlay } = await import("../js/teleop/trajectoryOverlay.js");
const handlers = new Map();
const ros = {
  subscribe(topic, handler) {
    handlers.set(topic, handler);
    return () => handlers.delete(topic);
  },
  emit(topic, msg) {
    const handler = handlers.get(topic);
    assert.ok(handler, `no subscription for ${topic}`);
    handler(msg);
  },
};
const session = { primaryCamera: { index: 0, name: "main" }, onChange: () => () => {} };
const video = makeEl();
video.videoWidth = 640;
video.videoHeight = 480;
const overlay = createTrajectoryOverlay(makeEl(), video, makeEl(), ros, session);

for (const topic of [
  "/navigation/plan",
  "/mapfree/plan",
  "/odom",
  "/amcl_pose",
  "/mars/head/current_position",
]) {
  assert.ok(handlers.has(topic), `missing subscription: ${topic}`);
}

ros.emit("/odom", {
  pose: { pose: { position: { x: 0, y: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } } },
});
ros.emit("/navigation/plan", {
  header: { frame_id: "map" },
  poses: [1, 2, 3].map((x) => ({ pose: { position: { x, y: 0 } } })),
});
while (rafQueue.length) rafQueue.shift()();
assert.equal(ctx.filled, 1, "a plan and pose should draw one ribbon");
assert.ok(ctx.polygon.length >= 6, "the filled ribbon should be a real polygon");

session.primaryCamera.name = "arm";
ros.emit("/navigation/plan", {
  header: { frame_id: "map" },
  poses: [1, 2, 3].map((x) => ({ pose: { position: { x, y: 0 } } })),
});
while (rafQueue.length) rafQueue.shift()();
assert.equal(ctx.filled, 1, "a non-head camera must suppress drawing");

overlay.destroy();
assert.equal(handlers.size, 0, "destroy must drop every subscription");
console.log("ok - overlay subscribes, draws, gates by camera, and cleans up");
