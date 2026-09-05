// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Mapping banner — the recording controls pinned over the scene while the
// robot is in mapping mode, a pure view of the nav store. Two steps, like
// the mobile app's record → name screens:
//
//   recording:  "…hold ⇧ Shift to drive slowly"   [Finish] [Discard]
//   naming:     name field + inline validation    [Save]   [Back]
//
// Visibility follows the store's mode (topic-driven), so a session started
// from the mobile app shows the same controls here; leaving either mapping
// mode — by whoever — resets the banner to the recording step for next time.

import { isMappingNavMode } from "../constants.js";
import { confirmDialog } from "./confirm.js";
import { MAP_NAME_RE } from "./navStore.js";

/**
 * Finish is a real motion boundary in autonomous mapping. Moving to manual
 * mapping makes mode_manager close goal admission, cancel and settle Nav2,
 * and leave slam_toolbox running for naming/saving.
 * @param {ReturnType<typeof import("./navStore.js").createNavStore>} store
 */
export async function quiesceForMapNaming(store) {
  if (store.state.mode !== "autonomous_mapping") return true;
  return store.changeMode("mapping");
}

// The ⇧ glyph as an outline path rather than the unicode character: its
// rendering varies wildly by platform font, and this matches the stroke
// language of every other icon in the app.
const SHIFT_ICON =
  '<svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><path d="M8 2.2l5.2 5.6H10.4V13.4H5.6V7.8H2.8z"/></svg>';

/** A Shift key cap, in the same visual language as the WASD hint chips. */
function shiftKeyCap() {
  const kbd = document.createElement("kbd");
  kbd.className = "mapping-kbd";
  kbd.innerHTML = `${SHIFT_ICON}<span>Shift</span>`;
  return kbd;
}

/**
 * @param {HTMLElement} scene the map stage the banner overlays.
 * @param {ReturnType<typeof import("./navStore.js").createNavStore>} store
 * @returns {{ destroy: () => void }}
 */
export function createMappingSession(scene, store) {
  const banner = document.createElement("div");
  banner.className = "mapping-banner";
  banner.hidden = true;

  // Prebuilt prose spans rather than retitling on every render: the manual
  // recording line carries a key cap, and render() runs on every store change.
  const recordingText = document.createElement("span");
  recordingText.className = "mapping-banner-text";
  recordingText.append("Recording map — cover the space, hold ", shiftKeyCap(), " to drive slowly");
  const autonomousText = document.createElement("span");
  autonomousText.className = "mapping-banner-text";
  autonomousText.textContent = "Autonomous mapping — joystick or WASD takes over";
  const namingText = document.createElement("span");
  namingText.className = "mapping-banner-text";
  namingText.textContent = "Name this map";

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "mapping-name mono";
  nameInput.placeholder = "map name";
  nameInput.title = "Letters, digits, _ and - only";
  const hint = document.createElement("span");
  hint.className = "mapping-hint mono";
  const primaryBtn = document.createElement("button");
  primaryBtn.type = "button";
  primaryBtn.className = "mapping-btn";
  const secondaryBtn = document.createElement("button");
  secondaryBtn.type = "button";
  secondaryBtn.className = "mapping-btn danger";
  banner.append(recordingText, autonomousText, namingText, nameInput, hint, primaryBtn, secondaryBtn);
  scene.appendChild(banner);

  /** @type {"recording" | "naming"} */
  let step = "recording";
  // mode_manager publishes a transient `switching` mode before the service
  // response and final `mapping` topic. Preserve Finish's naming intent across
  // either delivery order; unrelated exits still reset the next session.
  let namingTransitionPending = false;

  /** Inline validation while typing; returns the trimmed name if saveable. */
  function validName() {
    const name = nameInput.value.trim();
    if (!name) {
      hint.textContent = "";
      return null;
    }
    if (!MAP_NAME_RE.test(name)) {
      hint.textContent = /^[_-]+$/.test(name) ? "needs at least one letter or digit" : "letters, digits, _ or - only";
      return null;
    }
    hint.textContent = store.state.maps.includes(`${name}.yaml`) ? "exists — Save overwrites" : "";
    return name;
  }

  /** @param {import("./navStore.js").NavState} s */
  function render(s) {
    const mapping = isMappingNavMode(s.mode);
    banner.hidden = !mapping;
    if (!mapping) {
      if (!namingTransitionPending) {
        step = "recording";
        nameInput.value = "";
        hint.textContent = "";
      }
      return;
    }
    if (namingTransitionPending && s.mode === "mapping") {
      step = "naming";
      namingTransitionPending = false;
    }
    const naming = step === "naming";
    const autonomous = s.mode === "autonomous_mapping";
    recordingText.hidden = naming || autonomous;
    autonomousText.hidden = naming || !autonomous;
    namingText.hidden = !naming;
    nameInput.hidden = !naming;
    hint.hidden = !naming;
    primaryBtn.textContent = naming ? "Save" : "Finish";
    primaryBtn.title = naming ? "Save the map and make it the active one" : "Stop recording and name the map";
    secondaryBtn.textContent = naming ? "Back" : "Discard";
    secondaryBtn.title = naming ? "Return to recording" : "Throw away the recording and leave mapping mode";
    secondaryBtn.classList.toggle("danger", !naming);
    primaryBtn.disabled = !!s.busy || (naming && validName() === null);
    secondaryBtn.disabled = !!s.busy;
    nameInput.disabled = !!s.busy;
  }

  primaryBtn.addEventListener("click", async () => {
    if (step === "recording") {
      const leavingAutonomous = store.state.mode === "autonomous_mapping";
      namingTransitionPending = leavingAutonomous;
      if (!(await quiesceForMapNaming(store))) {
        namingTransitionPending = false;
        render(store.state);
        return;
      }
      step = "naming";
      if (store.state.mode === "mapping") namingTransitionPending = false;
      render(store.state);
      nameInput.focus();
      return;
    }
    const name = validName();
    if (!name) return;
    const overwrite = store.state.maps.includes(`${name}.yaml`);
    if (overwrite) {
      const ok = await confirmDialog({
        title: "Overwrite map?",
        body: `${name}.yaml already exists and will be replaced.`,
        confirmLabel: "Overwrite",
        danger: true,
      });
      if (!ok) return;
    }
    await store.saveAndActivate(name, overwrite);
    // On success the mode topic flips to navigation and render() resets us.
  });

  secondaryBtn.addEventListener("click", async () => {
    if (step === "naming") {
      step = "recording";
      render(store.state);
      return;
    }
    // Discard. With no saved maps there is no navigation to return to —
    // offer map-free instead of stranding the robot mapless.
    const mapless = store.state.maps.length === 0;
    const ok = await confirmDialog({
      title: "Discard recording?",
      body: mapless
        ? "The map progress is lost and the robot switches to map-free mode (no map)."
        : "The map progress is lost and the robot returns to navigation.",
      confirmLabel: "Discard",
      danger: true,
    });
    if (ok) await store.changeMode(mapless ? "mapfree" : "navigation");
  });

  nameInput.addEventListener("input", () => render(store.state));
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !primaryBtn.disabled) primaryBtn.click();
    if (e.key === "Escape") secondaryBtn.click();
  });

  const unsub = store.onChange(render);

  return {
    destroy() {
      unsub();
      banner.remove();
    },
  };
}
