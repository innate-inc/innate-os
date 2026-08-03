// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import { createSimEnvironmentWatcher, startSimEnvironmentWatcher } from "../js/simEnvironmentReload.js";

let passed = 0;
/** @param {string} name @param {() => Promise<void>} fn */
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

function reply(status, fingerprint = "") {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => ({ fingerprint }),
  };
}

await test("reloads once when the selected environment fingerprint changes", async () => {
  const replies = [reply(200, "apartment"), reply(200, "apartment"), reply(200, "gallery")];
  let reloads = 0;
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async () => replies.shift(),
    reloadFn: () => {
      reloads += 1;
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });
  await watcher.poll();
  await watcher.poll();
  assert.equal(reloads, 0);
  await watcher.poll();
  assert.equal(reloads, 1);
  await watcher.poll();
  assert.equal(reloads, 1);
});

await test("a temporarily missing descriptor reloads when activation appears", async () => {
  const replies = [reply(404), reply(200, "gallery")];
  let scheduled = 0;
  let reloads = 0;
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async () => replies.shift(),
    reloadFn: () => {
      reloads += 1;
    },
    setIntervalFn: () => {
      scheduled += 1;
      return 1;
    },
    clearIntervalFn: () => {},
  });
  await watcher.start();
  assert.equal(scheduled, 1);
  await watcher.poll();
  assert.equal(reloads, 1);
});

await test("the first poll reloads when the scene already resolved an older pack", async () => {
  let reloads = 0;
  const watcher = createSimEnvironmentWatcher({
    fetchFn: async () => reply(200, "gallery"),
    loadedFingerprintFn: () => "apartment",
    reloadFn: () => {
      reloads += 1;
    },
    setIntervalFn: () => 1,
    clearIntervalFn: () => {},
  });

  await watcher.start();
  assert.equal(reloads, 1);
});

await test("the config gate does not start environment polling on a real robot", async () => {
  let requests = 0;
  let scheduled = 0;
  const watcher = await startSimEnvironmentWatcher({
    fetchFn: async () => {
      requests += 1;
      return { status: 200, ok: true, json: async () => ({ simControls: false }) };
    },
    setIntervalFn: () => {
      scheduled += 1;
      return 1;
    },
  });
  assert.equal(watcher, null);
  assert.equal(requests, 1);
  assert.equal(scheduled, 0);
});

console.log(`\n${passed} passed`);
