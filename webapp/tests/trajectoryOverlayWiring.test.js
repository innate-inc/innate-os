// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Wiring tests for js/teleop/trajectoryOverlay.js — zero dependencies, plain node:
//   node tests/trajectoryOverlayWiring.test.js
// The geometry is covered next door in trajectoryOverlay.test.js; this drives
// the whole module against a stub DOM instead, because the failure that
// actually shipped was wiring, not math: nothing reached the canvas. Feed it a
// plan, a pose and a head angle, then assert a polygon really got filled — and
// that the gates (wrong camera, stale plan, toggle off) each stop it.

import assert from "node:assert/strict";

/** @type {any} */ const g = globalThis;

const rafQueue = [];
g.requestAnimationFrame = (fn) => {
  rafQueue.push(fn);
  return rafQueue.length;
};
g.cancelAnimationFrame = () => {};
const flush = () => {
  while (rafQueue.length) rafQueue.shift()();
};

class ResizeObserverStub {
  constructor(cb) {
    this.cb = cb;
  }
  observe() {
    this.cb();
  }
  disconnect() {}
}
g.ResizeObserver = ResizeObserverStub;

const store = new Map();
g.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, v),
};
g.window = { devicePixelRatio: 1 };

/** Records what the module draws so tests can assert on the filled polygon. */
function makeCtx() {
  return {
    polygon: [],
    filled: 0,
    setTransform() {},
    clearRect() {},
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
}

let ctx = makeCtx();
function makeEl() {
  const el = {
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
  return el;
}
g.document = { createElement: () => makeEl() };

const { createTrajectoryOverlay } = await import("../js/teleop/trajectoryOverlay.js");

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  ctx = makeCtx();
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** Stand-in for RosClient: hands back each topic's handler so tests can publish. */
function makeRos() {
  /** @type {Map<string, (msg: any) => void>} */
  const handlers = new Map();
  return {
    handlers,
    subscribe(topic, handler) {
      handlers.set(topic, handler);
      return () => handlers.delete(topic);
    },
    /** @param {string} topic @param {any} msg */
    emit(topic, msg) {
      const h = handlers.get(topic);
      assert.ok(h, `no subscription for ${topic}`);
      h(msg);
    },
  };
}

function makeSession(cameraName = "main") {
  return { primaryCamera: { index: 0, name: cameraName }, onChange: () => () => {} };
}

/** A straight 3 m path ahead of a robot sitting at the origin. */
const STRAIGHT_PLAN = {
  header: { frame_id: "map" },
  poses: [1, 2, 3].map((x) => ({ pose: { position: { x, y: 0 } } })),
};
const AT_ORIGIN = {
  pose: { pose: { position: { x: 0, y: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } } },
};

/** @param {{ camera?: string }} [opts] */
function mount(opts = {}) {
  const ros = makeRos();
  const stage = makeEl();
  const video = makeEl();
  video.videoWidth = 640;
  video.videoHeight = 480;
  const rail = makeEl();
  const overlay = createTrajectoryOverlay(stage, video, rail, ros, makeSession(opts.camera));
  return { ros, overlay };
}

test("a plan plus odom paints a ribbon on the canvas", () => {
  const { ros, overlay } = mount();
  ros.emit("/odom", AT_ORIGIN);
  ros.emit("/navigation/plan", STRAIGHT_PLAN);
  flush();
  assert.equal(ctx.filled, 1, "expected exactly one filled ribbon");
  assert.ok(ctx.polygon.length >= 6, `polygon too small: ${ctx.polygon.length} vertices`);
  // Straight ahead: every vertex hugs the image's horizontal centre (640/2 at
  // 320-px calibration scaled to 640 wide → principal point ≈ 306).
  for (const p of ctx.polygon) {
    assert.ok(Math.abs(p.x - 306) < 12, `vertex drifted off centre: ${p.x}`);
  }
  overlay.destroy();
});

test("subscribes to both planner topics, odom, amcl and head pitch", () => {
  const { ros, overlay } = mount();
  for (const topic of [
    "/navigation/plan",
    "/mapfree/plan",
    "/odom",
    "/amcl_pose",
    "/mars/head/current_position",
  ]) {
    assert.ok(ros.handlers.has(topic), `missing subscription: ${topic}`);
  }
  overlay.destroy();
  assert.equal(ros.handlers.size, 0, "destroy must drop every subscription");
});

test("head pitch moves the ribbon down the image", () => {
  const { ros, overlay } = mount();
  ros.emit("/odom", AT_ORIGIN);
  ros.emit("/navigation/plan", STRAIGHT_PLAN);
  flush();
  const level = Math.min(...ctx.polygon.map((p) => p.y));
  ctx = makeCtx();
  ros.emit("/mars/head/current_position", { data: JSON.stringify({ current_position: 20 }) });
  flush();
  const tilted = Math.min(...ctx.polygon.map((p) => p.y));
  assert.ok(tilted > level, `pitching up should push the ribbon down (${tilted} vs ${level})`);
  overlay.destroy();
});

test("mapfree plans are read in the odom frame", () => {
  const { ros, overlay } = mount();
  // Robot has driven 5 m along +x; an odom-frame plan just ahead of it must
  // still project ahead, not 5 m behind (which would cull to nothing).
  ros.emit("/odom", {
    pose: { pose: { position: { x: 5, y: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } } },
  });
  ros.emit("/mapfree/plan", {
    header: { frame_id: "odom" },
    poses: [6, 7, 8].map((x) => ({ pose: { position: { x, y: 0 } } })),
  });
  flush();
  assert.equal(ctx.filled, 1, "odom-frame plan should still draw");
  overlay.destroy();
});

test("nothing draws while a non-head camera is primary", () => {
  const { ros, overlay } = mount({ camera: "arm" });
  ros.emit("/odom", AT_ORIGIN);
  ros.emit("/navigation/plan", STRAIGHT_PLAN);
  flush();
  assert.equal(ctx.filled, 0);
  overlay.destroy();
});

test("an empty path clears the route", () => {
  const { ros, overlay } = mount();
  ros.emit("/odom", AT_ORIGIN);
  ros.emit("/navigation/plan", STRAIGHT_PLAN);
  flush();
  assert.equal(ctx.filled, 1);
  ctx = makeCtx();
  ros.emit("/navigation/plan", { header: { frame_id: "map" }, poses: [] });
  flush();
  assert.equal(ctx.filled, 0, "cleared route must not repaint");
  overlay.destroy();
});

test("a plan with no pose yet draws nothing rather than guessing the origin", () => {
  const { ros, overlay } = mount();
  ros.emit("/navigation/plan", STRAIGHT_PLAN);
  flush();
  assert.equal(ctx.filled, 0);
  overlay.destroy();
});

test("blocked storage (Safari 'Block all cookies') must not kill the mount", () => {
  const prior = g.localStorage;
  g.localStorage = {
    getItem() {
      throw new Error("SecurityError");
    },
    setItem() {
      throw new Error("SecurityError");
    },
  };
  try {
    const { ros, overlay } = mount();
    assert.ok(ros.handlers.size > 0, "overlay must default enabled when storage throws");
    ros.emit("/odom", AT_ORIGIN);
    ros.emit("/navigation/plan", STRAIGHT_PLAN);
    flush();
    assert.equal(ctx.filled, 1, "still draws with storage blocked");
    overlay.destroy();
  } finally {
    g.localStorage = prior;
  }
});

console.log(`\n${passed} tests passed`);
