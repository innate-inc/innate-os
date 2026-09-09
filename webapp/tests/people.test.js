// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pure-helper tests for js/settings/people.js — zero dependencies, plain node:
//   node tests/people.test.js
// The request bodies are checked against the .srv files themselves rather than
// against a copy of them: rws reports a field the robot never declared as a
// generic failure, and "Forget" silently doing nothing is the worst possible
// bug on this card. The rest is the roster the card renders from one fixture,
// including the difference between "nobody is known" and "nothing answered".

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  FORGET_PERSON_SERVICE,
  GET_PEOPLE_SERVICE,
  MERGE_PEOPLE_SERVICE,
  PEOPLE_SERVICE_TYPES,
  RENAME_PERSON_SERVICE,
  SET_PEOPLE_COLLECTION_SERVICE,
} from "../js/constants.js";
import {
  forgetPersonRequest,
  getPeopleRequest,
  mergePeopleRequest,
  optionLabel,
  parseRoster,
  personMeta,
  renamePersonRequest,
  rosterRows,
  setCollectionRequest,
  shortId,
  thumbnailSrc,
} from "../js/settings/people.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

const SRV_DIR = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../ros2_ws/src/brain/brain_messages/srv",
);

/**
 * The request field names a .srv declares, in order — everything before the
 * `---` separator that isn't a comment.
 * @param {string} type e.g. "brain_messages/srv/GetPeople"
 * @returns {string[]}
 */
function requestFields(type) {
  const file = resolve(SRV_DIR, `${type.split("/").pop()}.srv`);
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    assert.fail(`${type} is not at ${file} — has brain_messages moved?`);
  }
  return text
    .split(/^---\s*$/m)[0]
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => line.split(/\s+/)[1]);
}

const NOW = 1_788_818_400;

/** One GetPeople answer with `include_roster` + `include_thumbnails` set. */
const ROSTER_JSON = JSON.stringify({
  schema: 1,
  stamp: NOW,
  image_size: [640, 480],
  collection_enabled: true,
  capacity_full: false,
  people: [],
  roster: [
    {
      person_id: "person_7f92a1b3",
      name: "Theo",
      unnamed: false,
      encounters: 41,
      last_seen: { stamp: NOW - 300, map: "home", x: 3.1, y: 1.4 },
      thumbnail: "/9j/4AAQSkZJRgABAQ==",
      description: "Man, 30s, glasses.",
    },
    { person_id: "person_11aa22bb", name: null, unnamed: true, encounters: 1, last_seen: NOW - 2 * 86400 },
    { id: "person_0c1d2e3f", name: "  Zoe  ", encounters: 0 },
    { name: "no id at all" },
    "junk",
    null,
  ],
});

test("every request body is exactly its .srv request fields", () => {
  const bodies = {
    [GET_PEOPLE_SERVICE]: getPeopleRequest(true),
    [RENAME_PERSON_SERVICE]: renamePersonRequest("person_7f92a1b3", "Theo"),
    [MERGE_PEOPLE_SERVICE]: mergePeopleRequest("person_11aa22bb", "person_7f92a1b3"),
    [FORGET_PERSON_SERVICE]: forgetPersonRequest("person_7f92a1b3"),
    [SET_PEOPLE_COLLECTION_SERVICE]: setCollectionRequest(false),
  };
  assert.deepEqual(Object.keys(bodies).sort(), Object.keys(PEOPLE_SERVICE_TYPES).sort());
  for (const [service, body] of Object.entries(bodies)) {
    const fields = requestFields(PEOPLE_SERVICE_TYPES[service]);
    assert.ok(fields.length > 0, `${service}: no request fields parsed`);
    assert.deepEqual(Object.keys(body).sort(), [...fields].sort(), `${service} request fields`);
  }
});

test("the request values are the ones the services are documented to take", () => {
  // Thumbnails are a JPEG read per person on the people node's single-threaded
  // executor, so the default request leaves them out: only the card, once it is
  // actually on screen, asks for them.
  assert.deepEqual(getPeopleRequest(), { include_roster: true, include_thumbnails: false });
  assert.deepEqual(getPeopleRequest(true), { include_roster: true, include_thumbnails: true });
  // "app" is the consent path stored with the profile — a name typed here is
  // the owner naming someone, not the person saying their own name.
  // The card mutates the ids the roster listed, so it has no snapshot to have
  // decided on, and its calls are single clicks with nothing to retry.
  const decided = { idempotency_key: "", decided_on_stamp_ns: "" };
  assert.deepEqual(renamePersonRequest("P3", "Theo"), { who: "P3", name: "Theo", source: "app", ...decided });
  assert.deepEqual(mergePeopleRequest("a", "b"), { source_id: "a", target_id: "b", ...decided });
  assert.deepEqual(forgetPersonRequest("person_7f92a1b3"), { who: "person_7f92a1b3", ...decided });
  assert.deepEqual(setCollectionRequest(false), { enabled: false });
});

