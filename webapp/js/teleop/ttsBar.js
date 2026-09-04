// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// TTS bar — press Enter to focus, type, then Enter again and the robot says it.
// While focused, keyboard drive is suppressed (the typing-context guard in
// keyboardDrive). Sending, Ctrl, or Esc returns focus to the page so WASD works.

import { AGENT_STATUS_TOPIC, TTS_TOPIC } from "../constants.js";

const SENT_FLASH_MS = 600;
const TTS_UNAVAILABLE_PLACEHOLDER = "Speech needs an Innate service key";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{
 *   onSpeak?: (text: string) => void,
 *   onAvailabilityChange?: (available: boolean) => void
 * }} [opts]
 * @returns {{ destroy: () => void }}
 */
export function createTtsBar(parent, rosClient, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "tts-bar";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "tts-input";
  input.placeholder = "Make the robot speak…";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Text to speech");
  input.setAttribute("aria-keyshortcuts", "Enter");

  const focusHint = document.createElement("button");
  focusHint.type = "button";
  focusHint.className = "tts-key tts-focus-key";
  focusHint.textContent = "↵";
  focusHint.setAttribute("aria-label", "Focus speech input");
  focusHint.setAttribute("aria-describedby", "tts-shortcut-tip");
  focusHint.onclick = () => input.focus();

  const shortcutTip = document.createElement("span");
  shortcutTip.id = "tts-shortcut-tip";
  shortcutTip.className = "tts-shortcut-tip";
  shortcutTip.setAttribute("role", "tooltip");
  shortcutTip.innerHTML = `
    <kbd class="tts-tip-key">Enter</kbd><span>focus input</span>
    <span aria-hidden="true">·</span>
    <kbd class="tts-tip-key">Ctrl</kbd><span>return to drive</span>
  `;

  const sendBtn = document.createElement("button");
  sendBtn.type = "button";
  sendBtn.className = "tts-key tts-send-key";
  sendBtn.innerHTML = '<span class="tts-send-icon" aria-hidden="true"></span>';
  sendBtn.title = "Make the robot speak";
  sendBtn.setAttribute("aria-label", "Make the robot speak");

  wrap.append(input, focusHint, sendBtn, shortcutTip);
  parent.appendChild(wrap);

  /** @type {number | null} */
  let flashTimer = null;

  function syncActions() {
    const hasText = input.value.trim().length > 0;
    focusHint.hidden = input.disabled || hasText;
    sendBtn.hidden = input.disabled || !hasText;
  }
  syncActions();

  function send() {
    const text = input.value.trim();
    if (!text) return false;
    rosClient.publish(TTS_TOPIC, { data: text });
    opts.onSpeak?.(text);
    input.value = "";
    syncActions();
    wrap.classList.add("sent");
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashTimer = null;
      wrap.classList.remove("sent");
    }, SENT_FLASH_MS);
    return true;
  }

  sendBtn.onclick = send;
  input.addEventListener("input", syncActions);
  let ctrlAlone = false;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Control") {
      if (!e.repeat) ctrlAlone = true;
      return;
    }
    if (e.ctrlKey) ctrlAlone = false;
    if (e.key === "Escape") {
      input.blur();
      return;
    }
    if (e.key === "Enter" && !e.isComposing && send()) input.blur();
  });
  input.addEventListener("keyup", (e) => {
    if (e.key !== "Control") return;
    if (ctrlAlone) input.blur();
    ctrlAlone = false;
  });
  input.addEventListener("blur", () => {
    ctrlAlone = false;
  });

  /** @param {KeyboardEvent} e */
  function focusSpeechInput(e) {
    // A skill form also uses Enter to launch. Its handler prevents the event,
    // closes the menu, and returns focus to body before this window listener
    // sees the same keydown; do not reinterpret that consumed Enter as chat.
    if (e.defaultPrevented || e.key !== "Enter" || e.repeat || e.altKey || e.ctrlKey || e.metaKey) return;
    const active = document.activeElement;
    const target = e.target;
    const fromTyping =
      target instanceof HTMLElement &&
      (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    const typing =
      active instanceof HTMLElement &&
      (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable);
    const neutral = !active || active === document.body;
    if (input.disabled || fromTyping || typing || !neutral) return;
    e.preventDefault();
    input.focus();
  }
  window.addEventListener("keydown", focusSpeechInput);

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
    syncActions();
    input.placeholder = available ? "Make the robot speak…" : TTS_UNAVAILABLE_PLACEHOLDER;
    input.title = available ? TTS_TOPIC : "The speak bar needs the hosted Innate brain (INNATE_SERVICE_KEY).";
    opts.onAvailabilityChange?.(available);
  }, undefined, "std_msgs/msg/String");

  return {
    destroy() {
      unsubStatus();
      if (flashTimer !== null) clearTimeout(flashTimer);
      window.removeEventListener("keydown", focusSpeechInput);
      wrap.remove();
    },
  };
}
