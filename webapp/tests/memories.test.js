// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pure-helper tests for js/map/memories.js — zero dependencies, plain node:
//   node tests/memories.test.js
// Parsing tolerance matters most: these payloads cross rosbridge as JSON in a
// String and a malformed one must degrade to "keep the last good state", never
// to a layer crash.

import assert from "node:assert/strict";
import {
  ageAlpha,
  ageText,
  cacheLabel,
  headerSkew,
  memoryImageUrl,
  parseMemories,
  parseSearch,
  withAlpha,
} from "../js/map/memories.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

const NOW = 1_754_500_000;

test("parseMemories reads a full payload", () => {
  const msg = {
    data: JSON.stringify({
      map: "Home.yaml",
      fingerprint: "abc123def456",
      cache: "warm",
      positions: [{ id: 3, x: 1.5, y: -0.25, theta: 1.57, stamp: NOW }],
    }),
  };
  assert.deepEqual(parseMemories(msg), {
    map: "Home.yaml",
    fingerprint: "abc123def456",
    cache: "warm",
    memories: [{ id: 3, x: 1.5, y: -0.25, theta: 1.57, stamp: NOW }],
  });
});

test("parseMemories skips malformed entries and defaults missing fields", () => {
  const msg = {
    data: JSON.stringify({ positions: [{ id: 1, x: 0, y: 0 }, { x: 2, y: 2 }, "junk", null] }),
  };
  const state = parseMemories(msg);
  assert.equal(state?.map, "");
  assert.equal(state?.fingerprint, "");
  assert.equal(state?.cache, "off");
  assert.deepEqual(state?.memories, [{ id: 1, x: 0, y: 0, theta: 0, stamp: 0 }]);
});

test("headerSkew reads the robot-browser offset from a stamped message", () => {
  const sec = Math.floor(Date.now() / 1000) + 3600;
  const skew = headerSkew({ header: { stamp: { sec, nanosec: 500_000_000 } } });
  assert.ok(skew !== null && Math.abs(skew - 3600.5) < 5, `an hour-ahead robot reads as ~+3600, got ${skew}`);
  assert.equal(headerSkew({ header: { stamp: {} } }), null);
  assert.equal(headerSkew({}), null);
});

test("parseMemories rejects garbage rather than throwing", () => {
  assert.equal(parseMemories({ data: "{not json" }), null);
  assert.equal(parseMemories({ data: JSON.stringify({ positions: "nope" }) }), null);
  assert.equal(parseMemories({}), null);
});

test("parseSearch keeps a verdict and rejects garbage", () => {
  const verdict = { query: "the kitchen", found: true, id: 2, x: 1, y: 2, stamp: NOW };
  assert.deepEqual(parseSearch({ data: JSON.stringify(verdict) }), verdict);
  assert.equal(parseSearch({ data: JSON.stringify({ found: true }) }), null); // no query/stamp
  assert.equal(parseSearch({ data: "]" }), null);
});

test("memoryImageUrl carries the map, id, and stamp cache-buster", () => {
  const url = memoryImageUrl("My Home.yaml", { id: 7, x: 0, y: 0, theta: 0, stamp: 1234.6 });
  assert.equal(url, "/memory/image?map=My%20Home.yaml&id=7&v=1235");
});

test("ageText buckets read naturally", () => {
  assert.equal(ageText(NOW - 30, NOW), "just now");
  assert.equal(ageText(NOW - 5 * 60, NOW), "5 min ago");
  assert.equal(ageText(NOW - 3 * 3600, NOW), "3 h ago");
  assert.equal(ageText(NOW - 2 * 86400, NOW), "2 d ago");
  assert.equal(ageText(NOW + 999, NOW), "just now"); // clock skew must not go negative
});

test("ageAlpha glows fresh, floors old, and eases between", () => {
  assert.equal(ageAlpha(NOW, NOW), 1);
  assert.equal(ageAlpha(NOW - 599, NOW), 1);
  assert.equal(ageAlpha(NOW - 100 * 86400, NOW), 0.35);
  const mid = ageAlpha(NOW - 4 * 3600, NOW);
  assert.ok(mid > 0.35 && mid < 1, `mid-age alpha in the open interval, got ${mid}`);
});

test("withAlpha formats and clamps", () => {
  assert.equal(withAlpha("#b48cff", 0.5), "rgb(180 140 255 / 0.5)");
  assert.equal(withAlpha("#000000", 2), "rgb(0 0 0 / 1)");
  assert.equal(withAlpha("#ffffff", -1), "rgb(255 255 255 / 0)");
});

test("cacheLabel covers every state the robot publishes", () => {
  for (const state of ["warm", "cold", "inline", "unsupported", "off"]) {
    const { text, kind } = cacheLabel(state);
    assert.ok(text.length > 0 && ["ok", "warn", "muted"].includes(kind), state);
  }
  assert.equal(cacheLabel("warm").kind, "ok");
  assert.equal(cacheLabel("cold").kind, "warn");
  assert.equal(cacheLabel("banana").kind, "muted");
});

console.log(`\n${passed} passed`);