test("parseRoster reads the roster, its stamp, and the collection switch", () => {
  const roster = parseRoster(ROSTER_JSON);
  assert.equal(roster?.stamp, NOW);
  assert.equal(roster?.collectionEnabled, true);
  assert.equal(roster?.capacityFull, false);
  assert.deepEqual(
    roster?.people.map((p) => p.id),
    ["person_7f92a1b3", "person_11aa22bb", "person_0c1d2e3f"],
  );
  assert.deepEqual(roster?.people[0], {
    id: "person_7f92a1b3",
    name: "Theo",
    unnamed: false,
    lastSeen: NOW - 300,
    encounters: 41,
    thumbnail: "/9j/4AAQSkZJRgABAQ==",
    description: "Man, 30s, glasses.",
  });
  // A record with no name is unnamed even if the store didn't say so, and a
  // bare epoch reads the same as the store's {stamp, map, x, y}.
  assert.equal(roster?.people[1].unnamed, true);
  assert.equal(roster?.people[1].lastSeen, NOW - 2 * 86400);
  assert.equal(roster?.people[2].name, "Zoe");
  assert.equal(roster?.people[2].unnamed, false);
  assert.equal(roster?.people[2].lastSeen, 0);
});

test("parseRoster tells 'nobody is known' apart from 'nothing answered'", () => {
  assert.deepEqual(parseRoster(JSON.stringify({ stamp: NOW, roster: [] }))?.people, []);
  // A snapshot with no roster key is a people node that cannot answer this
  // card — showing it as an empty roster would claim the robot knows nobody.
  assert.equal(parseRoster(JSON.stringify({ stamp: NOW, people: [] })), null);
  assert.equal(parseRoster("{not json"), null);
  assert.equal(parseRoster(""), null);
});

test("collection_enabled defaults on, and capacity_full is only ever explicit", () => {
  const bare = parseRoster(JSON.stringify({ roster: [] }));
  assert.equal(bare?.collectionEnabled, true);
  assert.equal(bare?.capacityFull, false);
  const off = parseRoster(JSON.stringify({ roster: [], collection_enabled: false, capacity_full: true }));
  assert.equal(off?.collectionEnabled, false);
  assert.equal(off?.capacityFull, true);
});

test("rosterRows renders the fixture as the card shows it", () => {
  const roster = parseRoster(ROSTER_JSON);
  assert.ok(roster);
  const rows = rosterRows(roster);
  // Most recently seen first; someone never seen sits at the bottom.
  assert.deepEqual(
    rows.map((r) => r.id),
    ["person_7f92a1b3", "person_11aa22bb", "person_0c1d2e3f"],
  );
  assert.equal(rows[0].name, "Theo");
  assert.equal(rows[0].meta, "Last seen 5 min ago · 41 encounters");
  assert.equal(rows[0].thumbnailSrc, "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ==");
  assert.equal(rows[0].initial, "T");
  assert.equal(rows[0].description, "Man, 30s, glasses.");
  assert.equal(rows[1].name, "unnamed");
  assert.equal(rows[1].label, "unnamed 11aa22bb");
  assert.equal(rows[1].meta, "Last seen 2 d ago · 1 encounter");
  assert.equal(rows[1].thumbnailSrc, null);
  assert.equal(rows[1].initial, "?");
  assert.equal(rows[2].meta, "Not seen yet · 0 encounters");
});

test("merge targets are everyone else, named so they can be told apart", () => {
  const roster = parseRoster(ROSTER_JSON);
  assert.ok(roster);
  const rows = rosterRows(roster);
  for (const row of rows) {
    assert.ok(!row.mergeOptions.some((option) => option.value === row.id), "cannot merge into itself");
    assert.equal(row.mergeOptions.length, rows.length - 1);
  }
  assert.deepEqual(
    rows[0].mergeOptions.map((option) => option.label),
    ["unnamed 11aa22bb", "Zoe"],
  );
  // The only person on the roster has nowhere to merge, so the card's select
  // has no options to offer.
  const alone = parseRoster(JSON.stringify({ roster: [{ person_id: "person_1", name: "Ana" }] }));
  assert.ok(alone);
  assert.deepEqual(rosterRows(alone)[0].mergeOptions, []);
});

test("relative times come off the robot's own clock, not the browser's", () => {
  const roster = parseRoster(ROSTER_JSON);
  assert.ok(roster);
  // The stamp in the answer is the robot's "now": a robot hours off the
  // browser must not read as "last seen 4 h ago" for someone in the room.
  assert.equal(rosterRows(roster)[0].meta, rosterRows(roster, NOW)[0].meta);
  assert.equal(personMeta(roster.people[0], NOW + 4 * 3600), "Last seen 4 h ago · 41 encounters");
});

test("ids and thumbnails degrade to something showable", () => {
  assert.equal(shortId("person_7f92a1b3"), "7f92a1b3");
  assert.equal(shortId("legacy-id"), "legacy-id");
  assert.equal(optionLabel({ id: "person_9", name: "Ana" }), "Ana");
  assert.equal(optionLabel({ id: "person_9", name: null }), "unnamed 9");
  assert.equal(thumbnailSrc({ thumbnail: null }), null);
  // A robot that already framed its thumbnail as a data URL is passed through
  // rather than base64-prefixed twice.
  assert.equal(thumbnailSrc({ thumbnail: "data:image/png;base64,AAA" }), "data:image/png;base64,AAA");
});

console.log(`\n${passed} passed`);
