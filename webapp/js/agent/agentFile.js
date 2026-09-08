// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The Agent Studio form rendered as an agent file, for the live code pane.
// A port of brain_client/agents/studio.py `render_agent` — the brain renders
// the same form again when it saves, so this only has to match closely enough
// that the pane doesn't jump on save; the file on disk is always the brain's.
// Pure: no DOM, testable in plain node.

/** @typedef {{ id: string, displayName: string, prompt: string, skillIds: string[], listen: boolean, gaze: boolean }} AgentSpec */
/** @typedef {{ module: string, className: string }} SkillImport */

const MICRO_IMPORT = { module: "inputs.micro_input", className: "MicroInput" };
const LINE_LENGTH = 120;

/** `kitchen_helper` -> `KitchenHelperAgent` (physical_refs.class_name_for + the Agent suffix). @param {string} agentId */
export function agentClassName(agentId) {
  const leaf = agentId.split("/").pop() ?? "";
  const parts = leaf.split(/[-_. ]+/).filter(Boolean);
  return parts.map((p) => p[0].toUpperCase() + p.slice(1)).join("") + "Agent";
}

/** @param {string} text */
function strLiteral(text) {
  return `"${text.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n")}"`;
}

/** Escape for the inside of a triple-quoted literal; a trailing unescaped quote would merge with the delimiter. @param {string} text */
function escapeTriple(text) {
  let escaped = text.replace(/\\/g, "\\\\").replace(/"""/g, '\\"\\"\\"');
  const trailing = /(\\*)"$/.exec(escaped);
  if (trailing && trailing[1].length % 2 === 0) escaped = escaped.slice(0, -1) + '\\"';
  return escaped;
}

/** @param {string} prompt */
function promptLiteral(prompt) {
  return prompt ? `"""${escapeTriple(prompt)}"""` : '""';
}

/** @param {string} text */
function docstringBlock(text) {
  const body = escapeTriple(text.trim())
    .split("\n")
    .map((line) => `    ${line}`.replace(/\s+$/, ""))
    .join("\n");
  return `    """\n${body}\n    """`;
}

/** @param {string[]} refs */
function listReturn(refs) {
  const oneLine = `        return [${refs.join(", ")}]`;
  if (oneLine.length <= LINE_LENGTH) return [oneLine];
  return ["        return [", ...refs.map((ref) => `            ${ref},`), "        ]"];
}

/**
 * @param {AgentSpec} spec
 * @param {Map<string, SkillImport>} imports skill id -> importable class, from the skills roster
 * @param {string} [docstring] carried over from the existing file; default names the studio
 */
export function renderAgent(spec, imports, docstring) {
  const className = agentClassName(spec.id);
  /** @type {Map<string, string>} class name -> module */
  const imported = new Map();
  /** @type {string[]} */
  const refs = [];
  for (const skillId of spec.skillIds) {
    const { module, className: name } = imports.get(skillId) ?? { module: "", className: "" };
    const clash = (imported.get(name) ?? module) !== module || name === className;
    if (!module || !name || clash) {
      refs.push(strLiteral(skillId));
      continue;
    }
    imported.set(name, module);
    refs.push(name);
  }
  if (spec.listen) imported.set(MICRO_IMPORT.className, MICRO_IMPORT.module);
  const types = spec.listen ? "Agent, InputRef, SkillRef" : "Agent, SkillRef";

  const lines = [...imported].map(([name, module]) => `from ${module} import ${name}`).sort();
  if (lines.length) lines.push("");
  lines.push(
    `from brain_client.agents.types import ${types}`,
    "",
    "",
    `class ${className}(Agent):`,
    docstringBlock(docstring || `${spec.displayName} - made in Agent Studio.`),
    "",
    "    @property",
    "    def id(self) -> str:",
    `        return ${strLiteral(spec.id)}`,
    "",
    "    @property",
    "    def display_name(self) -> str:",
    `        return ${strLiteral(spec.displayName)}`,
    "",
    "    def get_skills(self) -> list[SkillRef]:",
    ...listReturn(refs),
  );
  if (spec.listen) lines.push("", "    def get_inputs(self) -> list[InputRef]:", `        return [${MICRO_IMPORT.className}]`);
  lines.push("", "    def get_prompt(self) -> str:", `        return ${promptLiteral(spec.prompt)}`);
  if (spec.gaze) lines.push("", "    def uses_gaze(self) -> bool:", "        return True");
  return lines.join("\n") + "\n";
}
