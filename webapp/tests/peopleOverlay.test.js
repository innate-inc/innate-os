// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pure-helper tests for js/agent/peopleOverlay.js — zero dependencies, plain node:
//   node tests/peopleOverlay.test.js
// Two things decide whether a box lands on the right person: the per-mille →
// pixel mapping against the letterbox `object-fit: contain` leaves, and the
// refusal to draw a snapshot that no longer describes the picture. Both are
// here, along with the parse tolerance every JSON-in-String payload needs.

import assert from "node:assert/strict";
import {
  STATE_COLORS,
  boxRect,
  containRect,
  isFresh,
  parseSnapshot,
  stateColor,
  tagLabel,
} from "../js/agent/peopleOverlay.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

const NOW = 1_788_818_400.12;

/** The snapshot of docs/rfc/people-memory.md, trimmed to what the overlay reads. */
const SNAPSHOT = {
  schema: 1,
  stamp: NOW,
  frame_stamp_ns: "1788818400123456789",
  image_size: [640, 480],
  health: { camera: "ok", face_model: "ok" },
  collection_enabled: true,
  attention: { tag: "P4", text: "trying to see P4's face", head_bbox: [120, 400, 220, 480] },
  people: [
    {
      tag: "P3",
      person_id: "person_7f92a1b3",
      name: "Theo",
      state: "known",
      evidence: ["face"],
      confidence: 0.91,
      bbox: [100, 300, 930, 560],
      head_bbox: [100, 380, 260, 480],
      range_m: 1.8,
      tracked_sec: 41.2,
      lost: false,
      description: "Man, 30s, glasses.",
    },
    { tag: "P5", person_id: null, name: null, state: "unknown", bbox: [200, 600, 900, 800], lost: false },
  ],
  recent: [{ person_id: "person_11aa22bb", name: "Marc", last_seen: NOW - 18400 }],
};

/** @param {object} snapshot */
function msg(snapshot) {
  return { data: JSON.stringify(snapshot) };
}

test("containRect pillarboxes a 4:3 frame in a wide stage", () => {
  // 640x480 into 1600x900: height binds, so the frame is 1200 wide, centered.
  assert.deepEqual(containRect(640, 480, 1600, 900), { x: 200, y: 0, w: 1200, h: 900 });
});

test("containRect letterboxes a 4:3 frame in a square stage", () => {
  assert.deepEqual(containRect(640, 480, 640, 640), { x: 0, y: 80, w: 640, h: 480 });
});

test("containRect falls back to the whole stage when the frame size is unknown", () => {
  // The sim's canvas has no stream of its own: it renders the head camera at
  // whatever size the stage is, so there is no letterbox to correct for.
  assert.deepEqual(containRect(0, 0, 800, 600), { x: 0, y: 0, w: 800, h: 600 });
});

test("boxRect maps a per-mille box onto the letterboxed frame", () => {
  const rect = containRect(640, 480, 1600, 900);
  // [ymin, xmin, ymax, xmax] = [100, 300, 930, 560] of a 1200x900 frame at x=200.
  assert.deepEqual(boxRect([100, 300, 930, 560], rect), { x: 560, y: 90, w: 312, h: 747 });
});

test("boxRect carries the letterbox offset into both axes", () => {
  const rect = containRect(640, 480, 640, 640);
  assert.deepEqual(boxRect([0, 0, 1000, 1000], rect), { x: 0, y: 80, w: 640, h: 480 });
  assert.deepEqual(boxRect([500, 500, 1000, 1000], rect), { x: 320, y: 320, w: 320, h: 240 });
});

test("a full-frame box fills exactly the frame, never the stage", () => {
  const rect = containRect(1280, 720, 500, 500);
  const box = boxRect([0, 0, 1000, 1000], rect);
  assert.deepEqual(box, rect);
});

test("state decides the color: teal settled, amber tentative, red contradicted", () => {
  assert.equal(stateColor("known"), STATE_COLORS.known);
  assert.equal(stateColor("conflict"), STATE_COLORS.conflict);
  for (const tentative of ["unknown", "familiar", "possible"]) {
    assert.equal(stateColor(tentative), STATE_COLORS.unknown);
  }
  // Teal is the only color that claims certainty, so an unrecognized state
  // must not reach it.
  assert.equal(stateColor("something_new"), STATE_COLORS.unknown);
  assert.notEqual(stateColor("something_new"), STATE_COLORS.known);
});

test("parseSnapshot reads the published snapshot", () => {
  const parsed = parseSnapshot(msg(SNAPSHOT));
  assert.equal(parsed?.stamp, NOW);
  assert.deepEqual(parsed?.people, [
    { tag: "P3", name: "Theo", state: "known", bbox: [100, 300, 930, 560] },
    { tag: "P5", name: null, state: "unknown", bbox: [200, 600, 900, 800] },
  ]);
});

test("parseSnapshot drops lost tracks and unusable boxes", () => {
  const parsed = parseSnapshot(
    msg({
      stamp: NOW,
      people: [
        // A lost track's box is where the tracker GUESSES they went.
        { tag: "P1", state: "known", name: "Ana", bbox: [0, 0, 500, 500], lost: true },
        { tag: "P2", state: "known", bbox: [500, 0, 100, 500] }, // inverted
        { tag: "P3", state: "known", bbox: [0, 0, 100] }, // short
        { tag: "P4", state: "known", bbox: [0, 0, 100, "500"] }, // not numbers
        { tag: "P5", state: "known", bbox: [0, 0, 100, 100] },
      ],
    }),
  );
  assert.deepEqual(
    parsed?.people.map((p) => p.tag),
    ["P5"],
  );
});

test("parseSnapshot clamps a box to the frame", () => {
  const parsed = parseSnapshot(msg({ stamp: NOW, people: [{ tag: "P1", state: "known", bbox: [-40, 0, 1200, 990] }] }));
  assert.deepEqual(parsed?.people[0].bbox, [0, 0, 1000, 990]);
});

test("parseSnapshot rejects garbage rather than throwing", () => {
  assert.equal(parseSnapshot({ data: "{not json" }), null);
  assert.equal(parseSnapshot({}), null);
  assert.equal(parseSnapshot(null), null);
  assert.equal(parseSnapshot({ data: JSON.stringify({ stamp: NOW }) }), null);
});

test("an empty people list parses to an empty overlay, not to null", () => {
  // "The node is running and nobody is there" is a real answer, and it is what
  // takes the previous boxes down.
  const parsed = parseSnapshot(msg({ stamp: NOW, people: [] }));
  assert.deepEqual(parsed, { stamp: NOW, people: [] });
});

test("tagLabel pairs the tag with the name once there is one", () => {
  assert.equal(tagLabel({ tag: "P3", name: "Theo" }), "P3 · Theo");
  assert.equal(tagLabel({ tag: "P5", name: null }), "P5");
  assert.equal(tagLabel({ tag: "", name: "Theo" }), "Theo");
  assert.equal(tagLabel({ tag: "", name: null }), "");
});

test("a snapshot older than two seconds stops being drawn", () => {
  const arrived = 10_000;
  assert.equal(isFresh(arrived, arrived), true);
  assert.equal(isFresh(arrived, arrived + 1_999), true);
  assert.equal(isFresh(arrived, arrived + 2_000), false);
  assert.equal(isFresh(arrived, arrived + 60_000), false);
});

console.log(`\n${passed} passed`);
