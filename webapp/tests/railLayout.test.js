// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Rail-order tests for js/railLayout.js — zero dependencies, plain node:
//   node tests/railLayout.test.js
// Covers the boundary rules the shell renders from: dividers only around
// labeled groups, empty groups vanishing with their label, and the sim roster.

import assert from "node:assert/strict";
import { FOOTER_SECTIONS, GROUPS, SECTIONS, SIM_SECTIONS, railRows } from "../js/railLayout.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** Compact fingerprint of a row list: section keys, "|" for a plain divider, "|LABEL" for a labeled one. */
function fingerprint(rows) {
  return rows.map((r) => (r.kind === "divider" ? `|${r.label ?? ""}` : r.section.key)).join(" ");
}

test("full roster: labeled groups bracketed, standalone pages cluster", () => {
  assert.equal(
    fingerprint(railRows(GROUPS, null)),
    "teleop agent policy nav logging |AI Lab collect datasets training profiling |Maintenance armsdk calibration",
  );
});

test("sim roster: AI Lab vanishes with its label, Maintenance keeps Arm SDK", () => {
  assert.equal(
    fingerprint(railRows(GROUPS, SIM_SECTIONS)),
    "teleop agent policy nav logging |Maintenance armsdk",
  );
});

test("SECTIONS flattens GROUPS then footer, with unique keys", () => {
  assert.deepEqual(
    SECTIONS.map((s) => s.key),
    [...GROUPS.flatMap((g) => g.sections.map((s) => s.key)), ...FOOTER_SECTIONS.map((s) => s.key)],
  );
  assert.equal(new Set(SECTIONS.map((s) => s.key)).size, SECTIONS.length);
});

test("settings is footer-only and survives the sim filter", () => {
  assert.deepEqual(FOOTER_SECTIONS.map((s) => s.key), ["settings"]);
  assert.ok(SIM_SECTIONS.has("settings"));
});

// Synthetic roster: today's GROUPS ends with its labeled groups, so only this
// fixture reaches the labeled→unlabeled boundary (a plain divider) and the
// unlabeled→unlabeled one (none).
test("divider rules hold on boundaries the real roster never hits", () => {
  /** @param {string} key */
  const sec = (key) => ({ key, label: key, icon: "" });
  const groups = [
    { label: "A", sections: [sec("a")] },
    { label: null, sections: [sec("b")] },
    { label: null, sections: [sec("c")] },
    { label: "D", sections: [] },
    { label: "E", sections: [sec("e")] },
  ];
  assert.equal(fingerprint(railRows(groups, null)), "a | b c |E e");
  assert.equal(fingerprint(railRows(groups, new Set(["b", "c"]))), "b c");
});

console.log(`\n${passed} passed`);
