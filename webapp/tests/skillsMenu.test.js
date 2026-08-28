// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import assert from "node:assert/strict";
import { nextSkillIndex, searchSkills } from "../js/teleop/skillsMenu.js";

const skills = [
  { id: "innate-os/wave", name: "Wave", group: "People" },
  { id: "innate-os/pick_any_object", name: "Pick Any Object", group: "Manipulation" },
  { id: "workspace/pick_sock", name: "Pick Sock", group: "Manipulation" },
];

const matches = searchSkills(skills, "pick manipulation");
const initial = 0;
const afterArrowDown = nextSkillIndex(initial, matches.length, 1);

assert.deepEqual(
  {
    matches: matches.map((skill) => skill.id),
    initiallySelected: matches[initial].id,
    enterAfterArrowDown: matches[afterArrowDown].id,
  },
  {
    matches: ["innate-os/pick_any_object", "workspace/pick_sock"],
    initiallySelected: "innate-os/pick_any_object",
    enterAfterArrowDown: "workspace/pick_sock",
  },
);

console.log("ok - skills menu search and keyboard path");
