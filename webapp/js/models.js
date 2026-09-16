// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
//
// The models a robot can be pointed at, and whether it can reach one — shared by the
// Settings page and the agent studio so the two can never disagree. The reachability rule
// mirrors innate_llm/configure.py: a server URL, else the Innate service key for the
// vendors the proxy serves, else that vendor's own key. Change one and change the other.
//
// No vendor marks: a drawn lookalike is what Anthropic ("no alterations"), Google ("don't
// imitate our visual identity") and OpenAI ("don't design a similar logo") each rule out,
// and their real logos need written permission. The names identify the models by themselves.

/**
 * @typedef {{ value: string, label: string, vendor: string, panel?: boolean, custom?: boolean }} ModelOption
 * `panel: false` keeps a model off the agent panel's quick menu; Settings still offers it.
 * `custom` marks the robot's current model when it is no catalog entry.
 */

/** @type {ModelOption[]} Most capable first within each vendor. */
export const MODEL_OPTIONS = [
  { value: "google:gemini-3.8-flash", label: "Gemini 3.8 Flash", vendor: "google" },
  { value: "google:gemini-3.7-flash", label: "Gemini 3.7 Flash", vendor: "google" },
  { value: "google:gemini-3.6-flash", label: "Gemini 3.6 Flash", vendor: "google" },
  { value: "google:gemini-3.5-flash", label: "Gemini 3.5 Flash", vendor: "google" },
  { value: "openai:gpt-6-astra", label: "GPT-6 Astra", vendor: "openai", panel: false },
  { value: "openai:gpt-5.6-sol", label: "GPT-5.6 Sol", vendor: "openai" },
  { value: "openai:gpt-5.6-terra", label: "GPT-5.6 Terra", vendor: "openai" },
  { value: "openai:gpt-5.6-luna", label: "GPT-5.6 Luna", vendor: "openai" },
  { value: "anthropic:claude-fable-5-1", label: "Claude Fable 5.1", vendor: "anthropic" },
  { value: "anthropic:claude-opus-5", label: "Claude Opus 5", vendor: "anthropic" },
  { value: "anthropic:claude-sonnet-5", label: "Claude Sonnet 5", vendor: "anthropic" },
  { value: "anthropic:claude-haiku-4-5", label: "Claude Haiku 4.5", vendor: "anthropic" },
];

export const VENDOR_LABEL = { google: "Google", openai: "OpenAI", "openai-chat": "OpenAI-compatible", anthropic: "Anthropic", custom: "Custom" };
export const VENDOR_KEY = { google: "GEMINI_API_KEY", openai: "OPENAI_API_KEY", "openai-chat": "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY" };
const KEY_LABEL = { google: "Google", openai: "OpenAI", "openai-chat": "OpenAI", anthropic: "Anthropic" };

/**
 * The vendor of a model spec, read the way innate_llm/models.py:split_spec reads it — or
 * null for a vendor prefix it rejects (with a server URL any name is the server's own).
 * @param {string} spec @param {string} [baseUrl]
 */
export function modelVendor(spec, baseUrl = "") {
  spec = String(spec).trim();  // configure() strips before it splits; so must this
  const [prefix, ...rest] = spec.split(":");
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
 * The agent panel's menu: its options, plus the robot's current model when it is not among
 * them — a Settings pick, a LAN model — beside its vendor's own, else last under Custom.
 * @param {string} current @returns {ModelOption[]}
 */
export function panelOptions(current) {
  const options = MODEL_OPTIONS.filter((option) => option.panel !== false);
  if (!current || options.some((option) => option.value === current)) return options;
  const extra = { value: current, label: modelLabel(current), vendor: modelVendor(current) || "custom", custom: true };
  let at = options.length;
  options.forEach((option, i) => {
    if (option.vendor === extra.vendor) at = i + 1;
  });
  options.splice(at, 0, extra);
  return options;
}

/**
 * Whether the robot can reach `spec` with the keys it has.
 * @param {string} spec @param {string} baseUrl
 * @param {{keys?: Record<string, {set: boolean}>, service_key?: boolean}} status
 * @returns {{ok: boolean, text: string, short: string}}  `short` names the gap alone, for a
 * vendor heading; `text` is the whole sentence, for the one place a model is named.
 */
export function modelReach(spec, baseUrl, status) {
  const vendor = modelVendor(spec, baseUrl);
  if (vendor === null) {
    const text = "Unknown vendor: use google:, anthropic:, openai: or openai-chat: before the model name.";
    return { ok: false, text, short: "Unknown vendor" };
  }
  if (vendor === "openai-chat" && baseUrl) {
    return { ok: true, text: `Reached through your server at ${baseUrl}.`, short: "" };
  }
  const key = /** @type {keyof typeof VENDOR_KEY} */ (vendor);
  const own = Boolean(status.keys?.[VENDOR_KEY[key]]?.set);
  if (vendor === "anthropic") {
    if (own) return { ok: true, text: "Reached with your Anthropic key.", short: "" };
    // The Innate proxy does not serve Anthropic, so the service key cannot stand in here.
    const text = "Needs an Anthropic key — the Innate proxy does not serve Claude. Add one in Settings → Agent → Keys.";
    return { ok: false, text, short: "needs an Anthropic key" };
  }
  if (status.service_key) return { ok: true, text: "Reached through the Innate proxy.", short: "" };
  if (own) return { ok: true, text: `Reached with your ${KEY_LABEL[key]} key.`, short: "" };
  const text = `Needs an Innate service key or a ${KEY_LABEL[key]} key. Add one in Settings → Agent → Keys.`;
  return { ok: false, text, short: `needs a ${KEY_LABEL[key]} key` };
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
