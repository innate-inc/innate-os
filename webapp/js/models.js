// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
//
// The models a robot can be pointed at, and whether it can reach one — shared by the
// Settings page and the agent studio so the two can never disagree. The reachability rule
// mirrors innate_llm/configure.py: a server URL, else the Innate service key for the
// vendors the proxy serves, else that vendor's own key. Change one and change the other.
//
// The marks below are generic shapes, not the vendors' logos: a spark, a sunburst, a ring.

/** @typedef {{ value: string, label: string, vendor: string }} ModelOption */

/** @type {ModelOption[]} Most capable first within each vendor. */
export const MODEL_OPTIONS = [
  { value: "google:gemini-3.8-flash", label: "Gemini 3.8 Flash", vendor: "google" },
  { value: "google:gemini-3.7-flash", label: "Gemini 3.7 Flash", vendor: "google" },
  { value: "google:gemini-3.6-flash", label: "Gemini 3.6 Flash", vendor: "google" },
  { value: "google:gemini-3.5-flash", label: "Gemini 3.5 Flash", vendor: "google" },
  { value: "openai:gpt-6-astra", label: "GPT-6 Astra", vendor: "openai" },
  { value: "openai:gpt-5.6-sol", label: "GPT-5.6 Sol", vendor: "openai" },
  { value: "openai:gpt-5.6-terra", label: "GPT-5.6 Terra", vendor: "openai" },
  { value: "openai:gpt-5.6-luna", label: "GPT-5.6 Luna", vendor: "openai" },
  { value: "anthropic:claude-fable-5-1", label: "Claude Fable 5.1", vendor: "anthropic" },
  { value: "anthropic:claude-opus-5", label: "Claude Opus 5", vendor: "anthropic" },
  { value: "anthropic:claude-sonnet-5", label: "Claude Sonnet 5", vendor: "anthropic" },
  { value: "anthropic:claude-haiku-4-5", label: "Claude Haiku 4.5", vendor: "anthropic" },
];

export const VENDOR_LABEL = { google: "Google", openai: "OpenAI", "openai-chat": "OpenAI-compatible", anthropic: "Anthropic" };
export const VENDOR_KEY = { google: "GEMINI_API_KEY", openai: "OPENAI_API_KEY", "openai-chat": "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY" };
const KEY_LABEL = { google: "Google", openai: "OpenAI", "openai-chat": "OpenAI", anthropic: "Anthropic" };

/** Vendor marks, 16px, drawn in currentColor so they take the row's own colour. */
const MARKS = {
  google: '<path d="M8 1.6l1.6 4.8 4.8 1.6-4.8 1.6L8 14.4l-1.6-4.8L1.6 8l4.8-1.6z"/>',
  anthropic:
    '<path d="M8 1.4v13.2M1.4 8h13.2M3.3 3.3l9.4 9.4M12.7 3.3l-9.4 9.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/>',
  // A hexagon ring: at 16px a six-petal rosette competes with the sunburst beside it, and
  // the vendors' own logos are theirs — three shapes that stay apart is what the row needs.
  openai: '<path d="M8 1.7l5.5 3.15v6.3L8 14.3l-5.5-3.15v-6.3z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/>',
};

/** @param {string} vendor */
export function vendorMark(vendor) {
  const body = MARKS[/** @type {keyof typeof MARKS} */ (vendor)] || MARKS.openai;
  return `<svg class="model-mark model-mark-${vendor}" viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true">${body}</svg>`;
}

/**
 * The vendor of a model spec, read the way innate_llm/models.py:split_spec reads it — or
 * null for a vendor prefix it rejects (with a server URL any name is the server's own).
 * @param {string} spec @param {string} [baseUrl]
 */
export function modelVendor(spec, baseUrl = "") {
  const [prefix, ...rest] = String(spec).split(":");
  if (rest.length && prefix in VENDOR_KEY) return prefix;
  if (baseUrl) return "openai-chat";
  if (rest.length) return null;
  if (spec.startsWith("claude")) return "anthropic";
  if (/^(gpt|o1|o3|o4)/.test(spec)) return "openai";
  return "google";
}

/** The catalog label for a spec, else the spec itself (a custom or LAN name). */
export function modelLabel(/** @type {string} */ spec) {
  return MODEL_OPTIONS.find((option) => option.value === spec)?.label || spec;
}

/**
 * Whether the robot can reach `spec` with the keys it has.
 * @param {string} spec @param {string} baseUrl
 * @param {{keys?: Record<string, {set: boolean}>, service_key?: boolean}} status
 * @returns {{ok: boolean, text: string}}
 */
export function modelReach(spec, baseUrl, status) {
  const vendor = modelVendor(spec, baseUrl);
  if (vendor === null) {
    return { ok: false, text: "Unknown vendor: use google:, anthropic:, openai: or openai-chat: before the model name." };
  }
  if (vendor === "openai-chat" && baseUrl) return { ok: true, text: `Reached through your server at ${baseUrl}.` };
  const key = /** @type {keyof typeof VENDOR_KEY} */ (vendor);
  const own = Boolean(status.keys?.[VENDOR_KEY[key]]?.set);
  if (vendor === "anthropic") {
    if (own) return { ok: true, text: "Reached with your Anthropic key." };
    // The Innate proxy does not serve Anthropic, so the service key cannot stand in here.
    return { ok: false, text: "Needs an Anthropic key — the Innate proxy does not serve Claude. Add one in Settings → Agent → Keys." };
  }
  if (status.service_key) return { ok: true, text: "Reached through the Innate proxy." };
  if (own) return { ok: true, text: `Reached with your ${KEY_LABEL[key]} key.` };
  return { ok: false, text: `Needs an Innate service key or a ${KEY_LABEL[key]} key. Add one in Settings → Agent → Keys.` };
}

/** The keys the robot has, for modelReach. Never carries a key's value. */
export async function fetchKeyStatus() {
  try {
    const res = await fetch("/keys.json", { cache: "no-store" });
    return { ...(await res.json()), loaded: true };
  } catch {
    // An older robot has no such route (its SPA fallback answers HTML): assume nothing,
    // which marks vendor models as needing a key rather than claiming they are reachable.
    return { keys: {}, service_key: false, readonly: true, loaded: false };
  }
}
