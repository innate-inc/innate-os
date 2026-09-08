// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

/** @param {string} name */
export const isPromptSuggestionSkill = name => (name.split("/").at(-1) ?? "").replace(/[^a-z0-9]/gi, "").toLowerCase() === "suggestuserprompts";

/** Consume validated tool completions, without replaying obsolete suggestions.
 * Events arrive in order on one socket, so "running after the last clear" is
 * the whole freshness test; their timestamps are the robot's clock, not ours.
 * @param {(prompts: string[] | null) => void} display */
export function createPromptSuggestions(display) {
  const runs = new Set();
  return {
    clear() { runs.clear(); display(null); },
    /** @param {any} event @returns {boolean} whether this is a suggestion event */
    consume(event) {
      if (!isPromptSuggestionSkill(String(event.skill_id ?? event.primitive_name ?? event.skill_name ?? ""))) return false;
      const key = event.primitive_id;
      if (!key) return true;
      if (event.status === "running") runs.add(key);
      if (event.status === "completed" && runs.delete(key)) {
        const prompts = event.args?.prompts;
        if (Array.isArray(prompts) && prompts.length <= 3 && prompts.every(p => typeof p === "string" && p.trim() && p.length <= 160)) display(prompts);
      } else if (["failed", "interrupted"].includes(event.status)) runs.delete(key);
      return true;
    },
  };
}
