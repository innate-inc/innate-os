// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// What the story's offers look like: an icon, a tint and one line for each character
// the world proposes and each skill the robot asks for. Keyed by the world's own
// strings, with a fallback, so a story that adds a persona or a skill still draws.

const SVG_OPEN =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
/** @param {string} paths */
const icon = (paths) => `${SVG_OPEN}${paths}</svg>`;

export const ICONS = {
  sparkle: icon('<path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/>'),
  gem: icon('<path d="M6 3h12l4 6-10 13L2 9Z"/><path d="M11 3 8 9l4 13 4-13-3-6"/><path d="M2 9h20"/>'),
  cat: icon(
    '<path d="M12 5c.67 0 1.35.09 2 .26 1.78-2 5.03-2.84 6.42-2.26 1.4.58-.42 7-.42 7 .57 1.07 1 2.24 1 3.44C21 17.9 16.97 21 12 21s-9-3-9-7.56c0-1.25.5-2.4 1-3.44 0 0-1.89-6.42-.5-7 1.39-.58 4.72.23 6.5 2.23A9.04 9.04 0 0 1 12 5Z"/><path d="M8 14v.5"/><path d="M16 14v.5"/><path d="M11.25 16.25h1.5L12 17l-.75-.75Z"/>',
  ),
  drama: icon(
    '<path d="M10 11h.01"/><path d="M14 6h.01"/><path d="M18 6h.01"/><path d="M6.5 13.1h.01"/><path d="M22 5c0 9-4 12-6 12s-6-3-6-12c0-2 2-3 6-3s6 1 6 3"/><path d="M17.4 9.9c-.8.8-2 .8-2.8 0"/><path d="M10.1 7.1C9 7.2 7.7 7.7 6 8.6c-3.5 2-4.7 3.9-3.7 5.6 4.5 7.8 9.5 8.4 11.2 7.4.9-.5 1.9-2.1 1.9-4.7"/><path d="M9.1 16.5c.3-1.1 1.4-1.7 2.4-1.4"/>',
  ),
  skull: icon(
    '<path d="m12.5 17-.5-1-.5 1h1z"/><path d="M15 22a1 1 0 0 0 1-1v-1a2 2 0 0 0 1.56-3.25 8 8 0 1 0-11.12 0A2 2 0 0 0 8 20v1a1 1 0 0 0 1 1z"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="12" r="1"/>',
  ),
  dice: icon(
    '<rect width="18" height="18" x="3" y="3" rx="3"/><path d="M16 8h.01"/><path d="M8 8h.01"/><path d="M8 16h.01"/><path d="M16 16h.01"/><path d="M12 12h.01"/>',
  ),
  pen: icon('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>'),
  smile: icon(
    '<circle cx="12" cy="12" r="9.5"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><path d="M9 9h.01"/><path d="M15 9h.01"/>',
  ),
  rotate: icon('<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'),
  hand: icon(
    '<path d="M18 11V6a2 2 0 0 0-4 0"/><path d="M14 10V4a2 2 0 0 0-4 0v2"/><path d="M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
  ),
  pin: icon('<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>'),
  history: icon(
    '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  ),
  plus: icon('<path d="M12 5v14"/><path d="M5 12h14"/>'),
};

/** @typedef {{ label: string, icon: string, hue: string, detail: string }} PersonaCard */
/** @type {Record<string, PersonaCard>} */
const PERSONA_CARDS = {
  "Rocky from Project Hail Mary": { label: "Rocky", icon: ICONS.gem, hue: "#e8a33d", detail: "Project Hail Mary. Amaze!" },
  "a grumpy cat": { label: "Grumpy cat", icon: ICONS.cat, hue: "#8fa3c4", detail: "Few words, all contemptuous." },
  "a Shakespearean actor": { label: "Shakespearean actor", icon: ICONS.drama, hue: "#e07a8a", detail: "Thee, thou, soliloquies." },
  "a pirate captain": { label: "Pirate captain", icon: ICONS.skull, hue: "#4fc3b0", detail: "Arr. This room is a brig." },
};

/** @param {string} name the persona as the world names it */
export const personaCard = (name) => PERSONA_CARDS[name] ?? { label: name, icon: ICONS.sparkle, hue: "#9482ff", detail: "" };

/** @typedef {{ icon: string, detail: string }} SkillCard */
/** What the skill lets the robot do, in the person's words. @type {Record<string, SkillCard>} */
const SKILL_CARDS = {
  "innate-os/head_emotion": { icon: ICONS.smile, detail: "Make a face for what it feels" },
  "innate-os/turn_in_place": { icon: ICONS.rotate, detail: "Spin around and look at the room" },
  "innate-os/pick_any_object": { icon: ICONS.hand, detail: "Reach down and grab things" },
  "innate-os/navigate_to_position": { icon: ICONS.pin, detail: "Drive itself to a spot" },
  "innate-os/search_memory": { icon: ICONS.history, detail: "Think back to places it has seen" },
};

/** @param {string} id the skill id as the brain names it */
export const skillCard = (id) => SKILL_CARDS[id] ?? { icon: ICONS.sparkle, detail: "" };
