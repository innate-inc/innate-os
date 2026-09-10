// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// The offer deck: what the interface asks the person for right now, docked above the
// composer so it is never scrolled away from. Characters to become are cards in a grid,
// a skill to grant is one card with a plus, and things to say are chips in a row.

import { cue } from "./cue.js";
import { ICONS } from "./storyCards.js";

/** persona, custom and grant are cards; the rest are chips. @typedef {"persona" | "grant" | "reply" | "random" | "custom"} OfferKind */
/** `text` is what selecting it means (and sends); `label` is what the card says instead, when shorter.
 * @typedef {{ text: string, kind: OfferKind, label?: string, detail?: string, icon?: string, hue?: string, onSelect: (text: string) => void }} Offer */

const CARD_KINDS = new Set(["persona", "custom", "grant"]);
const CHIP_ICONS = /** @type {Partial<Record<OfferKind, string>>} */ ({ random: ICONS.dice });

/** @returns {{ el: HTMLElement, set: (offers: Offer[], title?: string) => void }} */
export function createOfferDeck() {
  const el = document.createElement("div");
  el.className = "agent-offers";
  el.hidden = true;
  const head = document.createElement("div");
  head.className = "agent-offers-head";
  head.innerHTML = '<span class="microlabel agent-offers-kicker"></span><span class="agent-offers-title"></span>';
  const kicker = /** @type {HTMLElement} */ (head.querySelector(".agent-offers-kicker"));
  const title = /** @type {HTMLElement} */ (head.querySelector(".agent-offers-title"));
  const grid = document.createElement("div");
  grid.className = "agent-offers-grid";
  const row = document.createElement("div");
  row.className = "agent-offers-row";
  el.append(head, grid, row);
  // Cards are an ask, and the deck wears the "look here" itself; chips are only an offer.
  // Every new ask locks on afresh, so the eye is caught again.
  let asking = "";
  /** @type {(() => void) | null} */ let uncue = null;

  /** @param {Offer} offer */
  function card(offer) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `agent-offer-card ${offer.kind}`;
    if (offer.hue) button.style.setProperty("--offer-hue", offer.hue);
    button.innerHTML =
      `<span class="agent-offer-icon">${offer.icon ?? ICONS.sparkle}</span>` +
      '<span class="agent-offer-copy"><span class="agent-offer-name"></span><span class="agent-offer-detail"></span></span>' +
      (offer.kind === "grant" ? `<span class="agent-offer-go">${ICONS.plus}<span>Grant</span></span>` : "") +
      (offer.kind === "custom" ? `<span class="agent-offer-go">${ICONS.pen}<span>Type</span></span>` : "");
    /** @type {HTMLElement} */ (button.querySelector(".agent-offer-name")).textContent = offer.label ?? offer.text;
    /** @type {HTMLElement} */ (button.querySelector(".agent-offer-detail")).textContent = offer.detail ?? "";
    button.addEventListener("click", () => offer.onSelect(offer.text));
    return button;
  }

  /** @param {Offer} offer */
  function chip(offer) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `agent-offer-chip ${offer.kind}`;
    const glyph = CHIP_ICONS[offer.kind];
    if (glyph) button.innerHTML = glyph;
    button.append(offer.text);
    button.addEventListener("click", () => offer.onSelect(offer.text));
    return button;
  }

  /** @param {Offer[]} offers @param {string} [heading] */
  function set(offers, heading = "") {
    const cards = offers.filter((o) => CARD_KINDS.has(o.kind));
    const chips = offers.filter((o) => !CARD_KINDS.has(o.kind));
    el.hidden = !offers.length;
    const ask = cards.map((o) => `${o.kind}:${o.text}`).join("|");
    if (ask !== asking) {
      uncue?.();
      uncue = ask ? cue(el, "Your move") : null;
      asking = ask;
    }
    if (el.hidden) return;
    kicker.hidden = cards.length > 0;
    kicker.textContent = "Try asking";
    title.textContent = heading;
    title.hidden = !heading;
    grid.replaceChildren(...cards.map(card));
    grid.hidden = !cards.length;
    row.replaceChildren(...chips.map(chip));
    row.hidden = !chips.length;
  }

  return { el, set };
}
