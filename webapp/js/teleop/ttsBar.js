// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// TTS bar — type, Enter, the robot says it. While focused, keyboard drive is
// suppressed (the typing-context guard in keyboardDrive). Esc returns focus
// to the page so WASD works again.

import { AGENT_STATUS_TOPIC, TTS_TOPIC } from "../constants.js";

const SENT_FLASH_MS = 600;
const TTS_UNAVAILABLE_PLACEHOLDER = "Speech needs an Innate service key";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createTtsBar(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "tts-bar";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "tts-input";
  input.placeholder = "Make the robot speak…";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Text to speech");

  const sendBtn = document.createElement("button");
  sendBtn.type = "button";
  sendBtn.className = "tts-hint mono";
  sendBtn.textContent = "↵";
  sendBtn.setAttribute("aria-label", "Send");

  wrap.append(input, sendBtn);
  parent.appendChild(wrap);

  /** @type {number | null} */
  let flashTimer = null;

  function send() {
    const text = input.value.trim();
    if (!text) return;
    rosClient.publish(TTS_TOPIC, { data: text });
    input.value = "";
    wrap.classList.add("sent");
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashTimer = null;
      wrap.classList.remove("sent");
    }, SENT_FLASH_MS);
  }

  sendBtn.onclick = send;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      input.blur();
      return;
    }
    if (e.key === "Enter") send();
  });

  // Speech runs through Innate's hosted brain; with a local (Gemini-only)
  // backend the brain broadcasts tts_available=false and the bar grays out
  // instead of swallowing text. Absent field (older brains) = available.
  const unsubStatus = rosClient.subscribe(AGENT_STATUS_TOPIC, (msg) => {
    let payload;
    try {
      payload = JSON.parse(msg?.data ?? "");
    } catch {
      return;
    }
    if (typeof payload?.tts_available !== "boolean") return;
    const available = payload.tts_available;
    input.disabled = !available;
    sendBtn.disabled = !available;
    input.placeholder = available ? "Make the robot speak…" : TTS_UNAVAILABLE_PLACEHOLDER;
    input.title = available ? "" : "The speak bar needs the hosted Innate brain (INNATE_SERVICE_KEY).";
  });

  return {
    destroy() {
      unsubStatus();
      if (flashTimer !== null) clearTimeout(flashTimer);
      wrap.remove();
    },
  };
}
